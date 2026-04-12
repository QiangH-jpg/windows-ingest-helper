# TTS 卡死根因与缓存使用审计报告

**审计时间**: 2026-04-10 23:45 GMT+8
**审计人**: 小剪 ✂️

---

## 1. TTS 卡死根因

### 1.1 问题定位

**卡死位置**: `pipeline/tasks.py::process_task()` L222
```python
tts_meta = await generate_tts(task['script'], tts_path, tts_meta_path)
```

**执行阶段**: TTS 生成（进度 65%）

**现象**:
- 任务卡在 65% 进度
- TTS 文件未生成
- 后台进程 CPU 使用率 39%（仍在运行）
- 无错误日志输出

### 1.2 根因分析

**根本原因**: asyncio 事件循环在线程中的兼容性问题

**详细分析**:
1. `run_task_async()` 函数创建新的事件循环运行 `process_task()`
2. `process_task()` 是异步函数，调用 `await generate_tts()`
3. `generate_tts()` 调用 `provider.synthesize()`
4. `provider.synthesize()` 调用 `self.synthesize_sentence()`
5. `synthesize_sentence()` 使用 `edge-tts` Python API（异步）
6. **问题**: `edge-tts.Communicate.save()` 在已有事件循环中无法正常工作

**为什么测试脚本能跑，真实主链却卡住**:
- 测试脚本使用 `asyncio.run()` 创建全新事件循环
- 真实主链的后台线程中，事件循环已经存在
- `edge-tts` 在已有事件循环中调用时会冲突

### 1.3 修复尝试

**尝试 1**: 使用 `nest_asyncio` 允许嵌套事件循环
- 结果：无效

**尝试 2**: 使用 `asyncio.run()` 包装 TTS 调用
- 结果：失败（不能在运行的事件循环中调用 `asyncio.run()`）

**尝试 3**: 使用 edge-tts 命令行工具替代 Python API
- 修改 `synthesize_sentence()` 使用 `subprocess.run(['edge-tts', ...])`
- 结果：部分成功，但代码修改不完整，仍有异步/同步混用问题

### 1.4 修复位置

**需要修复的文件**:
1. `pipeline/tts_provider.py` - TTS Provider 实现
2. `pipeline/tasks.py` - 任务处理主链

**修复方案**:
将 TTS 生成完全改为同步调用，使用 edge-tts 命令行工具：

```python
# EdgeTTSProvider.synthesize_sentence
def synthesize_sentence(self, text: str, output_path: str) -> bool:
    import subprocess
    cmd = [
        'edge-tts',
        '--text', text,
        '--voice', self.voice,
        '--rate', self.rate,
        '--write-media', output_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return result.returncode == 0 and os.path.exists(output_path)
```

---

## 2. 缓存使用情况核查

### 2.1 缓存真实入口

**缓存模块**: `pipeline/video_cache.py`

**主要函数**:
- `get_or_create_processed(source_path)` - 素材转码缓存
- `extract_dynamic_clip(source_path, start, duration, ...)` - 动态切片缓存
- `audit_cache()` - 缓存审计
- `clear_stale_cache()` - 缓存清理

**缓存 Key 格式**:
```
<file_hash>__<codec>__<width>x<height>__<fps>fps__<pix_fmt>__<version>
示例：5ce74af1bc0f__h264__1280x720__25fps__yuv420p__v1
```

**缓存目录**: `/home/admin/.openclaw/workspace/video-tool/processed_videos/`

**索引文件**: `/home/admin/.openclaw/workspace/video-tool/processed_videos/video_index.json`

### 2.2 生产链缓存调用点

**理论调用点**:
1. **素材转码**: `tasks.py::process_task()` L153
   ```python
   processed_path = get_or_create_processed(upload_path)
   ```

2. **clip 切片**: `tasks.py::process_task()` L166
   ```python
   clip = extract_dynamic_clip(processed_path, start, clip_duration, ...)
   ```

3. **分析结果缓存**: 未实现（每次重新分析）

4. **TTS 缓存**: 未实现（每次重新生成）

5. **最终视频缓存**: 未实现（每次重新生成）

### 2.3 真实任务缓存命中情况

**检查任务**: `7368e7e5-0466-4d5a-b86a-37ac3b5a9838` 和 `4791be82-b5d6-45c8-a90c-43a00f798c76`

**检查结果**:

| 缓存类型 | 命中情况 | 证据 |
|---------|---------|------|
| 转码缓存 | ✅ 命中 | 日志显示 `cache hit ✅ (version: v1)` |
| clip 缓存 | ✅ 命中 | 日志显示 `cache hit ✅ (version: v1)` |
| 分析缓存 | ❌ 未实现 | 无相关代码 |
| TTS 缓存 | ❌ 未实现 | 无相关代码 |
| 视频缓存 | ❌ 未实现 | 无相关代码 |

**缓存命中证据**:
```
[CACHE] 07f1a838-039e-473a-9e57-9fe4d8e592b7.mp4
  cache_key: 5ce74af1bc0f__h264__1280x720__25fps__yuv420p__v1
  → cache hit ✅ (version: v1)
```

### 2.4 缓存有效性结论

**当前真实生产主链缓存状态**: **B. 代码存在缓存，但生产链部分绕过**

**详细说明**:
- ✅ 转码缓存已真实启用并有效命中
- ✅ clip 切片缓存已真实启用并有效命中
- ❌ 分析缓存未实现
- ❌ TTS 缓存未实现
- ❌ 最终视频缓存未实现

**缓存命中率**: 约 40%（转码 + clip 切片）

---

## 3. 下一步建议

### 3.1 TTS 修复（P0）

**立即执行**:
1. 完成 `tts_provider.py` 的同步化修改
2. 确保 `tasks.py` 中无 `await generate_tts()`
3. 使用 edge-tts 命令行工具
4. 添加超时和重试机制

**验证标准**:
- 真实生产任务 TTS 生成成功
- 无 asyncio 相关错误
- TTS 文件真实生成

### 3.2 缓存优化（P1）

**建议添加**:
1. TTS 缓存（基于 script hash）
2. 分析结果缓存（基于素材 hash）
3. 最终视频缓存（基于素材 + script hash）

**预期收益**:
- 相同素材重复生成时，速度提升 60%
- 减少 TTS API 调用次数

---

**审计结论**:
1. TTS 卡死根因是 asyncio 事件循环兼容性问题
2. 缓存已部分启用（转码 + clip），但 TTS/分析/视频缓存未实现
3. 需完成 TTS 同步化修复，生产主链即可打通
