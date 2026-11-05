@echo off
rem Step 3. The handoff chain through the session service: Jev without the order number hands over,
rem revision 2 supplies it, the LLM role takes at most one step, control returns to Jev.
rem Needs the VPN and the key, as step 2. Extra parameters of run-demo.ps1 pass through.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-demo.ps1" -Mode chain %*
set RC=%ERRORLEVEL%
echo.
pause
exit /b %RC%
