========================================
Windows 上传/预处理助手 v1.0
========================================

【运行方式】

方式 1：直接运行 Python 脚本（推荐）
  1. 确保已安装 Python 3.8+
  2. 安装依赖：pip install tos
  3. 双击运行 main.py
  或命令行：python main.py

方式 2：打包为 EXE（需在 Windows 上）
  1. 在 Windows 上安装 Python 3.8+
  2. pip install pyinstaller tos
  3. pyinstaller --onefile --windowed --name ingest_helper main.py
  4. 运行 dist/ingest_helper.exe

【功能】
- 扫描本地视频文件
- 转码为 720p proxy
- 检测坏片
- 上传到 TOS
- 生成素材清单

【TOS 配置】
已内置：
- Bucket: e23-video
- Region: cn-beijing
- Endpoint: tos-cn-beijing.volces.com

需要设置环境变量：
- TOS_AK：你的 Access Key
- TOS_SK：你的 Secret Key

【输出目录】
运行后在当前目录生成：
- output/manifest.json    素材清单
- output/proxy/           720p proxy 文件
- output/logs/            处理日志

【版本】v1.0
【日期】2026-04-12
