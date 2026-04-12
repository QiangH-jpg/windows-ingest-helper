#!/usr/bin/env python3
"""
正式样片生成脚本 - 精简版

只使用6个核心素材，生成45秒样片
"""
import os, sys, json, uuid, asyncio, subprocess
from datetime import datetime

sys.path.insert(0, '/home/admin/.openclaw/workspace/video-tool')

from core.storage import storage
from pipeline import processor
from pipeline.tts_provider import generate_tts, create_subtitle_srt_from_meta
from pipeline.project_state import validate_script

# 固定素材（精选6个）
FIXED_MATERIALS = [
    '/home/admin/.openclaw/workspace/video-tool/uploads/394A0108.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/394A0109.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115142627_0112_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115144146_0143_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115144510_0148_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115140336_0110_D.MP4',
]

# 固定新闻稿（压缩口播版）
OFFICIAL_SCRIPT = """3月26日，济南市人社局在美团服务中心开展"人社服务大篷车"活动。

活动以"走进奔跑者——保障与你同行"为主题，把人社服务送到外卖骑手等一线劳动者。

现场通过发放资料、面对面讲解，向小哥介绍社保参保、权益保障等政策。

还有互动环节，让大家在轻松氛围中了解政策。

济南市人社局持续推动服务走近新就业形态劳动者，打通保障"最后一公里"。"""

def main():
    task_id = str(uuid.uuid4())
    print(f"[task_id] {task_id}")
    
    # 验证素材
    print("[1/7] 验证素材...")
    materials = []
    for path in FIXED_MATERIALS:
        if os.path.exists(path):
            materials.append(path)
            print(f"  ✓ {os.path.basename(path)}")
        else:
            print(f"  ✗ 不存在: {path}")
    
    print(f"  有效素材: {len(materials)}")
    
    # 转码
    print("[2/7] 转码素材...")
    all_clips = []
    for i, path in enumerate(materials):
        transcode_path = os.path.join(storage.workdir, f"{task_id}_transcoded_{i}.mp4")
        processor.transcode_to_h264(path, transcode_path)
        
        clips = processor.extract_clips(transcode_path, clip_duration=5)
        for c in clips:
            c['source_index'] = i
        all_clips.extend(clips[:2])  # 每个素材只取2个clip
        print(f"  素材{i}: {len(clips[:2])} clips")
    
    print(f"  总clip数: {len(all_clips)}")
    
    # 选片
    print("[3/7] 选片...")
    selected = all_clips[:9]  # 9个clip = 45秒
    print(f"  选择: {len(selected)} clips = {len(selected)*5}秒")
    
    # TTS
    print("[4/7] TTS合成...")
    tts_path = os.path.join(storage.workdir, f"{task_id}_tts.mp3")
    tts_meta_path = os.path.join(storage.workdir, f"{task_id}_tts_meta.json")
    tts_meta = asyncio.run(generate_tts(OFFICIAL_SCRIPT, tts_path, tts_meta_path))
    print(f"  TTS时长: {tts_meta['total_duration']:.2f}秒")
    
    # 字幕
    print("[5/7] 生成字幕...")
    srt_path = os.path.join(storage.workdir, f"{task_id}.srt")
    create_subtitle_srt_from_meta(tts_meta, srt_path)
    print(f"  字幕文件: {srt_path}")
    
    # 合成
    print("[6/7] 合成视频...")
    output_path = os.path.join(storage.outputs_dir, f"{task_id}.mp4")
    processor.assemble_video(selected, tts_path, srt_path, output_path, target_duration=45, keep_concat=True)
    
    # 结果
    print("[7/7] 完成!")
    if os.path.exists(output_path):
        size_mb = os.path.getsize(output_path) / 1024 / 1024
        
        result = subprocess.run(
            ['/home/linuxbrew/.linuxbrew/bin/ffprobe', '-v', 'error', 
             '-show_entries', 'format=duration', 
             '-of', 'default=noprint_wrappers=1:nokey=1', output_path],
            capture_output=True, text=True
        )
        duration = float(result.stdout.strip())
        
        print(f"\n{'='*50}")
        print(f"task_id: {task_id}")
        print(f"输出: {output_path}")
        print(f"大小: {size_mb:.2f} MB")
        print(f"时长: {duration:.2f} 秒")
        print(f"下载: http://47.93.194.154:8088/download/{task_id}")
        print(f"{'='*50}")
        
        # 保存任务
        task_info = {
            'id': task_id,
            'status': 'completed',
            'script': OFFICIAL_SCRIPT,
            'script_source': 'fixed_official',
            'materials': [os.path.basename(m) for m in materials],
            'output_path': output_path,
            'output_size_mb': size_mb,
            'duration_sec': duration,
            'created_at': datetime.now().isoformat()
        }
        
        os.makedirs(os.path.join(storage.workdir, 'tasks'), exist_ok=True)
        with open(os.path.join(storage.workdir, 'tasks', f'{task_id}.json'), 'w', encoding='utf-8') as f:
            json.dump(task_info, f, ensure_ascii=False, indent=2)
        
        return task_id
    else:
        print("合成失败!")
        return None

if __name__ == '__main__':
    main()