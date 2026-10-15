#!/usr/bin/env python3
"""
Tool gửi comment lên bài viết Gapowork, xoay vòng nhiều tài khoản.

Chạy:  python gapo_comment.py

Thêm acc: trên Gapowork mở DevTools > Network, gửi tay 1 comment, chuột phải request
"comments" > Copy > Copy as cURL (hoặc copy cả khung Headers), dán vào ô "Thêm acc".
Tool lấy Authorization, x-gapo-workspace-id, x-gapo-lang và author_id (uid trong token).
Dán lại cURL của acc đã có -> cập nhật token mới.
Danh sách acc được lưu vào accounts.json để lần sau mở lại không cần dán.

Comment chạy hết danh sách sẽ quay lại dòng đầu, lặp đến khi bấm Dừng.
Log ghi ra màn hình và file gapo_comment.log.
"""

from __future__ import annotations

import base64
import itertools
import json
import queue
import random
import re
import sys
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import requests

API_URL = "https://api.gapowork.vn/comment/v2.1/comments"
# Khi đóng gói .exe (PyInstaller) lưu file cạnh .exe, không phải thư mục tạm
BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "gapo_comment.log"
ACC_FILE = BASE_DIR / "accounts.json"

JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
AUTHOR_IN_TEXT_RE = re.compile(r'author_id\\?"\s*:\s*\\?"?(\d+)')
CURL_HEADER_RE = re.compile(r"(?:-H|--header)\s+\$?(?:'([^']*)'|\"([^\"]*)\")")

DEFAULT_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.gapowork.vn",
    "Referer": "https://www.gapowork.vn/",
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
}


# ---------------------------------------------------------------- parse

def keep_header(name: str) -> bool:
    """Chỉ giữ header cần cho xác thực; còn lại dùng DEFAULT_HEADERS."""
    return name in ("authorization", "accept-language", "user-agent") or name.startswith("x-gapo-")


def parse_header_lines(text: str) -> dict:
    """Parse header đã dán, hỗ trợ 2 dạng:
    - 'name: value' mỗi dòng (Copy request headers)
    - tên 1 dòng, giá trị dòng dưới (copy từ khung Headers của DevTools)
    """
    headers = {}
    lines = [l.strip() for l in text.splitlines()]
    for i, line in enumerate(lines):
        m = re.match(r"^([A-Za-z0-9-]+)\s*:\s*(.+)$", line)
        if m and keep_header(m.group(1).lower()):
            headers[m.group(1).lower()] = m.group(2).strip()
        elif keep_header(line.lower()) and i + 1 < len(lines) and lines[i + 1]:
            headers[line.lower()] = lines[i + 1]
    return headers


def parse_curl(text: str) -> dict:
    headers = {}
    for a, b in CURL_HEADER_RE.findall(text):
        h = a or b
        if ":" not in h:
            continue
        name, value = h.split(":", 1)
        name = name.strip().lower()
        if keep_header(name):
            headers[name] = value.strip()
    return headers


def parse_pasted(text: str) -> tuple[dict, str | None]:
    """Nhận cURL / khung Headers / token. Trả về (headers, author_id nếu có)."""
    headers = parse_curl(text) if re.match(r"\s*curl\s", text) else parse_header_lines(text)
    if "authorization" not in headers:
        jwts = JWT_RE.findall(text)
        if jwts:
            headers["authorization"] = f"Bearer {jwts[0]}"
    m = AUTHOR_IN_TEXT_RE.search(text)
    return headers, (m.group(1) if m else None)


