@echo off
setlocal
cd /d "%~dp0"
py -3 startup_manage.py enable
echo.
echo Monitor Input Switcher will start automatically when you log in.
pause
