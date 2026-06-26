@echo off
setlocal
cd /d "%~dp0"
echo ProviderFlow Fax Sorter - First Time Setup
echo ==========================================
echo.
echo Installing Python dependencies (one time)...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Could not install dependencies. Make sure Python 3.10+ is installed
  echo and that "python" works in this window, then run this file again.
  pause
  exit /b 1
)
echo.
python "%~dp0app\setup.py"
echo.
pause
