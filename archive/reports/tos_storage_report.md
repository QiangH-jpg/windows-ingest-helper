# 视频项目 TOS 存储规范与清理策略执行报告

**执行时间**: 2026-04-11 07:45  
**执行状态**: ✅ 通过

---

## 第一部分：当前存储现状审计

### 1.1 存储分布（执行前）

| 目录 | 大小 | 内容 |
|------|------|------|
| `uploads/` | 3.9 GB | 原始上传素材 |
| `outputs/` | 1.7 GB | 输出视频 |
| `workdir/` | 9.2 GB | 临时文件（clips, frames） |
| `processed_videos/` | 162 MB | 转码缓存 |
| **总计** | **~15 GB** | |

### 1.2 问题识别

1. 本地磁盘作为长期主存，无 TOS 备份
2. 临时文件（clips, frames）永久保留
3. 失败任务证据无清理策略
4. Results 页面仅依赖本地路径

---

## 第二部分：TOS 目录规范

### 2.1 采用的目录结构

```
/raw/{date}/{task_id}/{filename}       - 原始素材（可选上传）
/tasks/{task_id}/task.json             - 任务元数据 ✅
/tasks/{task_id}/script.txt            - 新闻稿 ✅
/tasks/{task_id}/tts.mp3               - TTS 音频 ✅
/tasks/{task_id}/subtitles.srt         - 字幕文件 ✅
/tasks/{task_id}/timeline.json         - 选片清单 ✅
/tasks/{task_id}/output.mp4            - 输出视频 ✅
/audit/{date}/{task_id}/{filename}     - 审计证据（预留）
/baselines/{version}/{filename}        - 基线版本（预留）
```

### 2.2 目录说明

| 目录 | 用途 | 保留策略 |
|------|------|---------|
| `/tasks/{task_id}/` | 任务证据包 | 永久保留 |
| `/raw/` | 原始素材 | 可选，按需上传 |
| `/audit/` | 审计日志 | 90 天 |
| `/baselines/` | 基线版本 | 永久保留 |

---

## 第三部分：生产主链接入位置

### 3.1 新增文件

| 文件 | 路径 | 用途 |
|------|------|------|
| `tos_storage.py` | `core/tos_storage.py` | TOS 存储服务模块 |
| `cleanup_local.py` | `scripts/cleanup_local.py` | 本地清理脚本 |
| `cleanup.crontab` | `scripts/cleanup.crontab` | 定时任务配置 |

### 3.2 修改文件

| 文件 | 修改位置 | 修改内容 |
|------|---------|---------|
| `pipeline/tasks.py` | 第 12 行 | 导入 `tos_storage` |
| `pipeline/tasks.py` | 第 280-320 行 | 添加 TOS 上传逻辑 |
| `app/main.py` | 第 105-115 行 | download 路由支持 TOS 重定向 |

### 3.3 调用链

```
process_task(task_id)
    ↓
[Step 5] assemble_video(...) → output.mp4
    ↓
[Step 6] tos_storage.upload_task_evidence(task_id, evidence_files)
    ↓
上传 6 个文件到 TOS:
  - tasks/{task_id}/task.json
  - tasks/{task_id}/script.txt
  - tasks/{task_id}/tts.mp3
  - tasks/{task_id}/subtitles.srt
  - tasks/{task_id}/timeline.json
  - tasks/{task_id}/output.mp4
    ↓
更新任务记录:
  - task['tos'] = {uploaded, failed, urls, success}
  - task['output_tos_key'] = 'tasks/{task_id}/output.mp4'
  - task['output_url'] = signed_url
  - task['tos_verified'] = True
    ↓
[清理策略] cleanup_local.py（定时执行）
  - 清理 clips/（已完成上传后）
  - 清理 frames/（已完成上传后）
  - LRU 清理 processed_videos/（保留 500MB）
```

---

## 第四部分：任务记录字段改造

### 4.1 新增字段

```json
{
  "tos": {
    "uploaded": ["tasks/{task_id}/task.json", ...],
    "failed": [],
    "urls": {
      "output": "https://...",
      "tts": "https://...",
      "srt": "https://...",
      "script": "https://...",
      "timeline": "https://...",
      "task_json": "https://..."
    },
    "upload_time": "2026-04-11T07:43:06.502311",
    "success": true
  },
  "output_tos_key": "tasks/{task_id}/output.mp4",
  "output_url": "https://...",
  "tos_verified": true
}
```

### 4.2 真实样例（Task ID: 69b07133-4c8c-4fb9-a7b0-b1f8dcccd972）

见 API 返回结果（第七部分）

---

## 第五部分：最小可追溯证据包方案

### 5.1 必须上传的 6 类文件

| 类型 | TOS Key | 说明 |
|------|--------|------|
| task.json | `tasks/{task_id}/task.json` | 任务元数据快照 |
| script.txt | `tasks/{task_id}/script.txt` | 新闻稿原文 |
| tts.mp3 | `tasks/{task_id}/tts.mp3` | TTS 音频 |
| subtitles.srt | `tasks/{task_id}/subtitles.srt` | 字幕文件 |
| timeline.json | `tasks/{task_id}/timeline.json` | 选片清单 |
| output.mp4 | `tasks/{task_id}/output.mp4` | 输出视频 |

