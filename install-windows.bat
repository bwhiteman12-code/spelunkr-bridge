@echo off
rem Double-click to set up Spelunkr Bridge on this PC and start it in the background.
rem   install-windows.bat -Uninstall   removes it again
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-windows.ps1" %*
echo.
pause
