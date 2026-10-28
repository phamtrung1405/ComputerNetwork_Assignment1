@echo off
chcp 65001 > nul
title StreamChat Admin Portal Launcher
echo ========================================================
echo   Starting StreamChat Web Admin Portal (User Management)
echo ========================================================

cd /d "%~dp0"

set "VENV_PYTHON="
if exist ".venv\Scripts\python.exe" set "VENV_PYTHON=.venv\Scripts\python.exe"

if "%VENV_PYTHON%"=="" (
    if exist "%LocalAppData%\Programs\Python\Python314\python.exe" (
        set "VENV_PYTHON=%LocalAppData%\Programs\Python\Python314\python.exe"
    ) else if exist "%LocalAppData%\Programs\Python\Python313\python.exe" (
        set "VENV_PYTHON=%LocalAppData%\Programs\Python\Python313\python.exe"
    ) else if exist "%LocalAppData%\Programs\Python\Python312\python.exe" (
        set "VENV_PYTHON=%LocalAppData%\Programs\Python\Python312\python.exe"
    ) else if exist "%LocalAppData%\Programs\Python\Python311\python.exe" (
        set "VENV_PYTHON=%LocalAppData%\Programs\Python\Python311\python.exe"
    ) else if exist "%LocalAppData%\Programs\Python\Python310\python.exe" (
        set "VENV_PYTHON=%LocalAppData%\Programs\Python\Python310\python.exe"
    ) else (
        set "VENV_PYTHON=python"
    )
)

start "" http://127.0.0.1:9090
"%VENV_PYTHON%" admin_server.py --port 9090 %*
pause
