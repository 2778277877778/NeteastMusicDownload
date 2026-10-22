@echo off
chcp 65001 >nul
rem NeteastMusicDownload GUI launcher (no console window)
rem This repo ships source only; python\ is a local venv, interpreter is python\Scripts\pythonw.exe
cd /d "%~dp0"
if not exist "python\Scripts\pythonw.exe" (
    echo [ERROR] runtime not found: python\Scripts\pythonw.exe
    echo Create it first in this folder:
    echo     python -m venv python
    echo     python\Scripts\python.exe -m pip install requests mutagen pillow pycryptodomex tkinterdnd2
    echo.
    pause
    exit /b 1
)
start "" "python\Scripts\pythonw.exe" "ncm_gui.py"
exit /b 0
