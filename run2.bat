@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
title ReadAloudMaker v2 (Qwen3)

echo.
echo ============================================
echo   EPUB + audio  -^>  read-aloud EPUB3
echo   Qwen3-ForcedAligner edition (v2)
echo ============================================
echo.

set "PYEXE="
set "PYARGS="

rem 1. Dedicated v2 environment in this folder.
if exist "%~dp0.venv-qwen\Scripts\python.exe" (
    "%~dp0.venv-qwen\Scripts\python.exe" -c "import lxml" >nul 2>&1
    if not errorlevel 1 (
        set "PYEXE=%~dp0.venv-qwen\Scripts\python.exe"
        goto :run
    )
)

rem 2. The Python launcher, pinned to a modern interpreter.
py -3.12 -c "import lxml" >nul 2>&1
if not errorlevel 1 (
    set "PYEXE=py"
    set "PYARGS=-3.12"
    goto :run
)

py -3.11 -c "import lxml" >nul 2>&1
if not errorlevel 1 (
    set "PYEXE=py"
    set "PYARGS=-3.11"
    goto :run
)

rem 3. Whatever "python" is on PATH, as long as it can import lxml.
python -c "import lxml" >nul 2>&1
if not errorlevel 1 (
    set "PYEXE=python"
    goto :run
)

goto :missing

:run
"%PYEXE%" %PYARGS% -m qwen_pipeline %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" goto :failed
echo Finished. The read-aloud EPUB is in the "output" folder.
echo.
pause
exit /b 0

:failed
echo Something went wrong. Read the messages above.
if exist "%~dp0_qwen_work" (
    echo The _qwen_work folder was kept so the generated files can be inspected.
)
echo.
pause
exit /b %RC%

:missing
echo ERROR: no Python with the v2 requirements was found.
echo.
echo   Install Python 3.12, then run these commands in this folder:
echo.
echo       py -3.12 -m venv .venv-qwen
echo       .venv-qwen\Scripts\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cu124
echo       .venv-qwen\Scripts\python.exe -m pip install -r requirements-qwen.txt
echo.
echo   FFmpeg and ffprobe must also be on PATH.
echo   See docs\QWEN3_PIPELINE.md for details.
echo.
pause
exit /b 1
