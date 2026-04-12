# 回滚版本审计报告 - 第二轮（补证+根因）

**审计时间**: 2026-04-10 21:21 GMT+8
**审计人**: 小剪 ✂️

---

## 1. 旧路径/旧调用清零证据

### 1.1 目录存在性检查

| 目录/文件 | 是否存在 | 文件数 | 状态 |
|----------|---------|--------|------|
| `legacy/` | ✅ 存在 | 67 个.py 文件 | 遗留代码 |
| `v2_semantic/` | ✅ 存在 | 3 个文件 | 未调用模块 |
| `v5_gate/` | ✅ 存在 | 3 个文件 | 未调用模块 |
| `scripts/` | ✅ 存在 | 3 个文件 | 独立脚本 |
| `pipeline/` | ✅ 存在 | 10 个.py 文件 | 生产主链 |
| `app/` | ✅ 存在 | 2 个.py 文件 | 生产主链 |
| `run.py` | ✅ 存在 | 1 个文件 | 主入口 |

### 1.2 代码级检索证据

**检索命令 1**: 检查 legacy 是否被生产链引用
```bash
grep -rn "from legacy\|import legacy\|from \.legacy\|legacy\." --include="*.py" app/ pipeline/ core/ run.py
```
**结果**: ✅ 无命中

**检索命令 2**: 检查 v2_semantic 是否被生产链引用
```bash
grep -rn "from v2_semantic\|import v2_semantic\|v2_semantic\." --include="*.py" app/ pipeline/ core/ run.py
```
**结果**: ✅ 无命中

**检索命令 3**: 检查 v5_gate 是否被生产链引用
```bash
grep -rn "from v5_gate\|import v5_gate\|v5_gate\." --include="*.py" app/ pipeline/ core/ run.py
```
**结果**: ✅ 无命中

**检索命令 4**: 检查 scripts 是否被生产链引用
```bash
grep -rn "from scripts\|import scripts\|scripts\." --include="*.py" app/ pipeline/ core/ run.py
```
**结果**: ✅ 无命中

**检索命令 5**: 精确搜索 AI/语义相关关键词
```bash
grep -rn "SemanticPlanner\|QualityGate\|ENABLE_V2_SEMANTIC\|ENABLE_V5_GATE" --include="*.py" pipeline/ app/ core/ run.py
```
**结果**: ✅ 无命中

### 1.3 生产主链 import 清单

**run.py** (主入口):
```python
from app.main import app, start_background_worker
```

**app/main.py** (Flask 应用):
```python
from core.config import config
from core.storage import storage
from pipeline.tasks import create_task, get_task, list_tasks, process_task
```

**pipeline/tasks.py** (任务处理):
```python
from core.config import config
from pipeline import processor
from pipeline.tts_provider import generate_tts, create_subtitle_srt_from_meta
from pipeline.video_analyzer import create_video_provider, extract_frames_for_task
from core.storage import storage
from pipeline.project_state import load_project_state, validate_script, validate_task, get_state_constraints
```

**pipeline/processor.py** (视频处理):
```python
import os, subprocess, json, edge_tts, sys, threading
from core.config import config
```

### 1.4 最终结论

**✅ 生产主链已做到"0 旧路径残留、0 误触发入口"**

- `legacy/`, `v2_semantic/`, `v5_gate/`, `scripts/` 目录存在但**完全隔离**
- 生产主链 (`run.py` → `app/main.py` → `pipeline/tasks.py`) **无任何旧路径 import**
- 无 `ENABLE_XXX` 功能开关，无旁路决策逻辑
- 旧代码只是"存在"，但**未接入生产链**

---

## 2. 基线视频人工可见核查

### 2.1 视频技术参数

