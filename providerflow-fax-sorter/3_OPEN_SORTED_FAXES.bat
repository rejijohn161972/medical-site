@echo off
setlocal
cd /d "%~dp0app"
set "OUT="
for /f "usebackq delims=" %%i in (`python -c "from config_store import load_config; print(load_config()['output_dir'])" 2^>nul`) do set "OUT=%%i"
if not defined OUT set "OUT=C:\FaxOutput"
if not exist "%OUT%" mkdir "%OUT%"
start "" "%OUT%"
