@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" osintX.py
) else (
    python osintX.py
)
if errorlevel 1 pause
