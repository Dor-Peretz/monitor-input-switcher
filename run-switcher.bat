@echo off
setlocal
cd /d "%~dp0"
py -3 monitor_switcher.py run %*
