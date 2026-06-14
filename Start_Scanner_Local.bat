@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONDONTWRITEBYTECODE=1
echo Starting Parakh Scanner on this computer...
echo.
echo After it starts, open the shown /lan URL on this PC first.
echo On mobile, use the same Wi-Fi and open the shown http://PC-IP:8502/lan URL.
echo If mobile does not open, run Fix_Scanner_Firewall_Admin.bat as Administrator.
echo.
".venv\Scripts\python.exe" scanner_app.py
pause
