@echo off
rem Starts Jarvis in the system tray (no console window). Logs: data\logs\assistant.log
rem First start of a fresh clone: runs the setup (install.bat) first.
cd /d "%~dp0"
chcp 65001 >nul
if not exist ".venv\.jarvis-installed" (
    echo Джарвис ещё не установлен на этом компьютере. Запускаю установку...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1"
    if errorlevel 1 (
        echo Установка прервалась. Запустите install.bat ещё раз.
        pause
        exit /b 1
    )
)
rem The local model: start Ollama if it is installed but not running (Jarvis works without it, through the cloud).
tasklist /FI "IMAGENAME eq ollama.exe" 2>nul | find /I "ollama.exe" >nul
if errorlevel 1 if exist "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe" start "" "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe"
start "" ".venv\Scripts\pythonw.exe" -m assistant %*
