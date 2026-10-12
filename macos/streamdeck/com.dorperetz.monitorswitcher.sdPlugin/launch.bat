@echo off
REM Stream Deck on Windows launches this. The plugin folder must stay inside
REM the repo (a symlink is fine) so plugin.py can reach the Windows DDC helpers.
set "PLUGIN_DIR=%~dp0"
where py >nul 2>&1 && (
  py -3 "%PLUGIN_DIR%plugin.py" %*
  exit /b %ERRORLEVEL%
)
where python >nul 2>&1 && (
  python "%PLUGIN_DIR%plugin.py" %*
  exit /b %ERRORLEVEL%
)
echo No Python 3 found for Monitor Input Switcher. >&2
exit /b 1
