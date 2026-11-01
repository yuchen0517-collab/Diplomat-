@echo off
chcp 65001 >nul
setlocal
set PYTHONIOENCODING=utf-8
set "DEMO_ENGINE_DIR=%~dp0demo1.1"
set "DEMO_HOME_PAGE=observe1.8.html"
set "DEMO_HOST=127.0.0.1"
call "%~dp0start.bat" %*
