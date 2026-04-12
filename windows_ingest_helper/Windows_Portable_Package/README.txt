========================================
Windows 上传/预处理助手 v2.0
【免安装 Python 版】
========================================

【启动方式】
双击运行：start.bat

【说明】
本版本已内置 Python 运行时，无需额外安装！

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

需要设置环境变量（如需上传功能）：
set TOS_AK=你的 Access Key
set TOS_SK=你的 Secret Key

【输出目录】
运行后在当前目录生成：
- output/manifest.json    素材清单
- output/proxy/           720p proxy 文件
- output/logs/            处理日志

【版本】v2.0（免安装 Python 版）
【日期】2026-04-12
