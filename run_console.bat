@echo off
rem Same as run.bat but with a console window showing the log (for debugging).
cd /d "%~dp0"
chcp 65001 >nul
if not exist ".venv\.jarvis-installed" (
    echo Сначала запустите install.bat
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m assistant %*
pause
