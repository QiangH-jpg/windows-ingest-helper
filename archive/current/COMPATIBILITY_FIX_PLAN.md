# 特殊成片兼容性排查报告

**排查时间**: 2026-04-12 22:35  
**失败素材数**: 6 条

---

## 一、失败素材特征分析

**失败现象**: ffprobe 返回空 stdout 或 JSON 解析失败

**可能原因**:
1. 编码格式特殊（如 HEVC/H.265）
2. 容器格式不标准（如某些编辑软件自定义封装）
3. 文件损坏或不完整
4. 码率/分辨率异常

---

## 二、兼容性修复方案

### 方案 1: 增加 ffprobe 回退参数

```python
# 主尝试：标准参数
cmd = [FFPROBE, '-v', 'error', '-show_entries', 'stream=width,height,duration', '-show_entries', 'format=filename,size', '-print_format', 'json', video_path]

# 回退尝试：更宽松的参数
cmd_fallback = [FFPROBE, '-v', 'warning', '-show_format', '-show_streams', '-print_format', 'json', video_path]
```

### 方案 2: 增加兼容性警告标记

如果 ffprobe 失败，不直接返回"无法读取"，而是：
- 标记为 `compatibility_warning: true`
- 记录失败原因
- 允许用户手动处理

### 方案 3: 增加文件预检查

在调用 ffprobe 前：
- 检查文件大小（< 1KB 可能是空文件）
- 检查文件扩展名
- 尝试读取文件头

---

## 三、修复目标

**最低目标**:
- 6 条失败素材中，至少 3 条能成功读取
- 剩余 3 条明确标记为"不兼容格式"而非笼统"无法读取"

**理想目标**:
- 6 条全部能读取
- 或明确记录不支持的格式类型

---

**报告生成时间**: 2026-04-12 22:35
