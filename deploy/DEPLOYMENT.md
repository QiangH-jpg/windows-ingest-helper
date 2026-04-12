# 视频项目独立部署指南

## 概述

本文档描述如何将视频项目部署到独立服务器。

## 前置要求

### 系统要求
- Ubuntu 20.04+ 或 CentOS 7+
- Python 3.11+
- 4GB+ RAM
- 20GB+ 磁盘空间

### 依赖软件
- FFmpeg 4.4+
- Nginx（可选，用于反向代理）

## 安装步骤

### 1. 克隆项目

```bash
git clone <repository-url> /opt/video-tool
cd /opt/video-tool
```

### 2. 创建虚拟环境

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. 配置环境变量

```bash
cp config/.env.example config/.env
# 编辑 config/.env，填入实际配置
```

### 4. 配置 FFmpeg 路径

```bash
# 确认 FFmpeg 路径
which ffmpeg
# 更新 config/config.json 中的 ffmpeg_path
```

### 5. 配置 TOS（可选）

```bash
# 编辑 config/.env，填入 TOS AK/SK
TOS_AK=your_access_key
TOS_SK=your_secret_key
```

### 6. 安装 systemd 服务

```bash
sudo cp deploy/systemd/video-tool.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable video-tool
```

### 7. 配置 Nginx（可选）

```bash
sudo cp deploy/nginx/video-tool.conf /etc/nginx/sites-available/
sudo ln -s /etc/nginx/sites-available/video-tool.conf /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

### 8. 启动服务

```bash
sudo systemctl start video-tool
sudo systemctl status video-tool
```

## 验证

### 健康检查

```bash
curl http://localhost:8088/api/health
```

### 访问 Results 页面

```
http://<server-ip>:8088/results
```

## 目录说明

```
/opt/video-tool/
├── app/              # Web 应用
├── pipeline/         # 视频处理流水线
├── core/             # 核心模块
├── config/           # 配置文件
├── deploy/           # 部署资产
├── scripts/          # 运维脚本
├── uploads/          # 上传素材
├── workdir/          # 工作目录
├── outputs/          # 输出视频
├── logs/             # 日志
└── run.py            # 启动入口
```

## 日志位置

- 应用日志：`/opt/video-tool/logs/app/`
- 任务日志：`/opt/video-tool/logs/tasks/`
- 清理日志：`/opt/video-tool/logs/cleanup/`
- systemd 日志：`journalctl -u video-tool`

## 维护

### 重启服务

```bash
sudo systemctl restart video-tool
```

### 查看日志

```bash
sudo journalctl -u video-tool -f
```

### 备份数据

```bash
# 备份 uploads、outputs、workdir
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

### FFmpeg 错误

1. 确认 FFmpeg 已安装：`ffmpeg -version`
2. 检查路径配置：`cat config/config.json | grep ffmpeg`

### TOS 上传失败

1. 检查 AK/SK 配置
2. 检查网络连接
3. 查看 TOS 日志：`logs/app/tos.log`
