#!/usr/bin/env python3
"""生成30-60秒长样片 - 最终版"""
import os, sys, json, uuid, asyncio
from datetime import datetime
sys.path.insert(0, '/home/admin/.openclaw/workspace/video-tool')

from core.storage import storage
from pipeline import processor
from pipeline.tts_provider import generate_tts, create_subtitle_srt_from_meta

task_id = str(uuid.uuid4())
print(f"task_id: {task_id}")

materials = [
    '/home/admin/.openclaw/workspace/video-tool/uploads/ef50db2c-6423-440d-9c82-0d5622aefac7.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/1eb45180-814d-48a3-9d23-4639e3d1c42f.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/91faffb2-62f5-4ec4-8755-1d55df66013b.MP4',
]

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

# 交错选片
clips_by_src = {0: [], 1: [], 2: []}
for c in all_clips:
    clips_by_src[c['source_index']].append(c)

selected = []
for round_idx in range(3):
    for src in sorted(clips_by_src.keys()):
        if len(clips_by_src[src]) > round_idx:
            selected.append(clips_by_src[src][round_idx])

sources = list(set(c['source_index'] for c in selected))
print(f"交错选片: {len(selected)} clips, sources: {sources}")

# 保存 timeline
timeline_path = os.path.join(storage.workdir, f"{task_id}_timeline.json")
with open(timeline_path, 'w') as f:
    json.dump({
        'task_id': task_id,
        'selected_clips': [{'clip_path': c['path'], 'source_index': c['source_index'], 'source_file': os.path.basename(materials[c['source_index']]), 'start': c['start'], 'duration': c['duration']} for c in selected],
        'sources_used': sources,
        'selection_mode': 'interleaved',
        'no_loop': True,
        'created_at': datetime.now().isoformat()
    }, f, ensure_ascii=False, indent=2)

# 更长的稿件 - 确保30秒以上
script = """新闻短视频自动成片系统验证测试报告。
本视频使用三段真实素材进行自动成片验证。
第一段素材来自城市生活场景记录，画面内容丰富多彩。
第二段素材呈现自然风光美景，色彩层次分明动人。
第三段素材记录人文活动场景，内容生动自然真实。
系统已完成转码处理、智能切段、配音合成、字幕添加等全流程自动化处理。
本次验证采用交错选片算法，确保多个素材轮流入选。
最终音轨以TTS配音为主，不保留素材原声。
视频时长控制在三十秒至六十秒之间，符合产品需求。
感谢您观看本次自动成片验证测试。"""

tts_path = os.path.join(storage.workdir, f"{task_id}_tts.mp3")
tts_meta_path = os.path.join(storage.workdir, f"{task_id}_tts_meta.json")
tts_meta = asyncio.run(generate_tts(script, tts_path, tts_meta_path))
print(f"TTS时长: {tts_meta['total_duration']}秒")

srt_path = os.path.join(storage.workdir, f"{task_id}.srt")
create_subtitle_srt_from_meta(tts_meta, srt_path)

print(f"\n完成TTS生成，task_id: {task_id}")
print(f"需要手动执行ffmpeg合成视频")