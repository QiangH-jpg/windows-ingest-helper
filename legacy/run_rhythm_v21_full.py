#!/usr/bin/env python3
"""
剪辑节奏优化 V2.1 - 完整视频生成

在 V2.0 基础上增强：
1. 镜头时长分布：短 (2-3 秒 30%)/中 (3-4 秒 50%)/长 (4-6 秒 20%)
2. 节奏变化规则：短→中→短→长→中
3. 镜头类型节奏：全景开头→近景中段→互动中后→交流结尾
4. 相邻镜头关系：避免跳跃，优先渐进
"""
import os, sys, json, uuid, asyncio
sys.path.insert(0, '/home/admin/.openclaw/workspace/video-tool')

from core.storage import storage
from pipeline import processor
from pipeline.tts_provider import generate_tts, create_subtitle_srt_from_meta
from pipeline.project_state import validate_script, validate_task
from pipeline.video_cache import get_or_create_processed, extract_dynamic_clip
from pipeline.semantic_selector_v2 import load_material_tags_v2, select_best_material_v2
from pipeline.rhythm_optimizer_v21 import apply_rhythm_optimization, calculate_shot_type_sequence_score, get_shot_type_from_tags
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

SCRIPT_SEGMENTS = [
    {'id': 1, 'text': '3 月 26 日，济南市人社局在美团服务中心开展"人社服务大篷车"活动。', 'target_actions': ['全景展示', '举横幅合影', '拍摄横幅']},
    {'id': 2, 'text': '活动以"走进奔跑者——保障与你同行"为主题。', 'target_actions': ['举横幅合影', '拍摄横幅']},
    {'id': 3, 'text': '把人社服务送到外卖骑手等一线劳动者。', 'target_actions': ['骑手近景', '骑手列队', '骑手微笑']},
    {'id': 4, 'text': '现场通过发放资料。', 'target_actions': ['递发资料']},
    {'id': 5, 'text': '面对面讲解。', 'target_actions': ['面对面讲解', '手势指引']},
    {'id': 6, 'text': '向小哥介绍社保参保权益保障等政策。', 'target_actions': ['面对面讲解', '展示宣传页', '手势指引']},
    {'id': 7, 'text': '还有互动环节。', 'target_actions': ['投掷互动', '问答互动']},
    {'id': 8, 'text': '让大家在轻松氛围中了解政策。', 'target_actions': ['骑手微笑', '轻松交流']},
    {'id': 9, 'text': '济南市人社局持续推动服务走近新就业形态劳动者。', 'target_actions': ['领导讲话', '握手交流', '面对面讲解']},
    {'id': 10, 'text': '打通保障"最后一公里"。', 'target_actions': ['全景展示', '握手交流']}
]

