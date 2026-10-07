@echo off
chcp 65001 >nul
rem 调试启动：保留控制台，能看到报错
cd /d "%~dp0"
if not exist "python\python.exe" (
    echo [错误] 找不到内置运行时 python\python.exe
    echo 请确认整个文件夹被完整解压，不要只复制部分文件。
    echo.
    pause
    exit /b 1
)
"python\python.exe" "ncm_gui.py"
echo.
echo ---- 程序已退出 ----
pause
