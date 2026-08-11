@echo off
rem Double-click this to open the chat window in your browser.
title Lumen OS - keep this window open
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment missing. Run this once in PowerShell:
    echo    python -m venv .venv
    echo    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m agent --web
if errorlevel 1 pause
