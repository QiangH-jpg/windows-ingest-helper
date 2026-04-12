#!/usr/bin/env python3
"""C组多素材修复验证 - 强制交错选片"""
import os, sys, json, uuid, asyncio
from datetime import datetime
sys.path.insert(0, '/home/admin/.openclaw/workspace/video-tool')

from core.storage import storage
from pipeline import processor
from pipeline.tts_provider import generate_tts, create_subtitle_srt_from_meta

def run_c_fixed():
    """C组修复版本 - 强制多素材入链"""
    task_id = str(uuid.uuid4())
    print(f"\n=== C组修复版 ===")
    print(f"task_id: {task_id}")
    
    materials = [
        '/home/admin/.openclaw/workspace/video-tool/uploads/7669789c-bb40-47c8-b590-f8a25b7a0877.MP4',
        '/home/admin/.openclaw/workspace/video-tool/uploads/8963f5b3-dc86-4299-909e-95ebc6914721.MP4'
    ]
    
    # 转码
    all_clips = []
    for i, path in enumerate(materials):
        transcode_path = os.path.join(storage.workdir, f"{task_id}_transcoded_{i}.mp4")
        print(f"转码素材{i}: {os.path.basename(path)}")
        processor.transcode_to_h264(path, transcode_path)
        
        clips = processor.extract_clips(transcode_path, clip_duration=5)
        for c in clips:
            c['source_index'] = i
        all_clips.extend(clips[:2])
    
    # ========== 强制交错选片 ==========
    clips_by_source = {0: [], 1: []}
    for c in all_clips:
        clips_by_source[c['source_index']].append(c)
    
    # 交错排列：先取每个素材的第一段，再取第二段（如有）
    selected = []
    # 第一轮：每个素材至少贡献1段
    for src in sorted(clips_by_source.keys()):
        if clips_by_source[src]:
            selected.append(clips_by_source[src][0])
    # 第二轮：有第二段的素材补充
    for src in sorted(clips_by_source.keys()):
        if len(clips_by_source[src]) > 1:
            selected.append(clips_by_source[src][1])
    
    # 确保至少有2个不同素材
    sources = list(set(c['source_index'] for c in selected))
    if len(sources) < 2:
        print("ERROR: 无法保证多素材入链")
        return None
    
    sources = list(set(c['source_index'] for c in selected))
    sources = list(set(c['source_index'] for c in selected))
    print(f"交错选片: {len(selected)} clips, sources: {sources}")
    clip_order = " -> ".join([f"素材{c['source_index']}_clip{c['start']//5}" for c in selected])
    print(f"排列顺序: {clip_order}")
    
    # 保存 timeline
    timeline_path = os.path.join(storage.workdir, f"{task_id}_timeline.json")
    timeline_data = {
        'task_id': task_id,
        'selected_clips': [
            {
                'clip_path': c['path'],
                'source_index': c['source_index'],
                'source_file': materials[c['source_index']],
                'start': c['start'],
                'duration': c['duration']
            }
            for c in selected
        ],
        'sources_used': sources,
        'selection_mode': 'interleaved',
        'created_at': datetime.now().isoformat()
    }
    with open(timeline_path, 'w') as f:
        json.dump(timeline_data, f, ensure_ascii=False, indent=2)
    
    # TTS
    script = "这是C组多素材修复测试。第一段来自素材零，第二段来自素材一。验证多素材真正入链。"
    tts_path = os.path.join(storage.workdir, f"{task_id}_tts.mp3")
    tts_meta_path = os.path.join(storage.workdir, f"{task_id}_tts_meta.json")
    tts_meta = asyncio.run(generate_tts(script, tts_path, tts_meta_path))
    print(f"TTS时长: {tts_meta['total_duration']}秒")
    
    # SRT
    srt_path = os.path.join(storage.workdir, f"{task_id}.srt")
    create_subtitle_srt_from_meta(tts_meta, srt_path)
    
    # 生成视频
    output_path = storage.get_output_path(task_id)
    processor.assemble_video(selected, tts_path, srt_path, output_path, target_duration=15, keep_concat=True)
    
    # 保存任务
    task_info = {
        'id': task_id,
        'status': 'completed',
        'output_path': output_path,
        'progress': 100,
        'created_at': datetime.now().isoformat(),
        'updated_at': datetime.now().isoformat(),
        'script': script,
        'file_ids': ['7669789c-bb40-47c8-b590-f8a25b7a0877', '8963f5b3-dc86-4299-909e-95ebc6914721'],
        'error': None
    }
    os.makedirs(os.path.join(storage.workdir, 'tasks'), exist_ok=True)
    with open(os.path.join(storage.workdir, 'tasks', f'{task_id}.json'), 'w') as f:
        json.dump(task_info, f)
    
    size = os.path.getsize(output_path)
    print(f"\n完成!")
    print(f"task_id: {task_id}")
    print(f"输出路径: {output_path}")
    print(f"文件大小: {size/1024/1024:.1f}MB")
    
    return task_id

if __name__ == '__main__':
    task_id = run_c_fixed()
    print(f"\n新C组task_id: {task_id}")