@echo off
rem Same as run.bat but with a console window showing the log (for debugging).
cd /d "%~dp0"
chcp 65001 >nul
".venv\Scripts\python.exe" -m assistant %*
pause
