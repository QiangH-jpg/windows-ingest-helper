#!/usr/bin/env python3
"""
语义选片 V1.5 - 完整视频生成

基于动作标签优先的语义选片，生成完整视频样片。
"""
import os, sys, json, uuid, asyncio
sys.path.insert(0, '/home/admin/.openclaw/workspace/video-tool')

from core.storage import storage
from pipeline import processor
from pipeline.tts_provider import generate_tts, create_subtitle_srt_from_meta
from pipeline.project_state import validate_script, validate_task
from pipeline.video_cache import get_or_create_processed, extract_dynamic_clip
from pipeline.semantic_selector_v15 import load_material_tags_v15, load_semantic_units, select_best_material_v15
from pipeline.memory_guard import enforce_pre_check, get_guard

FIXED_MATERIALS = [
    '/home/admin/.openclaw/workspace/video-tool/uploads/394A0108.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/394A0109.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115140223_0109_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115140336_0110_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115142627_0112_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115143401_0119_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115143406_0120_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115143625_0127_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115143827_0133_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115144146_0143_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115144241_0146_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115144510_0148_D.MP4',
]

def main():
    enforce_pre_check()
    guard = get_guard()
    
    print("=" * 60)
    print("语义选片 V1.5 - 完整视频生成")
    print("=" * 60)
    
    # 1. 验证任务
    print("\n[1] 验证任务...")
    validation = validate_task('语义选片 V1.5 完整生成')
    if validation['decision'] == 'reject':
        print(f"  ✗ 任务被拒绝：{validation['reason']}")
        return
    print("  ✓ 任务验证通过")
    
    # 2. 生成 task_id
    task_id = str(uuid.uuid4())
    print(f"\n[2] 任务 ID: {task_id}")
    
    # 3. 加载语义单元
    print("\n[3] 加载语义单元...")
    units = load_semantic_units()
    print(f"  已加载 {len(units)} 个语义单元")
    
    # 4. TTS 合成
    print("\n[4] TTS 合成...")
    full_script = ' '.join(unit['text'] for unit in units)
    tts_path = os.path.join(storage.workdir, f"{task_id}_tts.mp3")
    tts_meta_path = os.path.join(storage.workdir, f"{task_id}_tts_meta.json")
    tts_meta = asyncio.run(generate_tts(full_script, tts_path, tts_meta_path))
    tts_duration = tts_meta['total_duration']
    print(f"  TTS 时长：{tts_duration:.2f} 秒")
    
    # 5. 语义选片 V1.5 + 动态裁剪
    print("\n[5] 语义选片 V1.5 + 动态裁剪...")
    
    selected_clips = []
    used_materials = {}
    last_material = None
    current_time = 0.0
    
    for i, unit in enumerate(units):
        # 计算该单元时长（按 TTS 时间比例）
        unit_duration = tts_duration / len(units)
        
        # 构建候选素材
        candidates = []
        for path in FIXED_MATERIALS:
            filename = os.path.basename(path)
            if last_material and filename == last_material:
                continue
            candidates.append({'path': path, 'name': filename})
        
        if not candidates:
            candidates = [{'path': p, 'name': os.path.basename(p)} for p in FIXED_MATERIALS]
        
        # 选择最佳素材
        best, reason = select_best_material_v15(
            candidates,
            unit['target_tags'],
            unit['target_actions'],
            used_materials,
            last_material
        )
        
        if best:
            # 动态裁剪（随机起点）
            import random
            random_start = random.uniform(1.0, 5.0)
            
            clip_result = extract_dynamic_clip(
                best['path'],
                start=random_start,
                duration=unit_duration,
                workdir=storage.workdir,
                task_id=task_id,
                clip_id=i
            )
            
            if clip_result:
                clip_result['unit'] = unit
                clip_result['reason'] = reason
                selected_clips.append(clip_result)
                print(f"  单元{i+1}: {best['name']} ({unit_duration:.1f}s) - {reason}")
                
                # 更新使用记录
                filename = best['name']
                used_materials[filename] = used_materials.get(filename, 0) + 1
                last_material = filename
                current_time += unit_duration
    
    print(f"\n  选中 {len(selected_clips)} 个镜头")
    
    # 6. 生成字幕
    print("\n[6] 生成字幕...")
    srt_path = os.path.join(storage.workdir, f"{task_id}.srt")
    create_subtitle_srt_from_meta(tts_meta, srt_path)
    
    # 7. 合成视频
    print("\n[7] 合成视频...")
    output_path = os.path.join(storage.outputs_dir, f"{task_id}.mp4")
    
    processor.assemble_video(
        selected_clips,
        tts_path,
        srt_path,
        output_path,
        target_duration=int(tts_duration),
        keep_concat=True
    )
    
    if not os.path.exists(output_path):
        print("\n✗ 合成失败")
        return
    
    # 8. 获取视频信息
    import subprocess
    probe_result = subprocess.run(
        ['/home/linuxbrew/.linuxbrew/bin/ffprobe', '-v', 'error',
         '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', output_path],
        capture_output=True, text=True
    )
    video_duration = float(probe_result.stdout.strip())
    video_size = os.path.getsize(output_path) / 1024 / 1024
    
    # 9. 保存选片结果
    result_path = os.path.join(storage.workdir, f"{task_id}_v15_full.json")
    with open(result_path, 'w', encoding='utf-8') as f:
        json.dump({
            'task_id': task_id,
            'video_duration': video_duration,
            'tts_duration': tts_duration,
            'selections': [
                {
                    'unit_id': clip['unit']['id'],
                    'text': clip['unit']['text'],
                    'target_actions': clip['unit']['target_actions'],
                    'material': clip['source_name'],
                    'reason': clip['reason']
                }
                for clip in selected_clips
            ]
        }, f, ensure_ascii=False, indent=2)
    
    # 10. 输出结果
    print("\n" + "=" * 60)
    print("生成完成")
    print("=" * 60)
    
    print(f"\n【视频信息】")
    print(f"  task_id: {task_id}")
    print(f"  视频时长：{video_duration:.2f}s")
    print(f"  TTS 时长：{tts_duration:.2f}s")
    print(f"  差值：{abs(video_duration - tts_duration):.2f}s")
    print(f"  文件大小：{video_size:.2f}MB")
    print(f"  镜头数：{len(selected_clips)}个")
    
    print(f"\n【下载地址】")
    print(f"  http://47.93.194.154:8088/download/{task_id}")
    
    print(f"\n【选片明细】")
    for i, clip in enumerate(selected_clips):
        print(f"  单元{i+1}: {clip['source_name']} - {clip['reason']}")
    
    print(f"\n选片结果已保存：{result_path}")
    
    return task_id, output_path

if __name__ == '__main__':
    main()