| 参数 | 值 | 说明 |
|------|-----|------|
| 视频流时长 | 15.00s | 硬编码 target_duration |
| 音频流时长 | 7.658s | TTS 实际生成时长 |
| 分辨率 | 640x360 | 原始素材分辨率 |
| 帧率 | 1 fps | **异常：只有 15 帧** |
| 总帧数 | 15 帧 | 每帧 1 秒 |
| 文件大小 | 126KB | 因帧率低导致文件小 |
| 视频编码 | h264 | 正常 |
| 音频编码 | aac | 正常 |

### 2.2 帧级别分析

**15 帧 MD5 校验**:
```
frame_001.png: 3e293f... 14K
frame_002.png: 0d96fb... 15K
frame_003.png: 75b878... 24K
frame_004.png: 7e34e7... 24K
frame_005.png: f11d6e... 24K
frame_006.png: 260c97... 14K
frame_007.png: 37627d... 20K
frame_008.png: 8fdc5d... 20K
frame_009.png: 46bdd5... 3.6K  ← 可能是黑屏/简单画面
frame_010.png: bf2758... 3.4K  ← 可能是黑屏/简单画面
frame_011.png: 7d5dd4... 6.7K
frame_012.png: 3cd3ba... 7.0K
frame_013.png: 58bd78... 7.1K
frame_014.png: e031b4... 7.0K
frame_015.png: b9e37f... 7.1K
```

**结论**:
- ✅ 所有 15 帧 MD5 不同，**无完全重复帧**
- ⚠️ 帧 9-10 文件大小仅 3.4-3.6K，可能是黑屏或简单画面
- ⚠️ 帧率 1fps **严重异常**（正常应为 25-30fps）

### 2.3 结尾方式核查

**ffmpeg showinfo 分析** (帧 10-14):
```
n:10 pts_time:10 checksum:1FC6016E mean:[94 157 102] stdev:[4.8 0.7 0.7]
n:11 pts_time:11 checksum:1A024639 mean:[95 157 104] stdev:[4.8 0.7 0.6]
n:12 pts_time:12 checksum:76A14851 mean:[95 156 105] stdev:[4.8 0.7 0.6]
n:13 pts_time:13 checksum:BEA5B075 mean:[96 156 106] stdev:[4.9 0.7 0.6]
n:14 pts_time:14 checksum:E0333A0D mean:[97 155 107] stdev:[4.7 0.6 0.6]
```

**结论**:
- ✅ 最后 5 帧 checksum 不同，**画面在变化**
- ✅ **非静帧补时结束**，是自然结束
- ⚠️ 但帧率 1fps 导致画面变化非常缓慢

### 2.4 人工可见验证逐项回答

| 检查项 | 结果 | 判断依据 |
|--------|------|---------|
| 1️⃣ 结尾是自然结束还是静帧补时？ | ✅ 自然结束 | 最后 5 帧 checksum 不同，画面在变化 |
| 2️⃣ 是否存在黑屏/静帧拖尾/卡顿？ | ⚠️ 部分存在 | 帧 9-10 仅 3.4K，可能是黑屏；帧率 1fps 导致卡顿感 |
| 3️⃣ 两段素材是否有明显循环或重复？ | ✅ 无明显重复 | 15 帧 MD5 全部不同 |
| 4️⃣ 字幕是否挡画面/过大/位置异常？ | ⚠️ 需实际播放 | SRT 已烧录，但未验证实际效果 |
| 5️⃣ 配音结束后，后半段是否无声空转？ | ❌ 是 | 配音 7.68s，视频 15s，后半段 7.32s 无声 |
| 6️⃣ 属于"可播放"还是"稳定基线样片"？ | ⚠️ 仅"可播放" | 帧率 1fps、无声空转、分辨率低，**不可作为稳定基线样片** |

---

## 3. 三个关键异常的根因审计

### 3.1 异常 1：视频 15.00s vs 配音 7.68s

**现象**: 配音结束后有 7.32 秒无声空转

**入口函数**: `pipeline/tasks.py::process_task()` L199-260

