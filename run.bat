@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
title ReadAloudMaker

echo.
echo ============================================
echo   EPUB + audio  ->  read-aloud EPUB3
echo ============================================
echo.

set "PYEXE="
set "PYARGS="

rem Prefer the Python launcher pinned to 3.9 (the version aeneas supports).
py -3.9 -c "import aeneas" >nul 2>&1
if not errorlevel 1 goto :use_py39

rem Fall back to a normal Python 3.9 install in the default location.
if not exist "C:\Program Files\Python39\python.exe" goto :try_python
"C:\Program Files\Python39\python.exe" -c "import aeneas" >nul 2>&1
if errorlevel 1 goto :try_python
set "PYEXE=C:\Program Files\Python39\python.exe"
goto :run

:use_py39
set "PYEXE=py"
set "PYARGS=-3.9"
goto :run

:try_python
python -c "import aeneas" >nul 2>&1
if not errorlevel 1 (
    set "PYEXE=python"
    goto :run
)

python3 -c "import aeneas" >nul 2>&1
if not errorlevel 1 (
    set "PYEXE=python3"
    goto :run
)

goto :missing

:run
"%PYEXE%" %PYARGS% "%~dp0make_readaloud.py" %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" goto :failed
echo Finished. The read-aloud EPUB is in the "output" folder.
echo.
pause
exit /b 0

:failed
echo Something went wrong. Read the messages above.
if exist "%~dp0_readaloud_work" (
    echo The _readaloud_work folder was kept so the generated files can be inspected.
)
echo.
pause
exit /b %RC%

:missing
echo ERROR: no Python with the required packages was found.
echo.
echo   1. Install Python 3.9 from https://www.python.org/downloads/
echo      (tick "Add Python to PATH" during installation).
echo   2. Open a terminal in this folder and run:
echo.
echo         python -m pip install -r requirements.txt
echo.
echo   3. Install FFmpeg and eSpeak NG and add both to PATH
echo      (see README.md for links).
echo.
pause
exit /b 1
