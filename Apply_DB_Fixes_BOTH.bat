@echo off
setlocal
cd /d "%~dp0"
echo Applying pending database migrations to TEST...
set APP_ENV=TEST
".venv\Scripts\python.exe" -c "from dotenv import load_dotenv; load_dotenv('.env'); from modules.db.migrations.runner import run_pending_migrations; print(run_pending_migrations())"
if errorlevel 1 goto failed
echo.
echo Applying pending database migrations to LIVE...
set APP_ENV=PROD
".venv\Scripts\python.exe" -c "from dotenv import load_dotenv; load_dotenv('.env'); from modules.db.migrations.runner import run_pending_migrations; print(run_pending_migrations())"
if errorlevel 1 goto failed
echo.
echo Done. TEST and LIVE schema are now updated.
pause
exit /b 0
:failed
echo.
echo Migration failed. Share the error shown above.
pause
exit /b 1