**当前逻辑**:
```python
# tasks.py L199-221
target_duration = config['video']['target_duration_sec']  # = 15
clips_per_source = max(1, target_duration // 5 // max(1, len(task['file_ids'])))
selected_clips = selected_clips[:max_clips]  # max_clips = 15 // 5 = 3

# L260
processor.assemble_video(selected_clips, tts_path, srt_path, output_path, target_duration, keep_concat=True)
```

**assemble_video 逻辑** (`processor.py` L324-377):
- 使用 ffmpeg concat demuxer 拼接所有 clips
- **未根据音频时长裁剪视频**
- 视频时长 = clips 总时长 = 3 × 5s = 15s
- 音频时长 = TTS 实际时长 = 7.68s
- **视频和音频独立处理，未做时长对齐**

**直接原因**: 
1. 选片逻辑按固定 5 秒切片，与 TTS 时长无关
2. `assemble_video` 不根据音频裁剪视频

**问题类型**: 📝 **主链设计缺陷**（非参数配置问题）

---

### 3.2 异常 2：输出 640x360 vs 目标 1280x720

**现象**: 输出分辨率是原始素材分辨率，未升采样

**入口函数**: `test_baseline.py::transcode_to_h264_direct()` L33-44

**当前逻辑**:
```python
# test_baseline.py L33-44
def transcode_to_h264_direct(input_path, output_path):
    cmd = [
        FFMPEG_PATH, '-y',
        '-i', input_path,
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
        '-c:a', 'aac', '-b:a', '128k',
        '-movflags', '+faststart',
        output_path
    ]
    # ❌ 无 -vf scale=1280:720 参数
```

**对比 video_cache.py 正确实现** (`video_cache.py` L246):
```python
# video_cache.py L246（正确实现，但未被调用）
cmd = [
    FFMPEG_PATH, '-y',
    '-i', source_path,
    '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
    '-vf', f'scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=decrease,pad={TARGET_WIDTH}:{TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2',
    # ✅ 有 scale 滤镜
    '-r', str(TARGET_FPS),
    '-pix_fmt', TARGET_PIX_FMT,
    ...
]
```

**配置定义** (`processor.py` L54-55):
```python
VIDEO_OUTPUT_WIDTH = 1280  # 统一宽度 1280x720
VIDEO_OUTPUT_HEIGHT = 720
# ❌ 定义了但未被使用
```

**直接原因**: 
1. 测试脚本绕过 `video_cache.get_or_create_processed()`
2. `transcode_to_h264_direct` 无 scale 滤镜
3. `processor.py` 中的常量定义但未在实际函数中使用

**问题类型**: 📝 **主链设计缺陷**（配置与实现脱节）

---

### 3.3 异常 3：文件仅 126KB

**现象**: 15 秒视频文件异常小

**根因分析**:

| 因素 | 影响 | 说明 |
|------|------|------|
| 帧率 1fps | 🔴 主要因素 | 15 帧 vs 正常 450 帧 (30fps×15s)，减少 96.7% 数据量 |
| 分辨率 640x360 | 🟡 次要因素 | 1280x720 的 1/4 像素量 |
| CRF 23 | 🟢 正常 | 标准质量参数 |
| 音频 7.68s | 🟡 次要因素 | 音频数据量小 |

**帧率 1fps 的根因**:

检查 `extract_clips_direct` 函数 (`test_baseline.py` L46-63):
```python
def extract_clips_direct(input_path, clip_duration=5):
    duration = get_video_duration(input_path)
    i = 0
    while i * clip_duration < duration:
        start = i * clip_duration
        output_path = f"{input_path}.clip_{i}.mp4"
        cmd = [
            FFMPEG_PATH, '-y',
            '-i', input_path,
            '-ss', str(start),
            '-t', str(clip_duration),
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
            '-c:a', 'aac', '-b:a', '128k',
            output_path
        ]
        # ❌ 未指定 -r 帧率参数
```

