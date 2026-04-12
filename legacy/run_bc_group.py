#!/usr/bin/env python3
"""快速生成 B/C 组验证"""
import os, sys, json, uuid, subprocess, asyncio
sys.path.insert(0, '/home/admin/.openclaw/workspace/video-tool')

from core.storage import storage
from pipeline import processor, video_analyzer
from pipeline.tts_provider import generate_tts, create_subtitle_srt_from_meta

def run_group(group_name, materials):
    """运行一组验证"""
    task_id = str(uuid.uuid4())
    print(f"\n=== {group_name} ===")
    print(f"task_id: {task_id}")
    
    # 转码
    all_clips = []
    for i, path in enumerate(materials):
        if not os.path.exists(path):
            print(f"ERROR: {path} not found")
            return None
        
        # 转码
        transcode_path = os.path.join(storage.workdir, f"{task_id}_transcoded_{i}.mp4")
        print(f"转码: {os.path.basename(path)}")
        processor.transcode_to_h264(path, transcode_path)
        
        # 切段
        clips = processor.extract_clips(transcode_path, clip_duration=5)
        for c in clips:
            c['source_index'] = i
        all_clips.extend(clips[:2])  # 每素材取前2段
    
    # 选择 clips - 交错选片，确保多素材均匀分布
    # 按素材分组
    clips_by_source = {}
    for c in all_clips:
        src = c['source_index']
        if src not in clips_by_source:
            clips_by_source[src] = []
        clips_by_source[src].append(c)
    
    # 交错选取：先取每个素材的第一段，再取第二段
    selected = []
    # 第一轮：每个素材取第一段
    for src in sorted(clips_by_source.keys()):
        if clips_by_source[src]:
            selected.append(clips_by_source[src][0])
    # 第二轮：每个素材取第二段（如有）
    for src in sorted(clips_by_source.keys()):
        if len(clips_by_source[src]) > 1:
            selected.append(clips_by_source[src][1])
    
    selected = selected[:4]  # 最多4段
    sources = list(set(c['source_index'] for c in selected))
    print(f"选择 {len(selected)} clips (交错), sources: {sources}")
    
    # 保存 timeline
    timeline_path = os.path.join(storage.workdir, f"{task_id}_timeline.json")
    with open(timeline_path, 'w') as f:
        json.dump({
            'task_id': task_id,
            'selected_clips': selected,
            'sources_used': sources
        }, f, ensure_ascii=False, indent=2)
    
    # TTS
    script = f"这是{group_name}测试。使用多段真实素材拼接，验证系统稳定性。"
    tts_path = os.path.join(storage.workdir, f"{task_id}_tts.mp3")
    tts_meta_path = os.path.join(storage.workdir, f"{task_id}_tts_meta.json")
    tts_meta = asyncio.run(generate_tts(script, tts_path, tts_meta_path))
    
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
        'progress': 100
    }
    os.makedirs(os.path.join(storage.workdir, 'tasks'), exist_ok=True)
    with open(os.path.join(storage.workdir, 'tasks', f'{task_id}.json'), 'w') as f:
        json.dump(task_info, f)
    
    size = os.path.getsize(output_path)
    print(f"完成: {output_path}, 大小: {size/1024/1024:.1f}MB")
    
    return task_id

if __name__ == '__main__':
    # B组素材
    b_materials = [
        '/home/admin/.openclaw/workspace/video-tool/uploads/ad8568bf-0852-4e0d-a6db-5880d3086428.MP4',
        '/home/admin/.openclaw/workspace/video-tool/uploads/bd5f89b2-9779-4487-838b-f16c7202eb16.MP4'
    ]
    
    # C组素材  
    c_materials = [
        '/home/admin/.openclaw/workspace/video-tool/uploads/7669789c-bb40-47c8-b590-f8a25b7a0877.MP4',
        '/home/admin/.openclaw/workspace/video-tool/uploads/8963f5b3-dc86-4299-909e-95ebc6914721.MP4'
    ]
    
    # 运行
    b_id = run_group("B组", b_materials)
    c_id = run_group("C组", c_materials)
    
    print(f"\n=== 结果 ===")
    print(f"B组: {b_id}")
    print(f"C组: {c_id}")