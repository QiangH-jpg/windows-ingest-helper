# 视频项目迁移指南

## 前提条件

- Python 3.11+
- FFmpeg（通过 LinuxBrew 或系统包管理器安装）
- Node.js（OpenClaw 运行时）

## 迁移步骤

### 1. 克隆项目

```bash
git clone <repository_url> video_tool
cd video_tool
```

### 2. 创建虚拟环境

```bash
python3.11 -m venv .venv
source .venv/bin/activate
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

### 4. 配置环境变量

```bash
cp config/.env.example config/.env
# 编辑 config/.env，修改实际路径
```

### 5. 初始化目录

```bash
python scripts/init_directories.py
```

### 6. 上传素材

将视频素材放入 `uploads/` 目录

### 7. 运行生产链

```bash
python scripts/run_production.py
```

### 8. 查看输出

- 合格成片：`outputs/approved/*.mp4`
- 失败报告：`data/rejection_report.json`
- 任务记录：`tasks/*.json`

## 目录结构

```
video_tool/
├── app/                    # Web 应用
├── v1_materials/          # V1 素材层
├── v2_semantic/           # V2 语义层
├── v3_timeline/           # V3 时序层
├── v4_render/             # V4 渲染层
├── v5_gate/               # V5 校验层
├── config/                # 配置文件
│   ├── .env.example       # 环境配置示例
│   ├── .env               # 环境配置（实际使用）
│   └── settings.py        # 配置管理模块
├── data/                  # 数据目录
├── cache/                 # 缓存目录（可重建）
├── outputs/               # 输出目录
│   ├── raw/               # 原始输出
│   └── approved/          # 合格成片
├── tasks/                 # 任务记录
├── scripts/               # 脚本目录
│   ├── run_production.py  # 唯一主链入口
│   └── init_directories.py # 初始化脚本
├── docs/                  # 文档
├── legacy/                # 旧链归档
└── uploads/               # 上传素材
```

## 配置说明

### 必改配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| APP_ROOT | 应用根目录 | /home/admin/.openclaw/workspace/video-tool |
| UPLOADS_DIR | 素材上传目录 | ${APP_ROOT}/uploads |
| OUTPUT_DIR | 输出目录 | ${APP_ROOT}/outputs |

### 可选配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| MIN_CLIP_DURATION | 最小镜头时长 | 1.5s |
| VIDEO_AUDIO_BUFFER | 视频缓冲时间 | 0.1s |
| SUBTITLE_MATCH_RATE | 字幕匹配率 | 95% |
| TARGET_WIDTH | 视频宽度 | 1280 |
| TARGET_HEIGHT | 视频高度 | 720 |
| TARGET_FPS | 视频帧率 | 25 |

## 校验规则

### 门禁式校验（V5）

以下任一失败则不输出合格成片：

1. **视频时长** ≥ 音频时长 + 0.1s
2. **最短镜头** ≥ 1.5 秒
3. **字幕匹配率** ≥ 95%
4. **无闪屏/异常片段**

## 常见问题

### Q: 缓存目录可以删除吗？

A: 可以。缓存目录 (`cache/`) 视为可重建数据，删除后会自动重建。

### Q: 如何迁移到另一台服务器？

A: 
1. 复制整个项目目录
2. 修改 `config/.env` 中的路径配置
3. 重新运行 `python scripts/init_directories.py`
4. 重新上传素材到 `uploads/`

### Q: 旧脚本还能用吗？

A: 旧脚本已归档到 `legacy/` 目录，不建议使用。请使用 `scripts/run_production.py` 作为唯一入口。

## 版本历史

- **v3.2** (2026-04-08): 隔离式重构，建立分层唯一入口/输出架构
- **v3.1**: 最终校准，使用 ffprobe 真实校验
- **v3.0**: 最终收口，时间轴规则重构
- **v2.x**: 语义选片/节奏优化实验阶段
- **v1.x**: 初始开发阶段
