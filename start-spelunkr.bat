@echo off
rem Runs Spelunkr Bridge in this window so you can watch the log (finds Jellyfin's ffmpeg on its own).
rem Close the window to stop it. For running in the background at every login, use install-windows.bat.
cd /d "%~dp0"
python spelunkr2jellyfin.py --host 127.0.0.1 --port 8765 %*
pause
