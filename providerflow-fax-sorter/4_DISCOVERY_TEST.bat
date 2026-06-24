@echo off
setlocal
cd /d "%~dp0app"
echo ProviderFlow Discovery Test
echo ===========================
echo This logs in and saves a snapshot of the real page structure.
echo It does NOT process or move any faxes.
echo.
python main.py --discovery --headed
echo.
echo If rows looked wrong, send the folder shown above to whoever maintains this tool.
pause
