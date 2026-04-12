#!/usr/bin/env python3
"""长片段优先选片策略 - 快速验证版（复用已有转码素材）"""
import os, sys, json, uuid
from datetime import datetime

def build_long_segments(clips, min_clips=2, max_clips=3):
    """构建连续片段"""
    if not clips:
        return []
    
    segments = []
    i = 0
    while i < len(clips):
        remaining = len(clips) - i
        if remaining >= max_clips:
            segments.append(clips[i:i+max_clips])
            i += max_clips
        elif remaining >= min_clips:
            segments.append(clips[i:i+min_clips])
            i += min_clips
        else:
            segments.append([clips[i]])
            i += 1
    
    return segments


# 复用之前task的转码文件
OLD_TASK = "73519f84-5bf1-4def-9946-542d9d969f57"
TASK_ID = str(uuid.uuid4())
print(f"新task_id: {TASK_ID}")
print(f"复用转码文件来自: {OLD_TASK}")

WORKDIR = "/home/admin/.openclaw/workspace/video-tool/workdir"
OUTPUTS = "/home/admin/.openclaw/workspace/video-tool/outputs"

# 模拟clip数据（基于之前的timeline）
clips_by_src = {
    0: [
        {'path': f'{WORKDIR}/{OLD_TASK}_transcoded_0.mp4.clip_0.mp4', 'source_index': 0, 'start': 0, 'duration': 5},
        {'path': f'{WORKDIR}/{OLD_TASK}_transcoded_0.mp4.clip_1.mp4', 'source_index': 0, 'start': 5, 'duration': 5},
        {'path': f'{WORKDIR}/{OLD_TASK}_transcoded_0.mp4.clip_2.mp4', 'source_index': 0, 'start': 10, 'duration': 5},
    ],
    1: [
        {'path': f'{WORKDIR}/{OLD_TASK}_transcoded_1.mp4.clip_0.mp4', 'source_index': 1, 'start': 0, 'duration': 5},
        {'path': f'{WORKDIR}/{OLD_TASK}_transcoded_1.mp4.clip_1.mp4', 'source_index': 1, 'start': 5, 'duration': 5},
        {'path': f'{WORKDIR}/{OLD_TASK}_transcoded_1.mp4.clip_2.mp4', 'source_index': 1, 'start': 10, 'duration': 5},
    ],
    2: [
        {'path': f'{WORKDIR}/{OLD_TASK}_transcoded_2.mp4.clip_0.mp4', 'source_index': 2, 'start': 0, 'duration': 5},
        {'path': f'{WORKDIR}/{OLD_TASK}_transcoded_2.mp4.clip_1.mp4', 'source_index': 2, 'start': 5, 'duration': 5},
        {'path': f'{WORKDIR}/{OLD_TASK}_transcoded_2.mp4.clip_2.mp4', 'source_index': 2, 'start': 10, 'duration': 5},
    ],
}

# 构建segment
print(f"\n=== 构建长片段 ===")
segments_by_source = {}
for src, clips in clips_by_src.items():
    segments = build_long_segments(clips, min_clips=2, max_clips=3)
    segments_by_source[src] = segments
    seg_info = [f"{len(s)*5}s" for s in segments]
    print(f"素材{src}: {len(clips)} clips → {len(segments)} segments [{', '.join(seg_info)}]")

# 交错选择segment
print(f"\n=== 交错选片（新策略）===")
selected = []
segment_info = []
max_segments = max(len(segs) for segs in segments_by_source.values())

for seg_idx in range(max_segments):
    for src in sorted(segments_by_source.keys()):
        segments = segments_by_source[src]
        if seg_idx < len(segments):
            segment = segments[seg_idx]
            segment_duration = len(segment) * 5
            segment_info.append({
                'source_index': src,
                'segment_index': seg_idx,
                'clip_count': len(segment),
                'duration': segment_duration
            })
            for clip in segment:
                selected.append(clip)

total_duration = len(selected) * 5
print(f"\n新策略结果:")
print(f"  总clip数: {len(selected)}")
print(f"  总segment数: {len(segment_info)}")
print(f"  预计时长: {total_duration}秒")

# 显示segment分布
print(f"\nSegment分布:")
for i, seg in enumerate(segment_info):
    duration = seg['duration']
    print(f"  Segment{i+1}: 素材{seg['source_index']}, {seg['clip_count']} clips, {duration}秒")

# 保存新timeline
timeline_path = f'{WORKDIR}/{TASK_ID}_timeline.json'
with open(timeline_path, 'w') as f:
    json.dump({
        'task_id': TASK_ID,
        'selected_clips': selected,
        'segment_info': segment_info,
        'total_clips': len(selected),
        'total_segments': len(segment_info),
        'selection_mode': 'long_segment_priority',
        'no_loop': True,
        'created_at': datetime.now().isoformat()
    }, f, ensure_ascii=False, indent=2)

# 创建concat文件
concat_path = f'{OUTPUTS}/{TASK_ID}.mp4.concat.txt'
with open(concat_path, 'w') as f:
    for clip in selected:
        f.write(f"file '{clip['path']}'\n")

print(f"\n=== 对比旧策略 ===")
print(f"旧策略（交错单clip）：")
print(f"  clip数: 9")
print(f"  每个clip: 5秒")
print(f"  结构: A1→B1→C1→A2→B2→C2→A3→B3→C3")

print(f"\n新策略（长片段优先）：")
print(f"  clip数: {len(selected)}")
print(f"  segment数: {len(segment_info)}")
print(f"  结构: 展示连续镜头")

# 计算平均镜头长度
avg_shot_old = 5.0
avg_shot_new = total_duration / len(segment_info) if segment_info else 0

print(f"\n平均镜头长度:")
print(f"  旧策略: {avg_shot_old}秒")
print(f"  新策略: {avg_shot_new:.1f}秒")

print(f"\n新task_id: {TASK_ID}")
print(f"concat文件: {concat_path}")