@echo off
title Parakh Scanner Firewall Fix
echo ============================================================
echo   Parakh Scanner Firewall Fix
echo ============================================================
echo This must be run as Administrator.
echo It opens TCP port 8502 for mobile scanner access on LAN.
echo.

net session >nul 2>&1
if %errorlevel% neq 0 (
  echo ERROR: Please right-click this file and choose "Run as administrator".
  echo.
  pause
  exit /b 1
)

netsh advfirewall firewall delete rule name="Parakh Scanner 8502" >nul 2>&1
netsh advfirewall firewall add rule name="Parakh Scanner 8502" dir=in action=allow protocol=TCP localport=8502 profile=any

echo.
echo Done. Now restart Start_Scanner_Local.bat and open:
echo   The /lan or /health URL printed by Start_Scanner_Local.bat
echo Example:
echo   http://YOUR-PC-IP:8502/lan
echo   http://YOUR-PC-IP:8502/health
echo.
pause
