# 视频项目变更日志

## [1.0.0] - 2026-04-11

### 独立化整理
- 创建独立项目目录结构
- 分离业务代码、配置、部署资产
- 建立 config/ 配置目录，提供配置模板
- 建立 deploy/ 部署目录，包含 systemd、nginx、脚本
- 建立 archive/ 档案目录，整理历史文档
- 建立 baselines/ 基线目录，保存阶段基线
- 建立 scripts/ 脚本目录，分离正式脚本和开发脚本
- 建立 logs/ 日志目录，分类管理日志

### 配置独立化
- 创建 config/config.example.json 配置模板
- 创建 config/.env.example 环境变量模板
- 创建 config/paths.example.json 路径配置模板
- 移除代码中硬编码的 OpenClaw 路径

### 部署资产
- 创建 systemd 服务文件
- 创建 Nginx 配置示例
- 创建启动/停止脚本
- 创建健康检查脚本

### 项目档案
- 整理阶段 1 基线文档
- 整理审计报告
- 整理 TOS 存储报告

---

## [0.9.0] - 2026-04-10

### TOS 存储接入
- 创建 core/tos_storage.py TOS 存储模块
- 任务完成后自动上传证据包到 TOS
- Results 页面支持 TOS URL 访问
- 创建本地清理策略脚本

### 阶段 1 基线固化
- Git 基线：84c44ef
- 验收样本：9a50f4f5-ef0e-41c2-8805-a65894464edc
- 固定素材：13 个
- 固定新闻稿：5 句 130 字

---

## [0.1.0] - 2026-04-06

### 初始版本
- 基础视频成片流程
- Web API 接口
- Results 页面
