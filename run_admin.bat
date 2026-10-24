@echo off
chcp 65001 > nul
title StreamChat Admin Portal Launcher
echo ========================================================
echo   Khoi chay Web Admin StreamChat (Quan ly User)
echo ========================================================

cd /d "%~dp0"

set "VENV_PYTHON="
if exist ".venv\Scripts\python.exe" set "VENV_PYTHON=.venv\Scripts\python.exe"
if exist ".venv\bin\python.exe" set "VENV_PYTHON=.venv\bin\python.exe"

if "%VENV_PYTHON%"=="" (
    set "VENV_PYTHON=python"
)

start "" http://127.0.0.1:9090
"%VENV_PYTHON%" admin_server.py --port 9090 %*
pause