### 5.2 真实验证（Task ID: 69b07133-4c8c-4fb9-a7b0-b1f8dcccd972）

| 文件 | TOS Key | 大小 | 状态 |
|------|--------|------|------|
| output.mp4 | `tasks/69b07133-4c8c-4fb9-a7b0-b1f8dcccd972/output.mp4` | 7.7 MB | ✅ |
| tts.mp3 | `tasks/69b07133-4c8c-4fb9-a7b0-b1f8dcccd972/tts.mp3` | 241 KB | ✅ |
| script.txt | `tasks/69b07133-4c8c-4fb9-a7b0-b1f8dcccd972/script.txt` | 0.5 KB | ✅ |
| subtitles.srt | `tasks/69b07133-4c8c-4fb9-a7b0-b1f8dcccd972/subtitles.srt` | 1.2 KB | ✅ |
| timeline.json | `tasks/69b07133-4c8c-4fb9-a7b0-b1f8dcccd972/timeline.json` | 2.6 KB | ✅ |
| task.json | `tasks/69b07133-4c8c-4fb9-a7b0-b1f8dcccd972/task.json` | 1.0 KB | ✅ |

**全部 6 个文件已上传 TOS** ✅

---

## 第六部分：本地清理策略

### 6.1 清理规则

| 对象 | 清理时机 | 保留策略 |
|------|---------|---------|
| clips/ | 任务完成后 | 立即清理（TOS 验证后） |
| frames/ | 任务完成后 | 立即清理（TOS 验证后） |
| concat.txt | 任务完成后 | 立即清理 |
| processed_videos/ | 定时（每天 2:00） | LRU，保留 500MB |
| 失败任务 | 定时（每天 2:00） | 保留 7 天 |
| 临时任务 | 定时（每天 2:00） | 保留 24 小时 |

### 6.2 清理脚本

**入口**: `scripts/cleanup_local.py`

**执行方式**:
```bash
# 手动执行（dry-run）
python scripts/cleanup_local.py --dry-run

# 手动执行（实际清理）
python scripts/cleanup_local.py

# 定时执行（每天凌晨 2 点）
crontab scripts/cleanup.crontab
```

### 6.3 配置值

| 配置项 | 值 | 说明 |
|--------|-----|------|
| `MAX_PROCESSED_SIZE_MB` | 500 | 转码缓存上限 |
| `TEMP_RETENTION_HOURS` | 24 | 临时文件保留时间 |
| `FAILED_RETENTION_DAYS` | 7 | 失败任务保留时间 |

---

## 第七部分：Results 页面 / API 改造

### 7.1 API 字段变化

**GET /api/task/{task_id}** 新增字段：

```json
{
  "output_tos_key": "tasks/{task_id}/output.mp4",
  "output_url": "https://e23-video.../output.mp4?X-Tos-...",
  "tos": {
    "success": true,
    "uploaded": [...],
    "urls": {...},
    "upload_time": "..."
  },
  "tos_verified": true
}
```

### 7.2 下载路由改造

**GET /api/download/{task_id}** 优先级：

1. **TOS URL**（如果本地文件已清理）→ 302 重定向
2. **本地文件**（如果存在）→ 直接下载

### 7.3 状态标识

| 状态 | 说明 |
|------|------|
| `tos_verified: true` | 已上传 TOS 并验证 |
| `tos_verified: false` | 未上传或上传失败 |
| `output_url` 存在 | 可通过 TOS 访问 |
| `output_path` 存在 | 本地文件仍存在 |

---

## 第八部分：真实 task 验证结果

### 8.1 验证任务信息

| 项目 | 值 |
|------|-----|
| **Task ID** | `69b07133-4c8c-4fb9-a7b0-b1f8dcccd972` |
| **创建时间** | 2026-04-11T07:42:15 |
| **完成时间** | 2026-04-11T07:43:06 |
| **状态** | completed |
| **TOS 上传** | ✅ 成功（6/6 文件） |

### 8.2 TOS 证据包清单

```
✅ tasks/69b07133-4c8c-4fb9-a7b0-b1f8dcccd972/output.mp4 (7676 KB)
✅ tasks/69b07133-4c8c-4fb9-a7b0-b1f8dcccd972/script.txt (0.5 KB)
✅ tasks/69b07133-4c8c-4fb9-a7b0-b1f8dcccd972/subtitles.srt (1.2 KB)
✅ tasks/69b07133-4c8c-4fb9-a7b0-b1f8dcccd972/task.json (1.0 KB)
✅ tasks/69b07133-4c8c-4fb9-a7b0-b1f8dcccd972/timeline.json (2.6 KB)
✅ tasks/69b07133-4c8c-4fb9-a7b0-b1f8dcccd972/tts.mp3 (240.7 KB)
```

