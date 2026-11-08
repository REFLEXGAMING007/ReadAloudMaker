@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
title ReadAloudMaker

rem ---------------------------------------------------------------
rem  One launcher for both pipelines.
rem
rem    run.bat                     web UI on http://127.0.0.1:9459
rem    run.bat --ui [options]      same, explicitly
rem    run.bat --v1 [options]      classic aeneas + syncabook (CLI)
rem    run.bat --v2 [options]      Qwen3-ForcedAligner (CLI)
rem ---------------------------------------------------------------

set "PYEXE="
set "PYARGS="

rem Prefer the Qwen environment: it also has lxml for the UI preview.
if exist "%~dp0.venv-qwen\Scripts\python.exe" (
    set "PYEXE=%~dp0.venv-qwen\Scripts\python.exe"
    goto :run
)

py -3.12 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "PYEXE=py"
    set "PYARGS=-3.12"
    goto :run
)

py -3.11 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "PYEXE=py"
    set "PYARGS=-3.11"
    goto :run
)

py -3.9 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "PYEXE=py"
    set "PYARGS=-3.9"
    goto :run
)

if exist "C:\Program Files\Python39\python.exe" (
    set "PYEXE=C:\Program Files\Python39\python.exe"
    goto :run
)

python -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "PYEXE=python"
    goto :run
)

python3 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "PYEXE=python3"
    goto :run
)

goto :missing

:run
"%PYEXE%" %PYARGS% "%~dp0run.py" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    pause
)
exit /b %RC%

:missing
echo ERROR: no usable Python was found.
echo.
echo   Install Python 3.9 (for the classic pipeline) and/or Python 3.12
echo   (for Qwen3), then set up the environment described in README.md.
echo.
echo   The web UI itself only needs the standard library, so any
echo   Python 3.9 or newer is enough to open it.
echo.
pause
exit /b 1
