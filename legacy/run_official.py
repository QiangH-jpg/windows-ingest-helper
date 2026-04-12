#!/usr/bin/env python3
"""
正式样片生成脚本 - 固定素材 + 固定新闻稿版

严格遵守 PROJECT_STATE.md 约束：
- 使用固定素材清单
- 使用固定新闻稿（压缩口播版）
- TTS、字幕、时长全部绑定用户稿件
- 禁止测试文案
"""
import os, sys, json, uuid, asyncio
from datetime import datetime
sys.path.insert(0, '/home/admin/.openclaw/workspace/video-tool')

from core.storage import storage
from pipeline import processor
from pipeline.tts_provider import generate_tts, create_subtitle_srt_from_meta
from pipeline.project_state import validate_script, load_project_state

# ============================================================
# 固定素材清单（严格使用，不更换）
# ============================================================
FIXED_MATERIALS = [
    '/home/admin/.openclaw/workspace/video-tool/uploads/394A0108.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/394A0109.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115140223_0109_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115140336_0110_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115142627_0112_D.MP4',
    # 跳过1秒片段: DJI_20001115143357_0118_D.MP4
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115143401_0119_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115143406_0120_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115143625_0127_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115143827_0133_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115144146_0143_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115144241_0146_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115144510_0148_D.MP4',
]

# ============================================================
# 固定新闻稿（压缩口播版）
# ============================================================
OFFICIAL_SCRIPT = """3月26日，济南市人社局在美团服务中心开展"人社服务大篷车"活动。

活动以"走进奔跑者——保障与你同行"为主题，把人社服务送到外卖骑手等一线劳动者。

现场通过发放资料、面对面讲解，向小哥介绍社保参保、权益保障等政策。

还有互动环节，让大家在轻松氛围中了解政策。

济南市人社局持续推动服务走近新就业形态劳动者，打通保障"最后一公里"。"""