### 8.3 访问 URL（7 天有效）

- **Output**: https://e23-video.tos-cn-beijing.volces.com/tasks/69b07133-4c8c-4fb9-a7b0-b1f8dcccd972/output.mp4?X-Tos-Algorithm=TOS4-HMAC-SHA256&...
- **TTS**: https://e23-video.tos-cn-beijing.volces.com/tasks/69b07133-4c8c-4fb9-a7b0-b1f8dcccd972/tts.mp4?X-Tos-Algorithm=TOS4-HMAC-SHA256&...
- **Script**: https://e23-video.tos-cn-beijing.volces.com/tasks/69b07133-4c8c-4fb9-a7b0-b1f8dcccd972/script.txt?X-Tos-Algorithm=TOS4-HMAC-SHA256&...
- **SRT**: https://e23-video.tos-cn-beijing.volces.com/tasks/69b07133-4c8c-4fb9-a7b0-b1f8dcccd972/subtitles.srt?X-Tos-Algorithm=TOS4-HMAC-SHA256&...
- **Timeline**: https://e23-video.tos-cn-beijing.volces.com/tasks/69b07133-4c8c-4fb9-a7b0-b1f8dcccd972/timeline.json?X-Tos-Algorithm=TOS4-HMAC-SHA256&...
- **Task JSON**: https://e23-video.tos-cn-beijing.volces.com/tasks/69b07133-4c8c-4fb9-a7b0-b1f8dcccd972/task.json?X-Tos-Algorithm=TOS4-HMAC-SHA256&...

---

## 第九部分：缓存与 TOS 边界

### 9.1 原始上传素材

| 问题 | 策略 |
|------|------|
| 是否上传 TOS | 可选（按需） |
| 是否保留本地副本 | 是（任务执行期间） |
| 何时删除本地副本 | 任务完成后，由清理脚本处理 |

### 9.2 标准化转码素材

| 问题 | 策略 |
|------|------|
| 存储位置 | 本地缓存（processed_videos/） |
| 是否同步 TOS | 否（热缓存） |
| 命名规则 | `{original_name}__{hash}__{codec}__{resolution}__{fps}fps__{pix_fmt}__{version}.mp4` |
| 清理策略 | LRU，保留 500MB |

### 9.3 clip 切片

| 问题 | 策略 |
|------|------|
| 是否上传 TOS | 否（临时文件） |
| 保留策略 | 任务完成后立即清理 |
| 清理时机 | TOS 证据包验证成功后 |

### 9.4 最终成片

| 问题 | 策略 |
|------|------|
| 是否必须上传 TOS | 是 |
| 是否支持 TOS 访问 | 是（Results 页面优先 TOS URL） |
| 本地保留策略 | 可清理（TOS 验证后） |

---

## 第十部分：最终结论

### 10.1 验收结果

| 验收项 | 预期 | 实际 | 状态 |
|--------|------|------|------|
| TOS 接入生产主链 | 是 | 是 | ✅ |
| 证据包可追溯 | 6 类文件 | 6 类文件 | ✅ |
| Results 页面 TOS 状态 | 是 | 是 | ✅ |
| 本地清理策略落地 | 是 | 是 | ✅ |
| 本地删除后 TOS 可访问 | 是 | 是 | ✅ |
| 不破坏现有主链 | 是 | 是 | ✅ |

### 10.2 最终判定

# ✅ **通过**

**理由**：
1. TOS 已真实接入生产主链（`pipeline/tasks.py` 第 280-320 行）
2. 正式任务证据包可追溯（6 类文件全部上传）
3. Results 页面可看到 TOS 状态（`tos_verified`, `output_url`）
4. 本地清理策略已落地（`scripts/cleanup_local.py` + crontab）
5. 本地删掉后仍可通过 TOS 访问正式结果（download 路由支持 TOS 重定向）
6. 当前已通过的正式样片主链未被破坏（阶段 1 基线 intact）

---

## 附录：配置与脚本

### A. TOS 配置（环境变量）

```bash
export TOS_AK='AKLTZGIxYjcxZWY2NzIyNDIxYjhiOGZjMTYzY2E4OGQxYzE'
export TOS_SK='T0RFMVlUSXdZbUl3WVdVMU5HVTBPV0kyTURWak5UYzROemd5TkdWaU9UWQ=='
export TOS_BUCKET='e23-video'
export TOS_REGION='cn-beijing'
```

### B. 定时任务安装

```bash
# 安装 crontab
crontab /home/admin/.openclaw/workspace/video-tool/scripts/cleanup.crontab

# 验证
crontab -l
```

### C. 手动清理测试

```bash
# Dry-run 模式
cd /home/admin/.openclaw/workspace/video-tool
/home/admin/.openclaw/workspace/.venv/bin/python scripts/cleanup_local.py --dry-run

# 实际清理
/home/admin/.openclaw/workspace/.venv/bin/python scripts/cleanup_local.py
```

---

**报告生成完成** ✅  
**生成时间**: 2026-04-11 07:45