**对比 video_cache.py 正确实现** (`video_cache.py` L248):
```python
# video_cache.py L248（正确实现）
cmd = [
    FFMPEG_PATH, '-y',
    '-i', source_path,
    ...
    '-r', str(TARGET_FPS),  # ✅ 指定帧率 25fps
    ...
]
```

**直接原因**: 
1. 切片函数未指定 `-r` 帧率参数
2. ffmpeg 默认行为导致输出帧率异常

**问题类型**: 📝 **主链设计缺陷**（关键参数缺失）

---

### 3.4 额外发现：字幕烧录链路核查

**问题**: 字幕是否真正绑定最终导出视频？

**入口函数**: `pipeline/processor.py::assemble_video()` L324-377

**当前逻辑**:
```python
# processor.py L343-349
if subtitle_path and os.path.exists(subtitle_path):
    abs_subtitle_path = os.path.abspath(subtitle_path)
    srt_path_escaped = abs_subtitle_path.replace(':', '\\:').replace("'", "'\\''")
    subtitle_filter = f"subtitles='{srt_path_escaped}':force_style='...'"
    cmd.extend(['-vf', subtitle_filter])  # ✅ 字幕滤镜直接应用到输出
```

**结论**: 
- ✅ 字幕通过 `-vf subtitles=` 滤镜**直接烧录到最终输出视频**
- ✅ 不是中间产物，是最终导出的一部分
- ✅ 链路正确

---

## 4. 当前版本是否真可算"稳定基线"

### 4.1 评估维度

| 维度 | 状态 | 评分 | 说明 |
|------|------|------|------|
| 生产主链完整性 | ✅ 完整 | 5/5 | run.py → app/main.py → tasks.py 链路清晰 |
| AI 残留清理 | ✅ 无残留 | 5/5 | 0 AI 控制，0 旧路径引用 |
| 视频可生成 | ✅ 可生成 | 5/5 | 能输出 mp4 文件 |
| 视频可播放 | ✅ 可播放 | 5/5 | ffprobe 验证通过 |
| 帧率正常 | ❌ 异常 | 0/5 | 1fps vs 目标 30fps |
| 分辨率正常 | ❌ 异常 | 0/5 | 640x360 vs 目标 1280x720 |
| 音画同步 | ❌ 异常 | 0/5 | 配音 7.68s，视频 15s，后半段无声 |
| 文件体积 | ❌ 异常 | 0/5 | 126KB vs 预期 5-20MB |
| 结尾质量 | ⚠️ 合格 | 3/5 | 非静帧结束，但帧率低导致卡顿 |

**总分**: 23/45 = **51%**

### 4.2 最终判定

**⚠️ 当前版本**不可**作为"稳定基线"**

理由：
1. **"能出片" ≠ "稳定基线"**：能生成 mp4 文件只是最低要求
2. **帧率 1fps 是致命缺陷**：视频卡顿，不可用
3. **音画不同步**：配音结束后 7.32 秒无声空转
4. **分辨率未达标**：640x360 vs 1280x720
5. **文件体积异常**：126KB 说明数据量严重不足

**正确定位**: 
- ✅ 可作为"功能验证基线"（验证链路连通性）
- ❌ **不可**作为"生产稳定基线"（质量不达标）

---

## 5. 修复优先级重排

### 5.1 基于本轮审计的重新排序

根据根因分析，**重新排序**如下：

| 优先级 | 问题 | 根因 | 影响 | 修复复杂度 |
|--------|------|------|------|-----------|
| **P0-1** | **帧率 1fps** | 切片函数未指定 `-r` 参数 | 🔴 视频卡顿，不可用 | 低（加 1 个参数） |
| **P0-2** | **音画时长不匹配** | 选片逻辑与 TTS 时长无关 | 🔴 后半段无声空转 | 中（需改逻辑） |
| **P0-3** | **分辨率未升采样** | 转码函数无 scale 滤镜 | 🟡 画质低 | 低（加 1 个参数） |
| **P1-1** | 缓存保护违规 | tasks.py 绕过 video_cache | 🟡 无法复用缓存 | 中（改调用方式） |
| **P1-2** | 配置与实现脱节 | VIDEO_OUTPUT_WIDTH 等常量未使用 | 🟡 维护困难 | 低（统一使用常量） |

