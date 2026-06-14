@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONDONTWRITEBYTECODE=1

echo.
echo ============================================================
echo   Parakh Scanner - Clean Restart
echo ============================================================
echo.
echo If an old scanner window is open, close it first with Ctrl+C.
echo This window must remain open while mobile scanner is being used.
echo.
echo After startup:
echo   PC reset:     http://127.0.0.1:8502/reset
echo   Mobile reset: open the shown http://PC-IP:8502/reset URL
echo.
echo Expected version: 2026-06-13-staff-report-v11
echo ============================================================
echo.

".venv\Scripts\python.exe" scanner_app.py
pause
