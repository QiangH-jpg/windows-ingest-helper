#!/usr/bin/env python3
"""生成30-60秒长样片 - 音轨纠偏 + 禁止轮播版"""
import os, sys, json, uuid, asyncio
from datetime import datetime
sys.path.insert(0, '/home/admin/.openclaw/workspace/video-tool')

from core.storage import storage
from pipeline import processor
from pipeline.tts_provider import generate_tts, create_subtitle_srt_from_meta

def run_fixed_video():
    task_id = str(uuid.uuid4())
    print(f"task_id: {task_id}")
    
    # 3个真实素材（时长足够，不重复）
    materials = [
        '/home/admin/.openclaw/workspace/video-tool/uploads/ef50db2c-6423-440d-9c82-0d5622aefac7.MP4',  # 40秒
        '/home/admin/.openclaw/workspace/video-tool/uploads/1eb45180-814d-48a3-9d23-4639e3d1c42f.MP4',  # 22秒
        '/home/admin/.openclaw/workspace/video-tool/uploads/91faffb2-62f5-4ec4-8755-1d55df66013b.MP4',  # 17秒
    ]
    
    # 转码 + 切段
    all_clips = []
    clips_by_source = {}
    
    for i, path in enumerate(materials):
        transcode_path = os.path.join(storage.workdir, f"{task_id}_transcoded_{i}.mp4")
        print(f"转码素材{i}: {os.path.basename(path)}")
        processor.transcode_to_h264(path, transcode_path)
        
        clips = processor.extract_clips(transcode_path, clip_duration=5)
        for c in clips:
            c['source_index'] = i
            clip_key = f"{i}_{c['start']}"  # 唯一标识
            if clip_key not in clips_by_source:
                clips_by_source[clip_key] = c
        all_clips.extend(clips[:3])
        print(f"  素材{i} 切得 {len(clips[:3])} 个clip")
    
    # 去重：同一素材同一时间段只选一次
    unique_clips = []
    seen = set()
    for c in all_clips:
        key = f"{c['source_index']}_{c['start']}"
        if key not in seen:
            seen.add(key)
            unique_clips.append(c)
    
    # 交错选片：每个素材轮流取
    clips_by_src = {0: [], 1: [], 2: []}
    for c in unique_clips:
        clips_by_src[c['source_index']].append(c)
    
    selected = []
    max_rounds = max(len(v) for v in clips_by_src.values())
    for round_idx in range(max_rounds):
        for src in sorted(clips_by_src.keys()):
            if len(clips_by_src[src]) > round_idx:
                selected.append(clips_by_src[src][round_idx])
    
    # 确保不重复：clip_path唯一
    final_selected = []
    seen_paths = set()
    for c in selected:
        if c['path'] not in seen_paths:
            seen_paths.add(c['path'])
            final_selected.append(c)
    
    sources = list(set(c['source_index'] for c in final_selected))
    total_duration = len(final_selected) * 5
    print(f"交错选片(去重): {len(final_selected)} clips, sources: {sources}")
    print(f"预计时长: {total_duration}秒")
    
    # 保存 timeline
    timeline_path = os.path.join(storage.workdir, f"{task_id}_timeline.json")
    timeline_data = {
        'task_id': task_id,
        'selected_clips': [
            {
                'clip_path': c['path'],
                'source_index': c['source_index'],
                'source_file': os.path.basename(materials[c['source_index']]),
                'start': c['start'],
                'duration': c['duration']
            }
            for c in final_selected
        ],
        'sources_used': sources,
        'selection_mode': 'interleaved_unique',
        'total_clips': len(final_selected),
        'estimated_duration': total_duration,
        'no_loop': True,
        'created_at': datetime.now().isoformat()
    }
    with open(timeline_path, 'w') as f:
        json.dump(timeline_data, f, ensure_ascii=False, indent=2)
    
    # TTS - 生成适合时长的稿件
    script = f"""新闻短视频自动成片系统验证。
本视频使用{len(sources)}段真实素材，通过交错选片算法拼接。
每段素材贡献不同的画面内容，确保视觉多样性。
系统已完成转码、切段、配音、字幕合成等全流程自动化。
这是音轨纠偏后的验证版本，最终音轨以TTS为主。
感谢观看。"""
    
    tts_path = os.path.join(storage.workdir, f"{task_id}_tts.mp3")
    tts_meta_path = os.path.join(storage.workdir, f"{task_id}_tts_meta.json")
    tts_meta = asyncio.run(generate_tts(script, tts_path, tts_meta_path))
    print(f"TTS时长: {tts_meta['total_duration']}秒")
    
    # SRT
    srt_path = os.path.join(storage.workdir, f"{task_id}.srt")
    create_subtitle_srt_from_meta(tts_meta, srt_path)
    
    # 合成视频 - 不设置target_duration，只用shortest
    output_path = storage.get_output_path(task_id)
    
    # 手动调用assemble_video，但不传target_duration
    # 或者直接用ffmpeg
    FONT_PATH = '/usr/share/fonts/wqy-microhei/wqy-microhei.ttc'
    concat_file = output_path + '.concat.txt'
    with open(concat_file, 'w') as f:
        for clip in final_selected:
            f.write(f"file '{clip['path']}'\n")
    
    # 构建drawtext滤镜
    drawtext_filter = processor.build_drawtext_filter(srt_path, FONT_PATH)
    
    # ffmpeg命令 - 明确只用TTS音轨，不用-t参数
    import subprocess
    cmd = [
        '/home/linuxbrew/.linuxbrew/bin/ffmpeg', '-y',
        '-f', 'concat', '-safe', '0', '-i', concat_file,
        '-i', tts_path,
        '-map', '0:v:0',  # 只取视频流
        '-map', '1:a:0',  # 只取TTS音频
    ]
    
    if drawtext_filter:
        cmd.extend(['-vf', drawtext_filter])
    
    cmd.extend([
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
        '-c:a', 'aac', '-b:a', '128k',
        '-af', 'volume=2.0',
        '-shortest',  # 只用shortest，不用-t
        output_path
    ])
    
    print(f"执行ffmpeg...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERROR: {result.stderr[:500]}")
        return None
    
    # 保存任务
    task_info = {
        'id': task_id,
        'status': 'completed',
        'output_path': output_path,
        'progress': 100,
        'created_at': datetime.now().isoformat(),
        'updated_at': datetime.now().isoformat(),
        'script': script,
        'file_ids': ['ef50db2c-6423-440d-9c82-0d5622aefac7', '1eb45180-814d-48a3-9d23-4639e3d1c42f', '91faffb2-62f5-4ec4-8755-1d55df66013b'],
        'selection_mode': 'interleaved_unique',
        'no_loop': True,
        'tts_primary_audio': True,
        'error': None
    }
    os.makedirs(os.path.join(storage.workdir, 'tasks'), exist_ok=True)
    with open(os.path.join(storage.workdir, 'tasks', f'{task_id}.json'), 'w') as f:
        json.dump(task_info, f)
    
    size = os.path.getsize(output_path)
    print(f"\n完成!")
    print(f"task_id: {task_id}")
    print(f"大小: {size/1024/1024:.1f}MB")
    
    return task_id

if __name__ == '__main__':
    task_id = run_fixed_video()
    print(f"\ntask_id: {task_id}")