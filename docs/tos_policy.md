# 原始素材 TOS 策略规范

**规范时间**: 2026-04-11 08:17  
**规范状态**: ✅ **统一**

---

## 策略口径

### 正式口径

**原始素材上传 TOS**: ✅ **默认启用**

**说明**:
- 每个正式任务创建时，原始素材自动上传到 TOS
- 目录：`/raw/{date}/{file_id}.MP4`
- 上传失败不影响任务执行，但记录失败状态

### 策略演进

| 阶段 | 策略 | 状态 |
|------|------|------|
| 第一轮接入 | 证据包上传（6 类文件） | ✅ 已完成 |
| 第二轮收口 | 原始素材上传 | ✅ 已完成 |
| 后续优化 | 批量上传、并发优化 | 待实施 |

---

## 上传流程

```
任务创建
    ↓
[Step 0] 上传原始素材到 TOS
    ↓
上传结果写入 task['input_files']
    ↓
[Step 1-6] 视频处理流程
    ↓
[Step 7] 上传证据包到 TOS
    ↓
任务完成
```

---

## 任务记录字段

```json
{
  "input_files": [
    {
      "file_id": "fd1b7c10-d6ef-45fa-8bae-a3c31195ea9a",
      "local_path": "/path/to/uploads/fd1b7c10...MP4",
      "tos_key": "raw/20260411/fd1b7c10-d6ef-45fa-8bae-a3c31195ea9a.MP4",
      "tos_url": "https://...",
      "size": 103956972,
      "uploaded": true
    }
  ],
  "tos": {
    "success": true,
    "uploaded": [
      "tasks/{task_id}/task.json",
      "tasks/{task_id}/script.txt",
      "tasks/{task_id}/tts.mp3",
      "tasks/{task_id}/subtitles.srt",
      "tasks/{task_id}/timeline.json",
      "tasks/{task_id}/output.mp4"
    ],
    "failed": [],
    "urls": {...}
  }
}
```

---

## TOS 目录结构

```
e23-video/
├── raw/                    # 原始素材（默认上传）
│   └── 20260411/
│       ├── {file_id}.MP4
│       └── ...
├── tasks/                  # 任务证据包（默认上传）
│   └── {task_id}/
│       ├── task.json
│       ├── script.txt
│       ├── tts.mp3
│       ├── subtitles.srt
│       ├── timeline.json
│       └── output.mp4
├── audit/                  # 审计证据（预留）
└── baselines/              # 基线版本（预留）
```

---

## 上传失败处理

### 原始素材上传失败

**影响**: 不影响任务执行  
**记录**: `task['input_files'][]` 中记录失败  
**处理**: 本地保留原始素材，后续可重试上传

### 证据包上传失败

**影响**: 任务标记为 `tos_verified: false`  
**记录**: `task['tos']['failed']` 中记录失败  
**处理**: 本地保留证据文件，后续可重试上传

---

## 文档统一

### 已更新文档

| 文档 | 更新内容 | 状态 |
|------|---------|------|
| docs/tos_storage_report.md | 原始素材接入 | ✅ |
| deploy/DEPLOYMENT.md | TOS 配置说明 | ✅ |
| README.md | TOS 存储策略 | ✅ |

### 待清理表述

| 文档 | 矛盾表述 | 计划 |
|------|---------|------|
| 历史报告 | "可选上传" | 归档，不修改 |
| 阶段 1 基线 | 未提及原始素材 TOS | 已更新 |

---

## 最终结论

# ✅ **通过 - TOS 策略口径统一**

**正式口径**:
- 原始素材：默认上传 TOS（/raw/ 目录）
- 证据包：默认上传 TOS（/tasks/ 目录）
- 上传失败：记录状态，不影响任务执行

---

**规范生成完成** ✅  
**生成时间**: 2026-04-11 08:17
