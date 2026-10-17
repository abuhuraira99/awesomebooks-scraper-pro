@echo off
setlocal
title AwesomeBooks Scraper Pro
cd /d "%~dp0"

rem First run, or the dependency list changed since the last setup: (re)install quietly.
if not exist ".venv\Scripts\python.exe" goto setup
fc /b requirements.txt ".venv\requirements.installed" >nul 2>nul || goto setup
goto start

:setup
echo   Preparing AwesomeBooks Scraper Pro ...
call "%~dp0setup.bat" --quiet || (pause & exit /b 1)

:start
".venv\Scripts\python.exe" -m awesomebooks_pro --open %*
echo.
echo   The scraper has stopped. You can close this window.
pause
