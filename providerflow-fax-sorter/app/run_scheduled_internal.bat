@echo off
cd /d "%~dp0"
if not exist "%LOCALAPPDATA%\ProviderFlowFaxSorter" mkdir "%LOCALAPPDATA%\ProviderFlowFaxSorter"
python main.py --scheduled >> "%LOCALAPPDATA%\ProviderFlowFaxSorter\scheduled.log" 2>&1
