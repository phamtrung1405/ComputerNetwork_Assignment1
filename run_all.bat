@echo off
chcp 65001 > nul
title StreamChat Launcher
echo ========================================================
echo   Launching full StreamChat system (Server + Clients)
echo ========================================================

cd /d "%~dp0"

set "PY_CMD="
if exist "%LocalAppData%\Programs\Python\Python311\python.exe" (
    set "PY_CMD="%LocalAppData%\Programs\Python\Python311\python.exe""
) else if exist "%LocalAppData%\Programs\Python\Python310\python.exe" (
    set "PY_CMD="%LocalAppData%\Programs\Python\Python310\python.exe""
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        set "PY_CMD=python"
    ) else (
        set "PY_CMD=py -3.11"
    )
)

set "VENV_PYTHON="
if exist ".venv\Scripts\python.exe" set "VENV_PYTHON=.venv\Scripts\python.exe"
if exist ".venv\bin\python.exe" set "VENV_PYTHON=.venv\bin\python.exe"

if "%VENV_PYTHON%"=="" (
    echo [Launcher] Virtual environment .venv not found. Creating .venv...
    %PY_CMD% -m venv .venv
    if errorlevel 1 (
        echo [Launcher] Error: Failed to create virtual environment .venv. Please check Python installation!
        pause
        exit /b 1
    )
    if exist ".venv\Scripts\python.exe" set "VENV_PYTHON=.venv\Scripts\python.exe"
    if exist ".venv\bin\python.exe" set "VENV_PYTHON=.venv\bin\python.exe"
    echo [Launcher] Successfully created .venv!
)

if "%VENV_PYTHON%"=="" (
    echo [Launcher] Error: python.exe not found in .venv\Scripts or .venv\bin!
    pause
    exit /b 1
)

echo [Launcher] Checking and installing dependencies from requirements.txt...
"%VENV_PYTHON%" -m pip install --upgrade pip
"%VENV_PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [Launcher] Error installing dependencies!
    echo [Launcher] Please check required packages!
    pause
    exit /b 1
)

echo [Launcher] Starting system...
"%VENV_PYTHON%" run_all.py %*
pause
