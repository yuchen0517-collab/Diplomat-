@echo off
chcp 65001 >nul
setlocal
set PYTHONIOENCODING=utf-8

set "PY="
if defined DSH_PYTHON if exist "%DSH_PYTHON%" set "PY=%DSH_PYTHON%"
if not defined PY if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set "PY=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not defined PY (
    for /f "delims=" %%v in ('py -3 -c "import sys;print(sys.version.split()[0])" 2^>nul') do set "PY=py -3"
)
if not defined PY (
    for /f "delims=" %%v in ('python -c "import sys;print(sys.version.split()[0])" 2^>nul') do set "PY=python"
)
if not defined PY (
    echo [error] Python not found. See README.md
    pause
    exit /b 9009
)

"%PY%" "%~dp0server.py" --check-key
echo.
pause
