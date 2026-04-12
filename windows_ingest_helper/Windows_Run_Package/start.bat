@echo off
echo ========================================
echo Windows 上传/预处理助手 v1.0
echo ========================================
echo.

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.8+
    echo 下载地址：https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

echo [检查] Python 已安装
echo.

REM 检查依赖
python -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo [警告] tkinter 未安装，GUI 模式不可用
    echo 将使用命令行模式
    echo.
    goto CLI_MODE
)

python -c "import tos" >nul 2>&1
if errorlevel 1 (
    echo [提示] 正在安装 tos 依赖...
    pip install tos -q
)

echo [启动] 正在启动 GUI 界面...
echo.
python main.py
goto END

:CLI_MODE
echo [提示] 使用命令行模式
echo 用法：python ingest_helper.py --input "C:\Videos" --output "./output"
echo.
pause
exit /b 1

:END
pause
