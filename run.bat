@echo off
rem Starts Jarvis in the system tray (no console window). Logs: data\logs\assistant.log
cd /d "%~dp0"
chcp 65001 >nul
if not exist ".venv\Scripts\pythonw.exe" (
    echo Сначала выполните: powershell -ExecutionPolicy Bypass -File scripts\install.ps1
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m assistant %*
