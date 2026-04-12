# 缓存目录规范

**规范时间**: 2026-04-11 08:16  
**规范状态**: ✅ **统一**

---

## 目录关系

### 当前状态

| 目录 | 用途 | 大小 | 状态 |
|------|------|------|------|
| `cache/` | 预留缓存目录 | 8KB | 几乎为空 |
| `processed_videos/` | 转码缓存 | 162MB | 实际使用 |

### 问题

两个目录功能重叠，造成混淆。

---

## 统一方案

### 方案选择

**采用方案**: 保留 `processed_videos/`，`cache/` 作为通用缓存目录

**理由**:
1. `processed_videos/` 已有 162MB 实际缓存
2. 代码中多处引用 `processed_videos/`
3. `cache/` 可用于其他缓存（如帧缓存、临时文件）

### 目录规范

```
video-tool/
├── cache/                  # 通用缓存目录
│   ├── frames/             # 帧缓存（可选）
│   ├── temp/               # 临时文件
│   └── ...                 # 其他缓存
│
└── processed_videos/       # 转码缓存（主缓存目录）
    ├── {original_name}__{hash}__{codec}__{resolution}__{fps}fps__{pix_fmt}__{version}.mp4
    └── video_index.json    # 缓存索引
```

### 使用规范

| 缓存类型 | 目录 | 保留策略 |
|----------|------|---------|
| 转码缓存 | processed_videos/ | LRU，保留 500MB |
| 帧缓存 | cache/frames/ | 任务完成后清理 |
| 临时文件 | cache/temp/ | 立即清理 |
| 其他缓存 | cache/ | 按需定义 |

---

## 代码引用

### 主链使用

**文件**: `pipeline/video_cache.py`

```python
# 转码缓存目录
PROCESSED_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'processed_videos')

# 缓存索引
INDEX_FILE = os.path.join(PROCESSED_DIR, 'video_index.json')
```

### 清理策略

**文件**: `scripts/cleanup_local.py`

```python
# 清理转码缓存（LRU）
def cleanup_processed_cache():
    PROCESSED_DIR = './processed_videos'
    MAX_SIZE_MB = 500
    # LRU 删除最旧文件
```

---

## 迁移说明

### 现有缓存

- `processed_videos/` 中的 162MB 缓存保留
- `cache/` 保持为空，作为通用缓存目录

### 未来扩展

如需新增缓存类型，在 `cache/` 下创建子目录：
- `cache/frames/` - 帧缓存
- `cache/thumbnails/` - 缩略图
- `cache/temp/` - 临时文件

---

## 最终结论

# ✅ **通过 - 缓存目录口径统一**

**规范**:
- `processed_videos/` - 转码缓存（主缓存）
- `cache/` - 通用缓存（预留）

**当前主链使用**: `processed_videos/`

---

**规范生成完成** ✅  
**生成时间**: 2026-04-11 08:16
