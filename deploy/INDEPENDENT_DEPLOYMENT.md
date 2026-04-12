# 视频项目独立部署指南

## 概述

本文档描述如何将视频项目部署为独立服务，不依赖 OpenClaw 环境。

## 前置要求

### 系统要求
- Ubuntu 20.04+ 或 CentOS 7+
- Python 3.11+
- 4GB+ RAM
- 20GB+ 磁盘空间

### 外部依赖
- FFmpeg 4.4+（必须预先安装）
- Nginx（可选，用于反向代理）

## 独立部署步骤

### 1. 创建独立目录

```bash
# 创建项目目录
sudo mkdir -p /opt/video-tool
sudo chown $USER:$USER /opt/video-tool

# 克隆或复制项目
cd /opt/video-tool
git clone <repository-url> .
# 或复制现有项目
# cp -r /path/to/video-tool/* /opt/video-tool/
```

### 2. 创建独立 Python 虚拟环境

```bash
# 创建独立 venv（不依赖 OpenClaw）
python3.11 -m venv .venv

# 激活虚拟环境
source .venv/bin/activate

# 安装依赖
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. 配置环境变量

```bash
# 复制配置模板
cp config/.env.example config/.env

# 编辑配置
vi config/.env
```

**必须配置项**：
```bash
# TOS 配置
TOS_AK=your_access_key_here
TOS_SK=your_secret_key_here
TOS_BUCKET=e23-video
TOS_REGION=cn-beijing

# 服务器配置
VIDEO_TOOL_HOST=0.0.0.0
VIDEO_TOOL_PORT=8088

# FFmpeg 路径（根据实际安装位置）
FFMPEG_PATH=/usr/local/bin/ffmpeg
FFPROBE_PATH=/usr/local/bin/ffprobe

# 日志级别
LOG_LEVEL=INFO

# 部署环境
DEPLOY_ENV=production
```

### 4. 配置 FFmpeg

```bash
# 确认 FFmpeg 已安装
ffmpeg -version

# 确认路径
which ffmpeg

# 更新配置
vi config/config.json
# 修改 ffmpeg_path 和 ffprobe_path
```

### 5. 安装 systemd 服务

```bash
# 复制服务文件
sudo cp deploy/systemd/video-tool.service /etc/systemd/system/

# 重新加载 systemd
sudo systemctl daemon-reload

# 启用服务
sudo systemctl enable video-tool

# 启动服务
sudo systemctl start video-tool

# 检查状态
sudo systemctl status video-tool
```

### 6. 配置 Nginx（可选）

```bash
# 复制 Nginx 配置
sudo cp deploy/nginx/video-tool.conf /etc/nginx/sites-available/

# 修改域名
sudo vi /etc/nginx/sites-available/video-tool.conf
# 修改 server_name 为实际域名

# 启用站点
sudo ln -s /etc/nginx/sites-available/video-tool.conf /etc/nginx/sites-enabled/

# 测试配置
sudo nginx -t

# 重载 Nginx
sudo systemctl reload nginx
```

### 7. 验证部署

```bash
# 健康检查
curl http://localhost:8088/api/health

# 访问 Results 页面
# http://<server-ip>:8088/results
```

## 目录结构

```
/opt/video-tool/
├── .venv/              # 独立 Python 虚拟环境
├── app/                # Web 应用
├── pipeline/           # 视频处理流水线
├── core/               # 核心模块
├── config/             # 配置文件
│   ├── config.json     # 主配置
│   ├── .env            # 环境变量
│   └── paths.json      # 路径配置
├── deploy/             # 部署资产
├── scripts/            # 运维脚本
├── uploads/            # 上传素材
├── workdir/            # 工作目录
├── outputs/            # 输出视频
├── cache/              # 缓存目录
├── processed_videos/   # 转码缓存
├── logs/               # 日志目录
└── run.py              # 启动入口
```

## 日志位置

- 应用日志：`/opt/video-tool/logs/app/`
- 任务日志：`/opt/video-tool/logs/tasks/`
- 清理日志：`/opt/video-tool/logs/cleanup/`
- systemd 日志：`journalctl -u video-tool -f`

## 维护

### 重启服务

```bash
sudo systemctl restart video-tool
```

### 查看日志

```bash
sudo journalctl -u video-tool -n 50 -f
```

### 备份数据

```bash
# 备份 uploads、outputs、workdir、config
tar -czf video-tool-backup-$(date +%Y%m%d).tar.gz \
    uploads/ outputs/ workdir/ config/.env
```

## 升级

```bash
cd /opt/video-tool
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart video-tool
```

## 故障排查

### 服务无法启动

1. 检查日志：`sudo journalctl -u video-tool -n 50`
2. 检查端口：`netstat -tlnp | grep 8088`
3. 检查权限：`ls -la /opt/video-tool/`
4. 检查 Python：`/opt/video-tool/.venv/bin/python --version`

### FFmpeg 错误

1. 确认 FFmpeg 已安装：`ffmpeg -version`
2. 检查路径配置：`cat config/config.json | grep ffmpeg`
3. 检查权限：`ls -la /usr/local/bin/ffmpeg`

### TOS 上传失败

1. 检查 AK/SK 配置：`cat config/.env | grep TOS`
2. 检查网络连接：`curl https://www.volces.com`
3. 查看 TOS 日志：`tail -f logs/app/*.log | grep -i tos`

### 端口冲突

```bash
# 检查端口占用
netstat -tlnp | grep 8088

# 修改端口
vi config/.env
VIDEO_TOOL_PORT=8089

# 重启服务
sudo systemctl restart video-tool
```

## 与 OpenClaw 的区别

| 项目 | OpenClaw 环境 | 独立部署 |
|------|--------------|---------|
| Python 环境 | 共享 .venv | 独立 .venv |
| 配置文件 | 可能依赖 OpenClaw 路径 | 完全独立配置 |
| 服务管理 | 依赖 OpenClaw | 独立 systemd 服务 |
| 日志 | 可能混用 | 独立日志目录 |
| 迁移 | 复杂 | 简单（整目录迁移） |

## 从 OpenClaw 迁移

如果当前项目运行在 OpenClaw 工作区，迁移步骤：

1. 复制项目目录到 /opt/video-tool
2. 创建独立 venv：`python3.11 -m venv .venv`
3. 安装依赖：`pip install -r requirements.txt`
4. 更新配置：修改 config/config.json 中的路径
5. 安装 systemd 服务
6. 测试运行
7. 确认无误后停止 OpenClaw 中的实例
