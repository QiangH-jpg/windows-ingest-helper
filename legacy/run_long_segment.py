#!/usr/bin/env python3
"""长片段优先选片策略 - 升级版"""
import os, sys, json, uuid, asyncio
from datetime import datetime
sys.path.insert(0, '/home/admin/.openclaw/workspace/video-tool')

from core.storage import storage
from pipeline import processor
from pipeline.tts_provider import generate_tts, create_subtitle_srt_from_meta

def build_long_segments(clips, min_clips=2, max_clips=3):
    """
    构建连续片段（长片段优先）
    
    输入：某个素材的clip列表（按时间顺序）
    输出：若干"连续片段组"列表
    
    规则：
    - 优先2-3个连续clip组成segment（8-12秒）
    - 每个clip只属于一个segment
    - 顺序保留
    
    示例：
    输入：[clip0, clip1, clip2, clip3, clip4]
    输出：[[clip0, clip1], [clip2, clip3], [clip4]]
    """
    if not clips:
        return []
    
    segments = []
    i = 0
    while i < len(clips):
        # 计算剩余clip数量
        remaining = len(clips) - i
        
        if remaining >= max_clips:
            # 优先取3个连续clip（15秒）
            segments.append(clips[i:i+max_clips])
            i += max_clips
        elif remaining >= min_clips:
            # 取2个连续clip（10秒）
            segments.append(clips[i:i+min_clips])
            i += min_clips
        else:
            # 剩余1个clip，单独成段（5秒）
            segments.append([clips[i]])
            i += 1
    
    return segments


def select_clips_long_segment_priority(all_clips_by_source, min_sources=3):
    """
    长片段优先选片策略
    
    1) 每个素材先构建long_segments
    2) 按素材交错选择segment
    3) 最终展开为clip列表
    
    输出结构类似：
    A(10s) → B(10s) → C(10s) → A(5s) → B(5s) ...
    """
    # 为每个素材构建segment
    segments_by_source = {}
    for src, clips in all_clips_by_source.items():
        segments = build_long_segments(clips, min_clips=2, max_clips=3)
        segments_by_source[src] = segments
        print(f"  素材{src}: {len(clips)} clips → {len(segments)} segments")
    
    # 获取最大segment数量
    max_segments = max(len(segs) for segs in segments_by_source.values()) if segments_by_source else 0
    
    # 交错选择segment
    selected_clips = []
    segment_info = []  # 用于记录选了哪些segment
    
    for seg_idx in range(max_segments):
        for src in sorted(segments_by_source.keys()):
            segments = segments_by_source[src]
            if seg_idx < len(segments):
                segment = segments[seg_idx]
                # 记录segment信息
                segment_duration = sum(c['duration'] for c in segment)
                segment_info.append({
                    'source_index': src,
                    'segment_index': seg_idx,
                    'clip_count': len(segment),
                    'duration': segment_duration,
                    'clip_starts': [c['start'] for c in segment]
                })
                # 展开segment中的clip
                for clip in segment:
                    selected_clips.append(clip)
    
    return selected_clips, segment_info


# ========== 主流程 ==========
task_id = str(uuid.uuid4())
print(f"task_id: {task_id}")

materials = [
    '/home/admin/.openclaw/workspace/video-tool/uploads/ef50db2c-6423-440d-9c82-0d5622aefac7.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/1eb45180-814d-48a3-9d23-4639e3d1c42f.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/91faffb2-62f5-4ec4-8755-1d55df66013b.MP4',
]

# 转码 + 切段
all_clips = []
for i, path in enumerate(materials):
    transcode_path = os.path.join(storage.workdir, f"{task_id}_transcoded_{i}.mp4")
    print(f"转码素材{i}: {os.path.basename(path)}")
    processor.transcode_to_h264(path, transcode_path)
    clips = processor.extract_clips(transcode_path, clip_duration=5)
    for c in clips:
        c['source_index'] = i
    all_clips.extend(clips[:3])
    print(f"  素材{i} 切得 {len(clips[:3])} 个clip")

# 按素材分组
clips_by_src = {0: [], 1: [], 2: []}
for c in all_clips:
    clips_by_src[c['source_index']].append(c)

print(f"\n=== 长片段优先选片 ===")
selected, segment_info = select_clips_long_segment_priority(clips_by_src)

sources = list(set(c['source_index'] for c in selected))
total_duration = sum(c['duration'] for c in selected)

print(f"\n选片结果:")
print(f"  总clip数: {len(selected)}")
print(f"  总segment数: {len(segment_info)}")
print(f"  预计时长: {total_duration}秒")
print(f"  素材来源: {sources}")

# 显示segment详情
print(f"\nSegment分布:")
for i, seg in enumerate(segment_info):
    print(f"  Segment{i+1}: 素材{seg['source_index']}, {seg['clip_count']} clips, {seg['duration']}秒")

# 保存 timeline
timeline_path = os.path.join(storage.workdir, f"{task_id}_timeline.json")
with open(timeline_path, 'w') as f:
    json.dump({
        'task_id': task_id,
        'selected_clips': [
            {'clip_path': c['path'], 'source_index': c['source_index'], 'source_file': os.path.basename(materials[c['source_index']]), 'start': c['start'], 'duration': c['duration']}
            for c in selected
        ],
        'segment_info': segment_info,
        'sources_used': sources,
        'total_clips': len(selected),
        'total_segments': len(segment_info),
        'clip_duration_total': total_duration,
        'selection_mode': 'long_segment_priority',
        'no_loop': True,
        'created_at': datetime.now().isoformat()
    }, f, ensure_ascii=False, indent=2)

# 稿件
script = """新闻短视频自动成片系统完成验证测试。
本视频采用三段真实素材进行自动化成片验证。
第一段素材记录城市生活场景，画面内容丰富生动自然。
第二段素材呈现自然风光美景，色彩层次分明引人入胜。
第三段素材展示人文活动场景，内容真实富有感染力。
系统已完整实现转码处理、智能切段、配音合成、字幕添加等全流程自动化。
本次验证采用长片段优先选片算法，确保画面连贯性和视觉连续性。
每个素材都贡献了较长的连续镜头，保证了视频的镜头感。
最终音轨以TTS配音为主，不保留素材原声，确保配音清晰可辨。
视频时长控制在三十秒至六十秒之间，完全符合产品设计需求。
感谢您观看本次自动成片验证测试样片。"""

tts_path = os.path.join(storage.workdir, f"{task_id}_tts.mp3")
tts_meta_path = os.path.join(storage.workdir, f"{task_id}_tts_meta.json")
tts_meta = asyncio.run(generate_tts(script, tts_path, tts_meta_path))
print(f"\nTTS时长: {tts_meta['total_duration']}秒")

srt_path = os.path.join(storage.workdir, f"{task_id}.srt")
create_subtitle_srt_from_meta(tts_meta, srt_path)

print(f"\n=== 准备合成 ===")
print(f"task_id: {task_id}")
print(f"clip数量: {len(selected)}")
print(f"segment数量: {len(segment_info)}")
print(f"预计最终时长: ~{total_duration}秒")
print(f"\n需要执行ffmpeg合成视频")
print(f"task_id: {task_id}")