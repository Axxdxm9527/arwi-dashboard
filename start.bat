@echo off
rem ============================================
rem  ARWI 3.0 dashboard launcher (Windows)
rem ============================================
cd /d "%~dp0"
chcp 65001 >nul 2>&1

echo.
echo  ==========================================
echo   ARWI 3.0 asset risk warning dashboard
echo  ==========================================
echo.

rem -- check if port 5000 already serving --
netstat -ano | findstr ":5000" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo  [INFO] Service already running on port 5000.
    echo  [INFO] Open browser: http://127.0.0.1:5000
    start "" http://127.0.0.1:5000
    echo.
    echo  (No need to start again. Close this window.)
    timeout /t 3 >nul
    exit /b 0
)

rem -- ensure dependencies --
python -c "import apscheduler, pandas_datareader, flask" >nul 2>&1
if errorlevel 1 (
    echo  [FIRST RUN] Installing dependencies, please wait...
    python -m pip install -q -r "%~dp0requirements.txt"
)

echo  [START] Launching server, browser will open automatically...
echo  [STOP]  Close this window to stop the service.
echo.
start "" http://127.0.0.1:5000
python app.py
pause
