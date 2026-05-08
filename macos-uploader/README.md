# 元泉智影上传助手 — macOS 客户端

macOS 素材转码 / 上传客户端。只负责本地转码、上传素材、创建 task。
视频生成、剪辑、返修全部走 Web 工作台。

## 客户端定位

```
macOS App (本客户端)          Web 工作台 (现有)
┌──────────────────┐        ┌──────────────────┐
│ 选择素材文件夹    │        │ L2 审查           │
│ 本地 FFmpeg 转码  │   →    │ L3 调度           │
│ TOS presign 上传  │  task  │ TTS 合成          │
│ 创建 task         │  URL   │ 视频渲染          │
│ 打开 Web 工作台   │   →    │ Mini Editor 返修  │
└──────────────────┘        └──────────────────┘
```

## Phase 1 已实现

- [x] Tauri 项目骨架
- [x] 暗色 NLE 风格 UI
- [x] 服务器地址配置 + `/api/health` 连接测试
- [x] 素材目录选择（Tauri dialog）
- [x] 新闻事件 / 视频主题输入框
- [x] 本地配置保存（localStorage）
- [x] "打开 Web 工作台" 按钮
- [x] GitHub Actions macOS 构建 workflow

## Phase 1 未实现

- [ ] FFmpeg 检测 / 内置
- [ ] 本地转码（1280×720 proxy）
- [ ] task/init API 调用
- [ ] TOS presign PUT 上传
- [ ] notify API 调用
- [ ] 上传进度显示
- [ ] 转码进度显示
- [ ] 坏片检测
- [ ] Apple 签名 / 公证

## 本地开发

```bash
cd clients/macos-uploader

# 安装依赖
npm install

# 前端开发（浏览器预览）
npm run dev
# 访问 http://localhost:1420

# Tauri 开发（需要 Rust 环境）
npm run tauri dev
```

### 前提条件

- Node.js 20+
- Rust stable（`rustup install stable`）
- macOS 10.15+（构建 macOS App 需要 macOS 环境）

## GitHub Actions 打包

### 触发方式

1. **手动触发**：仓库 Actions → Build macOS Uploader → Run workflow
2. **Tag 触发**：推送 `v*` 标签

### 产物

- 未签名 `.app` / `.dmg`
- artifact 保留 30 天

### 未签名 macOS App 内部测试

```bash
# 下载 artifact 后解压
# 方式 1：右键 → 打开
# 方式 2：命令行清除隔离标记
xattr -cr "元泉智影上传助手.app"
open "元泉智影上传助手.app"
```

## Phase 2 计划

1. FFmpeg 检测（内置 sidecar 或系统 PATH）
2. 本地转码（参数与 Windows V15.3.2 一致）
3. POST `/api/ui/task/init` 创建 task
4. POST `/api/ui/upload/presign-put` 获取预签名 URL
5. PUT 直传 TOS（不持有 AK/SK）
6. POST `/api/ui/task/{id}/notify` 通知完成
7. 自动打开 Web 工作台

## 技术栈

| 项目 | 值 |
|------|-----|
| 框架 | Tauri 2 |
| 前端 | Vanilla JS + Vite |
| 后端 | Rust (最小胶水) |
| 构建 | GitHub Actions macOS runner |
| 目标体积 | ~15-25 MB |