### 5.2 与上一轮报告的差异

| 问题 | 上一轮排序 | 本轮排序 | 变化原因 |
|------|-----------|---------|---------|
| 缓存保护违规 | P0 | P1-1 | 发现帧率/时长问题更致命 |
| 时长不匹配 | P2 | P0-2 | 后半段无声是严重体验问题 |
| 分辨率未统一 | P3 | P0-3 | 是基线质量的基本要求 |
| 帧率 1fps | 未发现 | P0-1 | 新增发现，最致命问题 |

### 5.3 修复顺序建议

**第一阶段（P0 - 必须立即修复）**:
1. **修复帧率 1fps** → 在切片函数中添加 `-r 30` 参数
2. **修复音画时长不匹配** → 根据 TTS 时长动态调整视频时长
3. **修复分辨率** → 在转码时添加 scale 滤镜到 1280x720

**第二阶段（P1 - 架构优化）**:
1. 修复缓存保护违规 → tasks.py 使用 video_cache
2. 统一配置使用 → 确保常量被实际使用

---

## 6. 下一步建议

### 6.1 立即执行（P0 修复）

**建议顺序**:
1. 先修**帧率**（影响最大，修复最简单）
2. 再修**时长对齐**（体验问题）
3. 最后修**分辨率**（画质问题）

**原因**:
- 帧率修复只需加 1 个参数，立竿见影
- 时长对齐需要改选片逻辑，需要测试
- 分辨率修复需要加 scale 滤镜，需要验证

### 6.2 验证标准

修复后必须满足：
1. ✅ 帧率 ≥ 25fps（ffprobe 验证）
2. ✅ 视频时长与配音时长误差 < 0.5s
3. ✅ 分辨率 1280x720（ffprobe 验证）
4. ✅ 文件大小 ≥ 5MB（15 秒视频合理体积）
5. ✅ 人工播放验证无卡顿、无声空转

### 6.3 禁止事项

- ❌ 禁止在 P0 修复前引入 AI 选片
- ❌ 禁止在 P0 修复前优化架构
- ❌ 禁止把"能出片"当成验收标准

---

## 7. 附录：关键证据汇总

### 7.1 检索命令汇总

```bash
# 检查旧路径是否被引用
grep -rn "from legacy\|import legacy" --include="*.py" app/ pipeline/ core/ run.py
grep -rn "from v2_semantic\|import v2_semantic" --include="*.py" app/ pipeline/ core/ run.py
grep -rn "from v5_gate\|import v5_gate" --include="*.py" app/ pipeline/ core/ run.py
grep -rn "SemanticPlanner\|QualityGate\|ENABLE_V2_SEMANTIC" --include="*.py" pipeline/ app/

# 验证视频参数
ffprobe -v error -show_entries stream=codec_name,width,height,r_frame_rate,duration,nb_frames baseline_test.mp4

# 抽取所有帧
ffmpeg -y -i baseline_test.mp4 -vf "select=gt(n\,-1)" -vsync 0 frame_%03d.png
```

### 7.2 视频参数对比表

| 参数 | 目标值 | 实际值 | 状态 |
|------|--------|--------|------|
| 帧率 | 30fps | 1fps | ❌ 异常 |
| 分辨率 | 1280x720 | 640x360 | ❌ 异常 |
| 视频时长 | = 配音时长 | 15s vs 7.68s | ❌ 异常 |
| 文件大小 | 5-20MB | 0.12MB | ❌ 异常 |
| 总帧数 | 450 帧 | 15 帧 | ❌ 异常 |

---

**审计报告结束**

**结论**: 当前回滚版本**不可**作为"稳定基线"，需要先修复 P0 问题（帧率、时长对齐、分辨率），再重新验证。
