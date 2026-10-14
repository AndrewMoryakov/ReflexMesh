@echo off
rem Step 2. One run of the order-note task with the real Jev (OpenRouter), a visible Chrome and the viewer.
rem Turn the VPN on first: without it openrouter.ai answers 403 from this country.
rem The key is asked for (input hidden) unless OPENROUTER_API_KEY is set; it stays in this window only.
rem Extra parameters of run-demo.ps1 pass through, e.g.  2-run-jev.bat -Proxy http://127.0.0.1:7890
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-demo.ps1" %*
set RC=%ERRORLEVEL%
echo.
pause
exit /b %RC%
