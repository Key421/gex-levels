@echo off
REM Double-click to start the GEX levels page. Installs anything missing, then opens your browser.
cd /d "%~dp0"
title GEX Levels

where py >nul 2>nul && (set "PY=py") || (set "PY=python")

echo Checking Python packages...
%PY% -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo.
    echo Package install failed. Make sure Python is installed from python.org and try again.
    pause
    exit /b 1
)

echo Starting GEX page at http://localhost:8000  ^(close this window to stop^)
%PY% gex_page.py
pause
