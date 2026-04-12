# 视频项目 README

## 项目名称

Video Tool - 智能视频成片服务

## 简介

Video Tool 是一个独立的视频成片服务，支持：
- 上传视频素材
- 输入新闻稿
- 自动生成配音、字幕
- 智能剪辑合成
- TOS 云存储
- Results 页面展示

## 快速开始

### 本地开发

```bash
# 安装依赖
pip install -r requirements.txt

# 启动服务
python run.py

# 访问
http://localhost:8088
```

### 生产部署

参见 [部署指南](deploy/DEPLOYMENT.md)

## 目录结构

```
video-tool/
├── app/              # Web 应用（Flask）
├── pipeline/         # 视频处理流水线
├── core/             # 核心模块（配置、存储、TOS）
├── config/           # 配置文件
├── deploy/           # 部署资产（systemd、nginx、脚本）
├── scripts/          # 运维脚本
│   ├── dev/          # 开发脚本
│   ├── debug/        # 调试脚本
│   ├── migration/    # 迁移脚本
│   └── ops/          # 运维脚本
├── uploads/          # 上传素材
├── workdir/          # 工作目录（临时文件）
├── outputs/          # 输出视频
├── cache/            # 缓存目录
├── processed_videos/ # 转码缓存
├── logs/             # 日志目录
├── archive/          # 项目档案
├── baselines/        # 阶段基线
├── samples/          # 验收样本
├── docs/             # 文档
├── tests/            # 测试
├── run.py            # 启动入口
├── requirements.txt  # Python 依赖
└── README.md         # 本文件
```

## API 接口

### 上传素材

```bash
POST /api/upload
Files: files (multiple)
Response: {"file_ids": [...], "count": N}
```

### 创建任务

```bash
POST /api/task
Body: {"file_ids": [...], "script": "..."}
Response: {"task_id": "...", "status": "queued"}
```

### 查询任务

```bash
GET /api/task/{task_id}
Response: {"id": "...", "status": "...", "output_path": "..."}
```

### 任务列表

```bash
GET /api/tasks
Response: [...]
```

### 下载视频

```bash
GET /api/download/{task_id}
```

### 健康检查

```bash
GET /api/health
Response: {"status": "ok", "version": "..."}
```

## 配置

配置文件位于 `config/` 目录：

- `config.json` - 主配置文件
- `.env` - 环境变量
- `paths.json` - 路径配置

参见 [配置示例](config/config.example.json)

## 存储策略

### 本地存储

- `uploads/` - 原始素材
- `workdir/` - 任务工作目录
- `outputs/` - 输出视频
- `cache/` - 缓存
- `processed_videos/` - 转码缓存

### TOS 云存储

- `/tasks/{task_id}/` - 任务证据包
- `/raw/` - 原始素材（可选）

参见 [TOS 存储报告](archive/reports/tos_storage_report.md)

## 清理策略

自动清理由 `scripts/cleanup_local.py` 执行：

- 临时文件：24 小时后清理
- 失败任务：7 天后清理
- 转码缓存：LRU，保留 500MB

定时任务：`0 2 * * *`（每天凌晨 2 点）

## 阶段基线

### 阶段 1 基线

- Git 提交：`84c44ef`
- 验收样本：`9a50f4f5-ef0e-41c2-8805-a65894464edc`
- 固定素材：13 个
- 固定新闻稿：5 句 130 字
- 视频时长：43.1 秒

参见 [阶段 1 基线文档](baselines/phase1_baseline.md)

## 开发

### 运行测试

```bash
python -m pytest tests/
```

### 代码风格

遵循 PEP 8，使用 black 格式化。

## 许可证

Proprietary - 内部使用

## 联系方式

视频项目组
