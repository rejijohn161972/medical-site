@echo off
setlocal
cd /d "%~dp0app"
echo Running the ProviderFlow Fax Sorter now...
echo.
python main.py --manual
echo.
pause
