@echo off
rem Step 1. The V0.6 demo with a fixed script instead of Jev: no key, no network.
rem Checks Chrome, the demo site, the verifier and the decision viewer. Expected result: completed/verified.
rem Extra parameters of run-demo.ps1 pass through, e.g.  1-check-without-model.bat -Headless
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-demo.ps1" -Provider script %*
set RC=%ERRORLEVEL%
echo.
pause
exit /b %RC%