def main():
    """主流程"""
    print("=" * 60)
    print("正式样片生成 - 固定素材 + 固定新闻稿")
    print("=" * 60)
    
    # 加载项目状态
    print("\n[1] 加载项目状态...")
    state = load_project_state()
    print("  ✓ PROJECT_STATE.md 已加载")
    
    # 验证稿件
    print("\n[2] 验证稿件...")
    validation = validate_script(OFFICIAL_SCRIPT)
    if validation['decision'] == 'reject':
        print(f"  ✗ 稿件被拒绝: {validation['reason']}")
        return
    print("  ✓ 稿件验证通过")
    
    task_id = str(uuid.uuid4())
    print(f"\n[3] 任务ID: {task_id}")
    
    # 素材验证
    print("\n[4] 验证素材...")
    valid_materials = []
    for path in FIXED_MATERIALS:
        if os.path.exists(path):
            size_mb = os.path.getsize(path) / 1024 / 1024
            print(f"  ✓ {os.path.basename(path)} ({size_mb:.1f}MB)")
            valid_materials.append(path)
        else:
            print(f"  ✗ 素材不存在: {path}")
    
    if len(valid_materials) < 3:
        print(f"\n✗ 错误: 有效素材不足（需要≥3，实际{len(valid_materials)}）")
        return
    
    print(f"\n  有效素材: {len(valid_materials)} 个")
    
    # 正文绑定
    print("\n[5] 正文绑定...")
    print(f"  稿件来源: 固定新闻稿")
    print(f"  稿件长度: {len(OFFICIAL_SCRIPT)} 字符")
    print(f"  预计时长: 40-50 秒")
    
    # 转码 + 切段
    print("\n[6] 处理素材...")
    all_clips = []
    
    for i, path in enumerate(valid_materials):
        transcode_path = os.path.join(storage.workdir, f"{task_id}_transcoded_{i}.mp4")
        processor.transcode_to_h264(path, transcode_path)
        
        clips = processor.extract_clips(transcode_path, clip_duration=5)
        for c in clips:
            c['source_index'] = i
            c['source_name'] = os.path.basename(path)
        all_clips.extend(clips)
        print(f"  素材{i}: {os.path.basename(path)} → {len(clips)} clips")
    
    # 长片段优先选片
    print("\n[7] 选片（长片段优先）...")
    
    # 按素材源分组
    clips_by_source = {}
    for c in all_clips:
        src = c['source_index']
        if src not in clips_by_source:
            clips_by_source[src] = []
        clips_by_source[src].append(c)
    
    # 从每个素材选2-3个clip
    selected = []
    for src in sorted(clips_by_source.keys()):
        clips = clips_by_source[src][:3]  # 每个素材最多3个clip
        selected.extend(clips)
    
    total_clips = len(selected)
    video_duration = total_clips * 5
    
    print(f"  选择: {total_clips} clips")
    print(f"  视频时长: {video_duration} 秒")
    
    # TTS
    print("\n[8] TTS合成...")
    tts_path = os.path.join(storage.workdir, f"{task_id}_tts.mp3")
    tts_meta_path = os.path.join(storage.workdir, f"{task_id}_tts_meta.json")
    tts_meta = asyncio.run(generate_tts(OFFICIAL_SCRIPT, tts_path, tts_meta_path))
    
    tts_duration = tts_meta['total_duration']
    print(f"  TTS时长: {tts_duration:.2f} 秒")
    print(f"  分句数: {tts_meta['sentence_count']}")
    
    # 字幕
    print("\n[9] 生成字幕...")
    srt_path = os.path.join(storage.workdir, f"{task_id}.srt")
    create_subtitle_srt_from_meta(tts_meta, srt_path)
    print(f"  字幕文件: {srt_path}")
    
    # 保存timeline
    timeline = {
        'task_id': task_id,
        'created_at': datetime.now().isoformat(),
        'materials': [os.path.basename(m) for m in valid_materials],
        'selected_clips': [
            {
                'clip_path': c['path'],
                'source_index': c['source_index'],
                'source_name': c['source_name'],
                'start': c['start'],
                'duration': c['duration']
            }
            for c in selected
        ],
        'selection_mode': 'long_segment_priority',
        'video_duration_sec': video_duration,
        'tts_duration_sec': tts_duration
    }
    
    timeline_path = os.path.join(storage.workdir, f"{task_id}_timeline.json")
    with open(timeline_path, 'w', encoding='utf-8') as f:
        json.dump(timeline, f, ensure_ascii=False, indent=2)
    
    # 合成视频
    print("\n[10] 合成视频...")
    output_path = os.path.join(storage.outputs_dir, f"{task_id}.mp4")
    
    processor.assemble_video(
        selected, 
        tts_path, 
        srt_path, 
        output_path, 
        target_duration=min(video_duration, int(tts_duration + 5)),
        keep_concat=True
    )
    
    # 结果
    if os.path.exists(output_path):
        output_size = os.path.getsize(output_path) / 1024 / 1024
        
        # 获取实际时长
        import subprocess
        result = subprocess.run(
            ['/home/linuxbrew/.linuxbrew/bin/ffprobe', '-v', 'error', 
             '-show_entries', 'format=duration', 
             '-of', 'default=noprint_wrappers=1:nokey=1', output_path],
            capture_output=True, text=True
        )
        actual_duration = float(result.stdout.strip())
        
        print("\n" + "=" * 60)
        print("生成完成")
        print("=" * 60)
        print(f"task_id: {task_id}")
        print(f"输出文件: {output_path}")
        print(f"文件大小: {output_size:.2f} MB")
        print(f"实际时长: {actual_duration:.2f} 秒")
        print(f"TTS时长: {tts_duration:.2f} 秒")
        print(f"素材数量: {len(valid_materials)}")
        print(f"公网地址: http://47.93.194.154:8088/download/{task_id}")
        
        # 保存任务信息
        task_info = {
            'id': task_id,
            'status': 'completed',
            'script': OFFICIAL_SCRIPT,
            'script_source': 'fixed_official',
            'tts_source': 'user_script',
            'subtitle_source': 'tts_meta',
            'materials': [os.path.basename(m) for m in valid_materials],
            'output_path': output_path,
            'output_size_mb': output_size,
            'actual_duration_sec': actual_duration,
            'tts_duration_sec': tts_duration,
            'created_at': datetime.now().isoformat()
        }
        
        os.makedirs(os.path.join(storage.workdir, 'tasks'), exist_ok=True)
        with open(os.path.join(storage.workdir, 'tasks', f'{task_id}.json'), 'w', encoding='utf-8') as f:
            json.dump(task_info, f, ensure_ascii=False, indent=2)
        
        return task_id, output_path
    else:
        print("\n✗ 合成失败")
        return None, None

if __name__ == '__main__':
    main()