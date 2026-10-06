@echo off
rem Jarvis setup: Python 3.12, Visual C++ runtime, Ollama, libraries and models (whatever is missing).
rem Safe to run again: it repairs and updates. Options: -JarvisVoice, -NoJarvisVoice, -NoOllama
cd /d "%~dp0"
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
if errorlevel 1 (
    echo.
    echo Установка прервалась. Запустите install.bat ещё раз: уже скачанное повторно не загружается.
    pause
    exit /b 1
)
pause
