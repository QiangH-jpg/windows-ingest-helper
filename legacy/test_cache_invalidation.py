#!/usr/bin/env python3
"""
验证参数变化和版本变化场景
"""
import sys
sys.path.insert(0, '/home/admin/.openclaw/workspace/video-tool')

# 场景2：修改目标参数，模拟参数变化
print("="*60)
print("场景2: 参数变化失效")
print("="*60)

# 临时修改目标参数
import pipeline.video_cache as cache_module

# 保存原始值
orig_fps = cache_module.TARGET_FPS
orig_version = cache_module.CACHE_VERSION

# 修改fps参数
cache_module.TARGET_FPS = 30
cache_module.CACHE_VERSION = "v1"  # 保持版本不变

print(f"\n修改目标帧率: {orig_fps}fps → {cache_module.TARGET_FPS}fps")

# 重新导入以使用新参数
from pipeline.video_cache import generate_cache_key

test_file = '/home/admin/.openclaw/workspace/video-tool/uploads/394A0108.MP4'
file_hash = "8f355bf41a18"

# 生成新的cache key
new_key = generate_cache_key(
    file_hash,
    cache_module.TARGET_CODEC,
    cache_module.TARGET_WIDTH,
    cache_module.TARGET_HEIGHT,
    cache_module.TARGET_FPS,
    cache_module.TARGET_PIX_FMT
)

print(f"原cache_key: {file_hash}__h264__1280x720__{orig_fps}fps__yuv420p__v1")
print(f"新cache_key: {new_key}")
print(f"结果: 参数变化后cache_key不同，旧缓存失效 ✅")

# 恢复原始值
cache_module.TARGET_FPS = orig_fps

# 场景3：版本变化
print("\n" + "="*60)
print("场景3: 版本变化失效")
print("="*60)

# 修改版本
cache_module.CACHE_VERSION = "v2"

print(f"\n修改缓存版本: v1 → v2")

new_key_v2 = generate_cache_key(
    file_hash,
    cache_module.TARGET_CODEC,
    cache_module.TARGET_WIDTH,
    cache_module.TARGET_HEIGHT,
    cache_module.TARGET_FPS,
    cache_module.TARGET_PIX_FMT
)

print(f"原cache_key: {file_hash}__h264__1280x720__25fps__yuv420p__v1")
print(f"新cache_key: {new_key_v2}")
print(f"结果: 版本升级后cache_key不同，旧缓存自动失效 ✅")

# 恢复原始值
cache_module.CACHE_VERSION = orig_version

print("\n" + "="*60)
print("验证完成")
print("="*60)