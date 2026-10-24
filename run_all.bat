@echo off
chcp 65001 > nul
title StreamChat Launcher
echo ========================================================
echo   Khoi chay toan bo he thong StreamChat (Server + Clients)
echo ========================================================

cd /d "%~dp0"

set "PY_CMD="
where py >nul 2>nul
if %errorlevel%==0 (
    set "PY_CMD=py -3"
) else (
    set "PY_CMD=python"
)

set "VENV_PYTHON="
if exist ".venv\Scripts\python.exe" set "VENV_PYTHON=.venv\Scripts\python.exe"
if exist ".venv\bin\python.exe" set "VENV_PYTHON=.venv\bin\python.exe"

if "%VENV_PYTHON%"=="" (
    echo [Launcher] Chua tim thay moi truong ao .venv. Dang tao .venv...
    %PY_CMD% -m venv .venv
    if errorlevel 1 (
        echo [Launcher] Loi: Khong the tao moi truong ao .venv. Vui long kiem tra lai Python!
        pause
        exit /b 1
    )
    if exist ".venv\Scripts\python.exe" set "VENV_PYTHON=.venv\Scripts\python.exe"
    if exist ".venv\bin\python.exe" set "VENV_PYTHON=.venv\bin\python.exe"
    echo [Launcher] Tao .venv thanh cong!
)

if "%VENV_PYTHON%"=="" (
    echo [Launcher] Loi: Khong tim thay python.exe trong .venv\Scripts hoac .venv\bin!
    pause
    exit /b 1
)

echo [Launcher] Dang kiem tra va cai dat thu vien tu requirements.txt...
"%VENV_PYTHON%" -m pip install --upgrade pip
"%VENV_PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [Launcher] Loi khi cai dat thu vien!
    echo [Launcher] Vui long kiem tra lai cac thu vien can thiet!
    pause
    exit /b 1
)

echo [Launcher] Bat dau chay he thong...
"%VENV_PYTHON%" run_all.py %*
pause
