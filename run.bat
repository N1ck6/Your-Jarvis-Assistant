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
rem "hidden": only the tray icon (without it newer Ollama versions open their window full screen).
tasklist /FI "IMAGENAME eq ollama.exe" 2>nul | find /I "ollama.exe" >nul
if errorlevel 1 if exist "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe" start "" "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe" hidden
rem Jarvis.exe (made or updated here if needed): the same as opening it by hand - "Jarvis" in Task Manager.
".venv\Scripts\python.exe" -c "from assistant.autostart import app_exe; app_exe()" 2>nul
if not exist ".venv\Scripts\Jarvis.exe" (
    start "" ".venv\Scripts\pythonw.exe" -m assistant %*
) else if "%~1"=="" (
    start "" ".venv\Scripts\Jarvis.exe"
) else (
    start "" ".venv\Scripts\Jarvis.exe" -m assistant %*
)
