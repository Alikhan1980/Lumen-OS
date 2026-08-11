@echo off
rem Double-click this to start the agent. No typing required.
title Lumen OS
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment missing. Run this once in PowerShell:
    echo    python -m venv .venv
    echo    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m agent
if errorlevel 1 pause