def decode_jwt(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def mask(s: str) -> str:
    return s if len(s) <= 24 else f"{s[:16]}…{s[-4:]}"


# ---------------------------------------------------------------- account

class Account:
    def __init__(self, headers: dict, author_id: str | None = None):
        self.headers = {k.lower(): v for k, v in headers.items() if keep_header(k.lower())}
        jwts = JWT_RE.findall(self.headers.get("authorization", ""))
        self.claims = decode_jwt(jwts[0]) if jwts else {}
        self.author_id = author_id or self._uid_from_token()
        # Thiếu workspace-id mà token chỉ thuộc 1 workspace -> lấy luôn
        if "x-gapo-workspace-id" not in self.headers:
            wsids = [w.strip() for w in str(self.claims.get("wsids", "")).split(",") if w.strip()]
            if len(wsids) == 1:
                self.headers["x-gapo-workspace-id"] = wsids[0]
        self.session: requests.Session | None = None
        self.alive = True

    @property
    def name(self) -> str:
        return f"acc_{self.author_id}"

    def _uid_from_token(self) -> str | None:
        for key in ("uid", "user_id", "userId", "sub"):
            val = self.claims.get(key)
            if val is not None and str(val).isdigit():
                return str(val)
        return None

    def warnings(self) -> list[str]:
        warn = []
        if "authorization" not in self.headers:
            warn.append("chưa có token")
        else:
            exp = self.claims.get("exp")
            if isinstance(exp, (int, float)) and exp < time.time():
                warn.append("token hết hạn")
        if "x-gapo-workspace-id" not in self.headers:
            warn.append("thiếu x-gapo-workspace-id")
        return warn

    def expiry(self) -> str:
        exp = self.claims.get("exp")
        return time.strftime("%d/%m %H:%M", time.localtime(exp)) if isinstance(exp, (int, float)) else "?"

    def label(self) -> str:
        warn = self.warnings()
        return f"{self.name}  (hạn {self.expiry()})" + (f"  ⚠ {', '.join(warn)}" if warn else "")

    def describe(self) -> str:
        auth = self.headers.get("authorization")
        text = (f"token: {mask(auth) if auth else 'KHÔNG CÓ'} (hạn {self.expiry()}) | "
                f"workspace: {self.headers.get('x-gapo-workspace-id', 'KHÔNG CÓ')}")
        warn = self.warnings()
        return text + (f" | ⚠ {', '.join(warn)}" if warn else "")

    def to_dict(self) -> dict:
        return {"author_id": self.author_id, "headers": self.headers}

    def build_session(self):
        s = requests.Session()
        s.headers.update(DEFAULT_HEADERS)
        s.headers.update(self.headers)
        self.session = s

    def comment(self, post_id: str, text: str, timeout: int = 20) -> requests.Response:
        payload = {
            "target_type": "post",
            "target_id": post_id,
            "comment_as": {"author_type": "user", "author_id": self.author_id},
            "data_source": 1,
            "text": text,
        }
        return self.session.post(API_URL, json=payload, timeout=timeout)


# ---------------------------------------------------------------- worker

def describe_response(r: requests.Response) -> str:
    parts = [f"HTTP {r.status_code} {r.reason}"]
    for h in ("x-request-id", "retry-after", "x-ratelimit-remaining"):
        if h in r.headers:
            parts.append(f"{h}: {r.headers[h]}")
    body = r.text.strip().replace("\n", " ")
    parts.append(f"body: {body[:1000] or '(rỗng)'}")
    return " | ".join(parts)


def iter_comments(comments):
    """Lặp vô hạn qua danh sách comment, trả về (vòng, số thứ tự, nội dung)."""
    for rnd in itertools.count(1):
        for i, text in enumerate(comments, 1):
            yield rnd, i, text


def run_job(post_id, comments, accounts, delay, retry, log, stop: threading.Event):
    try:
        _run_job(post_id, comments, accounts, delay, retry, log, stop)
    except Exception:
        log("[LỖI] Lỗi không mong muốn:\n" + traceback.format_exc())


def _run_job(post_id, comments, accounts, delay, retry, log, stop: threading.Event):
    for a in accounts:
        a.alive = True
        a.build_session()
        log(f"[i] {a.name} -> {a.describe()}")

    rotation = itertools.cycle(accounts)
    ok = fail = 0

    for rnd, i, text in iter_comments(comments):
        if stop.is_set():
            log("[x] Đã dừng.")
            break
        if not any(a.alive for a in accounts):
            log("[x] Tất cả acc đã chết, dừng.")
            break
        acc = next(rotation)
        while not acc.alive:
            acc = next(rotation)

        prefix = f"[vòng {rnd} | {i}/{len(comments)}] {acc.name}"

        for attempt in range(retry + 1):
            try:
                r = acc.comment(post_id, text)
            except requests.RequestException as e:
                log(f"{prefix} [LỖI MẠNG] {type(e).__name__}: {e}")
                stop.wait(2)
                continue

            if r.ok:
                ok += 1
                log(f"{prefix} OK: {text}")
            elif r.status_code in (401, 403):
                acc.alive = False
                fail += 1
                log(f"{prefix} [LỖI] {describe_response(r)}")
                log(f"    -> Acc bị loại. {acc.describe()}")
                if r.status_code == 401:
                    log("    -> Token sai/hết hạn: dán lại cURL mới của acc này rồi chạy lại.")
            elif r.status_code == 429 or r.status_code >= 500:
                wait = 10 * (attempt + 1)
                log(f"{prefix} [LỖI] {describe_response(r)} -> đợi {wait}s rồi thử lại")
                stop.wait(wait)
                continue
            else:
                fail += 1
                log(f"{prefix} [LỖI] {describe_response(r)}")
            break
        else:
            fail += 1
            log(f"{prefix} [LỖI] hết {retry + 1} lần thử, bỏ qua comment này")

        stop.wait(random.uniform(*delay))

    log(f"==> Kết thúc. Thành công: {ok}, lỗi: {fail}")


# ---------------------------------------------------------------- GUI

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Gapo Comment Tool")
        self.geometry("820x760")
        self.accounts: list[Account] = []
        self.log_q: queue.Queue = queue.Queue()
        self.stop_evt = threading.Event()
        self.worker: threading.Thread | None = None
        self._build()
        self._load_accounts()
        self.after(100, self._drain_log)

    def report_callback_exception(self, exc, val, tb):
        # Lỗi trong giao diện -> in ra log thay vì chỉ ra terminal
        self.log("[LỖI GIAO DIỆN]\n" + "".join(traceback.format_exception(exc, val, tb)))

    def _build(self):
        pad = {"padx": 8, "pady": 4}

        # Post ID + tuỳ chọn
        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Post ID:").pack(side="left")
        self.post_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.post_var, width=24).pack(side="left", padx=(4, 16))
        ttk.Label(top, text="Delay (s):").pack(side="left")
        self.dmin = tk.DoubleVar(value=3)
        self.dmax = tk.DoubleVar(value=8)
        ttk.Spinbox(top, from_=0, to=600, textvariable=self.dmin, width=5).pack(side="left", padx=2)
        ttk.Label(top, text="-").pack(side="left")
        ttk.Spinbox(top, from_=0, to=600, textvariable=self.dmax, width=5).pack(side="left", padx=2)

        # Tài khoản: ô dán cURL (trái) + danh sách acc (phải)
        abox = ttk.LabelFrame(self, text="Tài khoản — dán cURL / Headers của 1 request comment rồi bấm Thêm acc")
        abox.pack(fill="x", **pad)
        left = ttk.Frame(abox)
        left.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        self.curl_txt = scrolledtext.ScrolledText(left, height=5, width=50, wrap="char")
        self.curl_txt.pack(fill="both", expand=True)
        ttk.Button(left, text="＋ Thêm acc", command=self._add_acc).pack(anchor="e", pady=(4, 0))

        right = ttk.Frame(abox)
        right.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        self.acc_list = tk.Listbox(right, height=5, selectmode="extended")
        self.acc_list.pack(fill="both", expand=True)
        ttk.Button(right, text="Xoá acc chọn", command=self._remove_selected).pack(anchor="e", pady=(4, 0))

        # Nội dung comment
        cbox = ttk.LabelFrame(self, text="Nội dung comment (mỗi dòng 1 comment)")
        cbox.pack(fill="both", expand=True, **pad)
        self.comment_txt = scrolledtext.ScrolledText(cbox, height=10, wrap="word")
        self.comment_txt.pack(fill="both", expand=True, padx=4, pady=4)
        ttk.Button(cbox, text="Mở file .txt", command=self._load_comments).pack(anchor="e", padx=4, pady=(0, 4))

        # Start / Stop
        ctl = ttk.Frame(self)
        ctl.pack(fill="x", **pad)
        self.start_btn = ttk.Button(ctl, text="▶ Bắt đầu", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(ctl, text="■ Dừng", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(ctl, text="Xoá log", command=lambda: self.log_txt.delete("1.0", "end")).pack(side="right")

        # Log
        lbox = ttk.LabelFrame(self, text=f"Log (lưu tại {LOG_FILE.name})")
        lbox.pack(fill="both", expand=True, **pad)
        self.log_txt = scrolledtext.ScrolledText(lbox, height=10, wrap="word")
        self.log_txt.pack(fill="both", expand=True, padx=4, pady=4)
        self.log_txt.tag_config("err", foreground="#d33")
        self.log_txt.tag_config("ok", foreground="#2a2")

    # ---- log
    def log(self, msg: str):
        self.log_q.put(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}")

    def _drain_log(self):
        lines = []
        while not self.log_q.empty():
            line = self.log_q.get()
            lines.append(line)
            tag = "err" if ("[LỖI" in line or "[!]" in line or "    -> " in line) else (
                "ok" if " OK: " in line else "")
            self.log_txt.insert("end", line[11:] + "\n", tag)  # màn hình chỉ hiện giờ
            self.log_txt.see("end")
        if lines:
            try:
                with LOG_FILE.open("a", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
            except OSError:
                pass
        if self.worker and not self.worker.is_alive():
            self.worker = None
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")
        self.after(100, self._drain_log)

    # ---- comment
    def _load_comments(self):
        f = filedialog.askopenfilename(filetypes=[("Text", "*.txt"), ("All", "*.*")])
        if f:
            self.comment_txt.delete("1.0", "end")
            self.comment_txt.insert("1.0", Path(f).read_text(encoding="utf-8"))

    # ---- accounts
    def _refresh_list(self):
        self.acc_list.delete(0, "end")
        for acc in self.accounts:
            self.acc_list.insert("end", acc.label())

    def _save_accounts(self):
        try:
            ACC_FILE.write_text(json.dumps([a.to_dict() for a in self.accounts], indent=2), encoding="utf-8")
        except OSError as e:
            self.log(f"[!] Không lưu được {ACC_FILE.name}: {e}")

    def _load_accounts(self):
        if not ACC_FILE.exists():
            return
        try:
            for d in json.loads(ACC_FILE.read_text(encoding="utf-8")):
                self.accounts.append(Account(d["headers"], d.get("author_id")))
        except Exception as e:
            self.log(f"[!] Không đọc được {ACC_FILE.name}: {type(e).__name__}: {e}")
        self._refresh_list()
        if self.accounts:
            self.log(f"[i] Đã nạp {len(self.accounts)} acc từ {ACC_FILE.name}")

    def _add_acc(self):
        text = self.curl_txt.get("1.0", "end").strip()
        if not text:
            return messagebox.showwarning("Thiếu", "Dán cURL hoặc khung Headers vào ô trước")
        headers, author_id = parse_pasted(text)
        if "authorization" not in headers:
            return messagebox.showwarning("Sai", "Không tìm thấy token (authorization) trong nội dung đã dán")
        acc = Account(headers, author_id)
        if not acc.author_id:
            return messagebox.showwarning("Sai", "Không đọc được author_id từ token")

        for i, old in enumerate(self.accounts):
            if old.author_id == acc.author_id:
                self.accounts[i] = acc
                self.log(f"[*] Cập nhật token {acc.name} -> {acc.describe()}")
                break
        else:
            self.accounts.append(acc)
            self.log(f"[+] Thêm {acc.name} -> {acc.describe()}")
        self.curl_txt.delete("1.0", "end")
        self._refresh_list()
        self._save_accounts()

    def _remove_selected(self):
        for idx in reversed(self.acc_list.curselection()):
            del self.accounts[idx]
        self._refresh_list()
        self._save_accounts()

    # ---- run
    def _start(self):
        post_id = self.post_var.get().strip()
        comments = [l.strip() for l in self.comment_txt.get("1.0", "end").splitlines() if l.strip()]
        if not post_id:
            return messagebox.showwarning("Thiếu", "Nhập Post ID")
        if not comments:
            return messagebox.showwarning("Thiếu", "Nhập nội dung comment")
        if not self.accounts:
            return messagebox.showwarning("Thiếu", "Thêm ít nhất 1 acc")
        try:
            delay = sorted((float(self.dmin.get()), float(self.dmax.get())))
        except (tk.TclError, ValueError):
            return messagebox.showwarning("Sai", "Delay không hợp lệ")

        self.log(f"==> {len(comments)} comment, {len(self.accounts)} acc, post {post_id} (lặp đến khi Dừng)")
        self.stop_evt.clear()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.worker = threading.Thread(
            target=run_job,
            args=(post_id, comments, list(self.accounts), delay, 2, self.log, self.stop_evt),
            daemon=True,
        )
        self.worker.start()

    def _stop(self):
        self.stop_evt.set()
        self.log("Đang dừng...")


if __name__ == "__main__":
    App().mainloop()
