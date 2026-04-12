@echo off
echo ========================================
echo Windows 上传/预处理助手 v2.0
echo ========================================
echo.

REM 使用内置 Python
set PYTHON_HOME=%~dp0python
set PATH=%PYTHON_HOME%;%PYTHON_HOME%\Scripts;%PATH%
set PYTHONNOUSERSITE=1

echo [启动] 正在启动 GUI 界面...
echo.

REM 运行主程序
"%PYTHON_HOME%\python.exe" main.py

pause
