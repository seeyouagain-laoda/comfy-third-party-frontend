@echo off
rem ============================================================
rem  Comfy Third-Party Frontend launcher (Windows)
rem  Double-click: starts the local backend silently (no black
rem  window because the backend itself uses CREATE_NO_WINDOW),
rem  then opens the UI in your browser.
rem  Pure ASCII on purpose (cmd.exe reads .bat as ANSI).
rem
rem  GOTCHA: inside an "if ( ... )" block a bare ")" in echo text
rem  closes the block early and cmd dies with a syntax error like
rem  "... was unexpected at this time". So every echo line inside a
rem  block below avoids parentheses entirely.
rem ============================================================
setlocal
cd /d "%~dp0"

set "PYW="
if exist "%~dp0python\pythonw.exe" set "PYW=%~dp0python\pythonw.exe"
if not defined PYW if exist "%~dp0python\python.exe" set "PYW=%~dp0python\python.exe"
if not defined PYW if exist "%~dp0venv\Scripts\pythonw.exe" set "PYW=%~dp0venv\Scripts\pythonw.exe"
if not defined PYW if exist "%~dp0venv\Scripts\python.exe" set "PYW=%~dp0venv\Scripts\python.exe"

if not defined PYW (
  for %%P in (pythonw.exe python.exe) do if not defined PYW if exist "%%~$PATH:P" set "PYW=%%~$PATH:P"
)

if not defined PYW (
  echo.
  echo [ERROR] pythonw.exe / python.exe not found.
  echo   - Install Python 3.11+ and re-run; during setup tick the box
  echo     "Add python.exe to PATH", OR
  echo   - unzip a portable Python next to this file as .\python\
  echo.
  pause
  exit /b 1
)

if not exist "%~dp0comfy_studio_launch.py" (
  echo.
  echo [ERROR] comfy_studio_launch.py is missing next to this file.
  echo.
  pause
  exit /b 1
)

start "" "%PYW%" "%~dp0comfy_studio_launch.py" %*
exit /b 0
