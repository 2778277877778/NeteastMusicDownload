@echo off
chcp 65001 >nul
rem 网易云音乐工具箱 - 免安装启动器（无控制台窗口）
cd /d "%~dp0"
if not exist "python\pythonw.exe" (
    echo [错误] 找不到内置运行时 python\pythonw.exe
    echo 请确认整个文件夹被完整解压，不要只复制部分文件。
    echo.
    pause
    exit /b 1
)
start "" "python\pythonw.exe" "ncm_gui.py"
exit /b 0
