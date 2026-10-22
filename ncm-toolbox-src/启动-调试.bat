@echo off
chcp 65001 >nul
rem NeteastMusicDownload GUI launcher - debug mode, keeps console to show errors
cd /d "%~dp0"
if not exist "python\Scripts\python.exe" (
    echo [ERROR] runtime not found: python\Scripts\python.exe
    echo Create it first in this folder:
    echo     python -m venv python
    echo     python\Scripts\python.exe -m pip install requests mutagen pillow pycryptodomex tkinterdnd2
    echo.
    pause
    exit /b 1
)
"python\Scripts\python.exe" "ncm_gui.py"
echo.
echo ---- program exited ----
pause
