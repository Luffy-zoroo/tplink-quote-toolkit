@echo off
chcp 65001 >nul
cd /d "%~dp0"
title TP-LINK Quote Web

if /i "%~1"=="install" goto install
if /i "%~1"=="uninstall" goto uninstall

REM ---------------- find python ----------------
REM Only accept an interpreter that actually runs code.
set "PY="

REM 1 - WorkBuddy managed runtime, bundles openpyxl
if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

REM 2 - any other WorkBuddy bundled version
if not defined PY for /d %%D in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do if not defined PY if exist "%%D\python.exe" set "PY=%%D\python.exe"

REM 3 - python.org installer default locations
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not defined PY if exist "%ProgramFiles%\Python313\python.exe" set "PY=%ProgramFiles%\Python313\python.exe"
if not defined PY if exist "%ProgramFiles%\Python312\python.exe" set "PY=%ProgramFiles%\Python312\python.exe"

REM 4 - whatever python resolves to on PATH, but it must really run
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 (
    python -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=python"
  )
)

if not defined PY goto nopython

REM ---------------- check deps ----------------
"%PY%" -c "import openpyxl" 2>nul
if not errorlevel 1 goto started

echo   [ ] openpyxl missing, installing... this may take a minute
"%PY%" -m pip install openpyxl
"%PY%" -c "import openpyxl" 2>nul
if errorlevel 1 goto nodep

:started

REM ---------------- start, auto restart ----------------
:loop
cls
echo ============================================================
echo   TP-LINK Quote Web  --  http://127.0.0.1:8000/
echo   Python: %PY%
echo   Address and password are printed below.
echo   To stop: close this window.
echo ============================================================
echo.
"%PY%" -X utf8 webapp.py --port 8000
set RC=%errorlevel%
if "%RC%"=="2" goto portbusy
echo.
echo   [ ] exited with code %RC% -- restarting in 5s
echo       Keep restarting? port is probably taken, change --port 8000 to 8001
ping -n 6 127.0.0.1 >nul
goto loop

:portbusy
echo.
echo   [x] port 8000 already in use -- not restarting.
echo       If a browser window opens fine, an instance is already running.
echo       Otherwise edit this file and change 8000 to 8001.
echo.
pause
exit /b 2

:nopython
echo.
echo   [x] No usable Python found.
echo.
echo       Note: "python" on PATH may be the Microsoft Store placeholder,
echo       a 0-byte file under WindowsApps. It opens the Store instead of
echo       running code, so it cannot be used here.
echo.
echo       Fix - install Python 3.9+ and tick "Add python.exe to PATH":
echo         https://www.python.org/downloads/
echo       Then close this window and double-click start.bat again.
echo.
pause
exit /b 1

:nodep
echo.
echo   [x] openpyxl install failed. See the error above. Common fixes:
echo         "%PY%" -m ensurepip --upgrade
echo         "%PY%" -m pip install openpyxl
echo       No internet? copy the wheel from another PC and run:
echo         "%PY%" -m pip install openpyxl-*.whl
echo.
pause
exit /b 1

REM ---------------- autostart ----------------
:install
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\TPKIT-Quote-Web.lnk"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=New-Object -ComObject WScript.Shell; $k=$s.CreateShortcut('%LNK%'); $k.TargetPath='%~dp0start.bat'; $k.WorkingDirectory='%~dp0'; $k.WindowStyle=7; $k.Description='TPKIT quote web'; $k.Save()"
if exist "%LNK%" goto installed
echo   [x] shortcut creation failed. Manual way: right-click start.bat,
echo       Send to - Desktop, then drag it into the folder from shell:startup
pause
exit /b 1

:installed
echo   [ok] added to startup, minimized, runs after login
pause
exit /b 0

:uninstall
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\TPKIT-Quote-Web.lnk"
if exist "%LNK%" del "%LNK%"
echo   [ok] removed from startup
pause
exit /b 0
