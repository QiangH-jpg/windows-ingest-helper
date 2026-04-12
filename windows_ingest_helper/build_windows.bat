@echo off
echo ========================================
echo Windows 上传/预处理助手 - 打包脚本
echo ========================================
echo.

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.8+
    echo 下载地址：https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [检查] Python 已安装
echo.

REM 安装依赖
echo [安装] 正在安装依赖...
pip install pyinstaller tos -q
if errorlevel 1 (
    echo [错误] 依赖安装失败
    pause
    exit /b 1
)
echo [完成] 依赖安装完成
echo.

REM 打包 EXE
echo [打包] 正在打包为 EXE...
pyinstaller --onefile --windowed --name ingest_helper --icon=DEFAULT main.py
if errorlevel 1 (
    echo [错误] 打包失败
    pause
    exit /b 1
)
echo [完成] EXE 打包完成
echo.

REM 创建发布目录
echo [创建] 正在创建发布包...
mkdir Windows_Release
copy dist\ingest_helper.exe Windows_Release\
copy upload_to_tos.py Windows_Release\

REM 创建 README
echo Windows 上传/预处理助手 v2.0 > Windows_Release\README.txt
echo ======================================== >> Windows_Release\README.txt
echo. >> Windows_Release\README.txt
echo 【启动方式】 >> Windows_Release\README.txt
echo 双击运行：ingest_helper.exe >> Windows_Release\README.txt
echo. >> Windows_Release\README.txt
echo 【功能】 >> Windows_Release\README.txt
echo - 扫描本地视频文件 >> Windows_Release\README.txt
echo - 转码为 720p proxy >> Windows_Release\README.txt
echo - 检测坏片 >> Windows_Release\README.txt
echo - 上传到 TOS >> Windows_Release\README.txt
echo - 生成素材清单 >> Windows_Release\README.txt
echo. >> Windows_Release\README.txt
echo 【TOS 配置】 >> Windows_Release\README.txt
echo 已内置： >> Windows_Release\README.txt
echo - Bucket: e23-video >> Windows_Release\README.txt
echo - Region: cn-beijing >> Windows_Release\README.txt
echo - Endpoint: tos-cn-beijing.volces.com >> Windows_Release\README.txt
echo. >> Windows_Release\README.txt
echo 需要设置环境变量（如需上传功能）： >> Windows_Release\README.txt
echo set TOS_AK=你的 Access Key >> Windows_Release\README.txt
echo set TOS_SK=你的 Secret Key >> Windows_Release\README.txt
echo. >> Windows_Release\README.txt
echo 【版本】v2.0 >> Windows_Release\README.txt

REM 创建版本文件
echo v2.0 > Windows_Release\version.txt
echo Build: %DATE% >> Windows_Release\version.txt

REM 打包为 ZIP
echo [压缩] 正在创建 ZIP 包...
powershell -Command "Compress-Archive -Path Windows_Release\* -DestinationPath Windows_Ingest_Helper_v2.zip -Force"
if errorlevel 1 (
    echo [警告] ZIP 创建失败，尝试使用 7z...
    7z a -tzip Windows_Ingest_Helper_v2.zip Windows_Release\*
)

echo.
echo ========================================
echo 打包完成！
echo ========================================
echo 产物：Windows_Ingest_Helper_v2.zip
echo 位置：%CD%\Windows_Ingest_Helper_v2.zip
echo.
echo 启动方式：
echo 1. 解压 Windows_Ingest_Helper_v2.zip
echo 2. 双击 Windows_Release\ingest_helper.exe
echo.
pause
