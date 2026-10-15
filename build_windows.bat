@echo off
REM Build GapoComment.exe (1 file duy nhat). Chay file nay tren may Windows da cai Python 3.
cd /d "%~dp0"

python -m venv venv-win || goto :error
call venv-win\Scripts\activate.bat
python -m pip install --upgrade pip
pip install requests pyinstaller || goto :error

pyinstaller --onefile --windowed --clean --name chandiaeoi gapo_comment.py || goto :error

echo.
echo === Xong: dist\chandiaeoi.exe ===
pause
exit /b 0

:error
echo.
echo === Build loi ===
pause
exit /b 1
