#!/usr/bin/env python3
"""
验证缓存场景
"""
import sys
sys.path.insert(0, '/home/admin/.openclaw/workspace/video-tool')

from pipeline.video_cache import (
    get_or_create_processed,
    audit_cache,
    CACHE_VERSION, TARGET_CODEC, TARGET_WIDTH, TARGET_HEIGHT, TARGET_FPS, TARGET_PIX_FMT
)

# 测试素材
test_file = '/home/admin/.openclaw/workspace/video-tool/uploads/394A0108.MP4'

print("="*60)
print("场景1: 测试缓存命中")
print("="*60)

# 第一次调用：cache miss
print("\n[第一次调用]")
result1 = get_or_create_processed(test_file)

# 第二次调用：cache hit
print("\n[第二次调用]")
result2 = get_or_create_processed(test_file)

print(f"\n结果:")
print(f"  第一次路径: {result1}")
print(f"  第二次路径: {result2}")
print(f"  是否相同: {result1 == result2}")

# 审计
print("\n" + "="*60)
print("缓存审计")
print("="*60)
audit = audit_cache()
print(f"  总文件: {audit['total_files']}")
print(f"  总大小: {audit['total_size_mb']:.2f} MB")
print(f"  clip污染: {audit['clip_pollution']}")
print(f"  版本不匹配: {audit['version_mismatch']}")