def main():
    enforce_pre_check()
    guard = get_guard()
    
    print("=" * 60)
    print("剪辑节奏优化 V2.1 - 完整视频生成")
    print("=" * 60)
    
    # 1. 验证任务
    print("\n[1] 验证任务...")
    validation = validate_task('剪辑节奏优化 V2.1')
    if validation['decision'] == 'reject':
        print(f"  ✗ 任务被拒绝：{validation['reason']}")
        return
    print("  ✓ 任务验证通过")
    
    # 2. 生成 task_id
    task_id = str(uuid.uuid4())
    print(f"\n[2] 任务 ID: {task_id}")
    
    # 3. TTS 合成
    print("\n[3] TTS 合成...")
    full_script = ' '.join(seg['text'] for seg in SCRIPT_SEGMENTS)
    tts_path = os.path.join(storage.workdir, f"{task_id}_tts.mp3")
    tts_meta_path = os.path.join(storage.workdir, f"{task_id}_tts_meta.json")
    tts_meta = asyncio.run(generate_tts(full_script, tts_path, tts_meta_path))
    tts_duration = tts_meta['total_duration']
    print(f"  TTS 时长：{tts_duration:.2f} 秒")
    
    # 4. 语义选片 V2.0（先选片，后优化节奏）
    print("\n[4] 语义选片 V2.0（三层标签 + 镜头质量）...")
    
    selected_clips = []
    used_materials = {}
    last_material = None
    
    for i, unit in enumerate(SCRIPT_SEGMENTS):
        unit_duration = tts_duration / len(SCRIPT_SEGMENTS)
        
        candidates = []
        for path in FIXED_MATERIALS:
            filename = os.path.basename(path)
            if last_material and filename == last_material:
                continue
            candidates.append({'path': path, 'name': filename})
        
        if not candidates:
            candidates = [{'path': p, 'name': os.path.basename(p)} for p in FIXED_MATERIALS]
        
        best, reason, quality_score = select_best_material_v2(
            candidates,
            unit['target_tags'] if 'target_tags' in unit else [],
            unit['target_actions'],
            used_materials,
            last_material
        )
        
        if best:
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
                clip_result['quality_score'] = quality_score
                selected_clips.append(clip_result)
                
                used_materials[best['name']] = used_materials.get(best['name'], 0) + 1
                last_material = best['name']
    
    print(f"  选中 {len(selected_clips)} 个镜头")
    
    # 5. 应用节奏优化
    print("\n[5] 应用剪辑节奏优化...")
    
    optimized_clips = apply_rhythm_optimization(selected_clips, tts_duration)
    
    # 输出节奏分布
    short_count = sum(1 for c in optimized_clips if c['duration_category'] == '短')
    medium_count = sum(1 for c in optimized_clips if c['duration_category'] == '中')
    long_count = sum(1 for c in optimized_clips if c['duration_category'] == '长')
    
    print(f"  镜头时长分布：")
    print(f"    短镜头 (2-3 秒): {short_count}个 ({short_count/len(optimized_clips)*100:.0f}%)")
    print(f"    中镜头 (3-4 秒): {medium_count}个 ({medium_count/len(optimized_clips)*100:.0f}%)")
    print(f"    长镜头 (4-6 秒): {long_count}个 ({long_count/len(optimized_clips)*100:.0f}%)")
    
    # 6. 镜头类型序列评分
    print("\n[6] 镜头类型序列评分...")
    shot_type_score, shot_type_reason = calculate_shot_type_sequence_score(optimized_clips)
    print(f"  镜头类型序列得分：{shot_type_score}/10")
    print(f"  说明：{shot_type_reason}")
    
    # 7. 输出详细节奏信息
    print("\n[7] 镜头节奏详情：")
    for i, clip in enumerate(optimized_clips):
        shot_type = get_shot_type_from_tags(clip.get('tags_v2', []))
        duration = clip['optimized_duration']
        category = clip['duration_category']
        print(f"  镜头{i+1}: {clip['source_name']} | {shot_type} | {duration:.2f}s ({category})")
    
    # 8. 生成字幕
    print("\n[8] 生成字幕...")
    srt_path = os.path.join(storage.workdir, f"{task_id}.srt")
    create_subtitle_srt_from_meta(tts_meta, srt_path)
    
    # 9. 合成视频（使用优化后的时长）
    print("\n[9] 合成视频（节奏优化版）...")
    
    # 更新 clip 的 duration 为优化后的值
    for clip in optimized_clips:
        clip['duration'] = clip['optimized_duration']
    
    output_path = os.path.join(storage.outputs_dir, f"{task_id}.mp4")
    
    processor.assemble_video(
        optimized_clips,
        tts_path,
        srt_path,
        output_path,
        target_duration=int(tts_duration),
        keep_concat=True
    )
    
    if not os.path.exists(output_path):
        print("\n✗ 合成失败")
        return
    
    # 10. 获取视频信息
    import subprocess
    probe_result = subprocess.run(
        ['/home/linuxbrew/.linuxbrew/bin/ffprobe', '-v', 'error',
         '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', output_path],
        capture_output=True, text=True
    )
    video_duration = float(probe_result.stdout.strip())
    video_size = os.path.getsize(output_path) / 1024 / 1024
    
    # 11. 保存选片结果
    result_path = os.path.join(storage.workdir, f"{task_id}_v21_rhythm.json")
    with open(result_path, 'w', encoding='utf-8') as f:
        json.dump({
            'task_id': task_id,
            'video_duration': video_duration,
            'tts_duration': tts_duration,
            'shot_type_score': shot_type_score,
            'duration_distribution': {
                'short': short_count,
                'medium': medium_count,
                'long': long_count
            },
            'clips': [
                {
                    'index': i,
                    'material': clip['source_name'],
                    'shot_type': get_shot_type_from_tags(clip.get('tags_v2', [])),
                    'duration': clip['optimized_duration'],
                    'category': clip['duration_category']
                }
                for i, clip in enumerate(optimized_clips)
            ]
        }, f, ensure_ascii=False, indent=2)
    
    # 12. 输出结果
    print("\n" + "=" * 60)
    print("生成完成")
    print("=" * 60)
    
    print(f"\n【视频信息】")
    print(f"  task_id: {task_id}")
    print(f"  视频时长：{video_duration:.2f}s")
    print(f"  TTS 时长：{tts_duration:.2f}s")
    print(f"  差值：{abs(video_duration - tts_duration):.2f}s")
    print(f"  文件大小：{video_size:.2f}MB")
    print(f"  镜头数：{len(optimized_clips)}个")
    print(f"  镜头类型序列得分：{shot_type_score}/10")
    
    print(f"\n【下载地址】")
    print(f"  http://47.93.194.154:8088/download/{task_id}")
    
    print(f"\n【节奏分布】")
    print(f"  短镜头 (2-3 秒): {short_count}个 ({short_count/len(optimized_clips)*100:.0f}%) - 目标 30%")
    print(f"  中镜头 (3-4 秒): {medium_count}个 ({medium_count/len(optimized_clips)*100:.0f}%) - 目标 50%")
    print(f"  长镜头 (4-6 秒): {long_count}个 ({long_count/len(optimized_clips)*100:.0f}%) - 目标 20%")
    
    print(f"\n【节奏序列】")
    sequence = " → ".join(clip['duration_category'] for clip in optimized_clips)
    print(f"  {sequence}")
    
    print(f"\n选片结果已保存：{result_path}")
    
    return task_id, output_path

if __name__ == '__main__':
    main()
