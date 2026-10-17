@echo off
setlocal EnableExtensions
title AwesomeBooks Scraper Pro - Setup
cd /d "%~dp0"

echo.
echo   AwesomeBooks Scraper Pro - one-time setup
echo   ---------------------------------------
echo.

rem --- 1. Find Python 3.10+ and resolve it to a full path (quoted everywhere below) --
set "PYEXE="
where py >nul 2>nul && (
  for /f "delims=" %%p in ('py -3 -c "import sys; print(sys.executable if sys.version_info >= (3,10) else '')" 2^>nul') do set "PYEXE=%%p"
)
if not defined PYEXE (
  where python >nul 2>nul && (
    for /f "delims=" %%p in ('python -c "import sys; print(sys.executable if sys.version_info >= (3,10) else '')" 2^>nul') do set "PYEXE=%%p"
  )
)

rem --- 2. Install Python silently if missing ------------------------------------
if not defined PYEXE (
  echo   Python 3.10+ not found. Downloading Python 3.12 ...
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile \"$env:TEMP\python-installer.exe\""
  if errorlevel 1 goto download_failed
  echo   Installing Python silently ^(about a minute^) ...
  "%TEMP%\python-installer.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0
  del "%TEMP%\python-installer.exe" >nul 2>nul
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if not defined PYEXE goto install_failed
if not exist "%PYEXE%" goto install_failed
echo   Using this Python:
"%PYEXE%" --version

rem --- 3. Virtual environment + dependencies ------------------------------------
if not exist ".venv\Scripts\python.exe" (
  echo   Creating virtual environment ...
  "%PYEXE%" -m venv .venv
  if errorlevel 1 goto venv_failed
)
echo   Installing dependencies ^(needs internet, about a minute^) ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto pip_failed
copy /y requirements.txt ".venv\requirements.installed" >nul
".venv\Scripts\python.exe" -c "import curl_cffi.requests" >nul 2>nul
if errorlevel 1 (
  echo.
  echo   NOTE: the browser-fingerprint library could not load in this folder.
  echo         This usually means the folder path is too long for Windows.
  echo         Move the folder to a short path such as C:\AwesomeBooks Scraper Pro and run setup.bat again.
  echo         The scraper still works without it, using plain requests.
)

echo.
echo   Setup complete. From now on just double-click run.bat to start.
echo.
if /i not "%~1"=="--quiet" pause
exit /b 0

:download_failed
echo   ERROR: downloading Python failed. Install Python 3.12 from https://www.python.org/downloads/ and run setup.bat again.
goto fail
:install_failed
echo   ERROR: Python could not be installed automatically. Install Python 3.12 from https://www.python.org/downloads/
echo          ^(tick "Add python.exe to PATH"^), then run setup.bat again.
goto fail
:venv_failed
echo   ERROR: could not create the .venv folder. Make sure this folder is not read-only, then run setup.bat again.
goto fail
:pip_failed
echo   ERROR: installing dependencies failed. Check the internet connection and run setup.bat again.
goto fail
:fail
if /i not "%~1"=="--quiet" pause
exit /b 1
