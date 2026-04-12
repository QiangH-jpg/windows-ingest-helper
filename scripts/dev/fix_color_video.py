#!/usr/bin/env python3
"""
修复纯色视频问题 - 使用真实素材重新生成

根因：之前使用的素材位于 uploads/test/ 目录，是纯色测试占位文件
修复：使用 uploads/materials/ 中的真实 DJI 素材
"""
import os
import sys
import json
import subprocess
import uuid
from datetime import datetime

sys.path.insert(0, '/home/admin/.openclaw/workspace/video-tool')
from core.config import config
from pipeline.video_cache import get_or_create_processed, extract_dynamic_clip
from pipeline.processor import get_video_duration, assemble_video
from pipeline.tts_provider import generate_tts, create_subtitle_srt_from_meta
from core.storage import storage

VIDEO = config['video']
FFMPEG_PATH = VIDEO['ffmpeg_path']
FFPROBE_PATH = FFMPEG_PATH.replace('ffmpeg', 'ffprobe')

# 使用真实素材（长素材）
REAL_MATERIALS = [
    '/home/admin/.openclaw/workspace/video-tool/uploads/materials/394A0108.MP4',  # 40s
    '/home/admin/.openclaw/workspace/video-tool/uploads/materials/394A0109.MP4',  # 23s
]

# 测试脚本
TEST_SCRIPT = "这是一个使用真实素材生成的测试视频。画面应该显示真实的航拍内容，而不是纯色背景。"

def probe_video(path):
    """获取视频信息"""
    cmd = [
        FFPROBE_PATH, '-v', 'error',
        '-show_entries', 'stream=codec_name,width,height,bit_rate',
        '-show_entries', 'format=duration,size',
        '-of', 'json',
        path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return json.loads(result.stdout)

def extract_frame(video_path, output_path):
    """提取第一帧"""
    cmd = [
        FFMPEG_PATH, '-y',
        '-i', video_path,
        '-vf', 'select=eq(n,0)',
        '-frames:v', '1',
        '-update', '1',
        output_path
    ]
    subprocess.run(cmd, capture_output=True)
    return output_path

def main():
    task_id = f"fix_real_material_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    workdir = os.path.join(storage.workdir, task_id)
    os.makedirs(workdir, exist_ok=True)
    clips_dir = os.path.join(workdir, 'clips')
    os.makedirs(clips_dir, exist_ok=True)
    
    print(f"\n{'='*60}")
    print(f"【修复任务】{task_id}")
    print(f"{'='*60}")
    
    # 1. 原始素材证据
    print(f"\n【1. 原始素材证据】")
    source_info = []
    for material_path in REAL_MATERIALS:
        if not os.path.exists(material_path):
            print(f"  ⚠️ 素材不存在：{material_path}")
            continue
        
        info = probe_video(material_path)
        stream = info.get('streams', [{}])[0]
        fmt = info.get('format', {})
        
        source_info.append({
            'path': material_path,
            'codec': stream.get('codec_name'),
            'width': stream.get('width'),
            'height': stream.get('height'),
            'bit_rate': stream.get('bit_rate'),
            'duration': fmt.get('duration'),
            'size': fmt.get('size')
        })
        
        print(f"  素材：{os.path.basename(material_path)}")
        print(f"    分辨率：{stream.get('width')}x{stream.get('height')}")
        print(f"    比特率：{stream.get('bit_rate')} b/s")
        print(f"    时长：{fmt.get('duration')}s")
        print(f"    大小：{int(fmt.get('size', 0))/1024/1024:.2f} MB")
        
        # 提取帧证据
        frame_path = os.path.join(workdir, f"source_{len(source_info)}.png")
        extract_frame(material_path, frame_path)
        frame_size = os.path.getsize(frame_path) if os.path.exists(frame_path) else 0
        print(f"    帧图大小：{frame_size/1024:.1f} KB (真实画面证据)")
    
    # 2. 转码后素材
    print(f"\n【2. 转码后素材证据】")
    processed_paths = []
    for material_path in REAL_MATERIALS:
        if not os.path.exists(material_path):
            continue
        
        processed_path = get_or_create_processed(material_path)
        processed_paths.append(processed_path)
        
        info = probe_video(processed_path)
        stream = info.get('streams', [{}])[0]
        fmt = info.get('format', {})
        
        print(f"  转码后：{os.path.basename(processed_path)[:60]}...")
        print(f"    分辨率：{stream.get('width')}x{stream.get('height')}")
        print(f"    比特率：{stream.get('bit_rate')} b/s")
        print(f"    大小：{int(fmt.get('size', 0))/1024/1024:.2f} MB")
        
        # 提取帧证据
        frame_path = os.path.join(workdir, f"processed_{len(processed_paths)}.png")
        extract_frame(processed_path, frame_path)
        frame_size = os.path.getsize(frame_path) if os.path.exists(frame_path) else 0
        print(f"    帧图大小：{frame_size/1024:.1f} KB (转码后画面证据)")
    
    # 3. 切片 clips
    print(f"\n【3. Clip 切片证据】")
    all_clips = []
    for i, processed_path in enumerate(processed_paths):
        duration = get_video_duration(processed_path)
        print(f"  素材{i+1}: {duration:.1f}s")
        
        # 每个素材取 2 个 clip
        clip_count = min(2, int(duration / 5))
        for j in range(clip_count):
            start = j * 5
            clip = extract_dynamic_clip(processed_path, start, 5, workdir=storage.workdir, task_id=task_id, clip_id=j)
            if clip:
                all_clips.append(clip)
                clip_info = probe_video(clip['path'])
                clip_fmt = clip_info.get('format', {})
                print(f"    Clip{j}: {os.path.basename(clip['path'])[:50]}...")
                print(f"      时长：{clip['duration']}s, 大小：{int(clip_fmt.get('size', 0))/1024:.1f} KB")
                
                # 提取 clip 帧证据
                clip_frame = os.path.join(workdir, f"clip_{len(all_clips)}.png")
                extract_frame(clip['path'], clip_frame)
                clip_frame_size = os.path.getsize(clip_frame) if os.path.exists(clip_frame) else 0
                print(f"      帧图：{clip_frame_size/1024:.1f} KB")
    
    print(f"\n  总 clip 数：{len(all_clips)}")
    
    # 4. 生成 TTS 和字幕
    print(f"\n【4. TTS 和字幕】")
    tts_path = os.path.join(workdir, 'tts.mp3')
    tts_meta_path = os.path.join(workdir, 'tts_meta.json')
    tts_meta = generate_tts(TEST_SCRIPT, tts_path, tts_meta_path)
    tts_duration = get_video_duration(tts_path)
    print(f"  TTS 时长：{tts_duration:.2f}s")
    print(f"  TTS 文件：{os.path.basename(tts_path)}")
    
    srt_path = os.path.join(workdir, 'subtitles.srt')
    create_subtitle_srt_from_meta(tts_meta, srt_path)
    print(f"  字幕文件：{os.path.basename(srt_path)}")
    
    # 5. 组装视频
    print(f"\n【5. 视频组装】")
    output_path = os.path.join(storage.outputs_dir, f"{task_id}.mp4")
    
    # 生成 concat 文件
    concat_path = output_path + '.concat.txt'
    with open(concat_path, 'w') as f:
        for clip in all_clips:
            f.write(f"file '{os.path.abspath(clip['path'])}'\n")
    
    print(f"  Concat 文件内容:")
    with open(concat_path, 'r') as f:
        for line in f:
            print(f"    {line.strip()}")
    
    # 执行组装
    assemble_video(all_clips, tts_path, srt_path, output_path, target_duration=tts_duration, keep_concat=True)
    
    # 6. 验证输出
    print(f"\n【6. 输出验证】")
    output_info = probe_video(output_path)
    output_stream = output_info.get('streams', [{}])[0]
    output_fmt = output_info.get('format', {})
    
    print(f"  输出文件：{os.path.basename(output_path)}")
    print(f"    分辨率：{output_stream.get('width')}x{output_stream.get('height')}")
    print(f"    编码：{output_stream.get('codec_name')}")
    print(f"    时长：{output_fmt.get('duration')}s")
    print(f"    大小：{int(output_fmt.get('size', 0))/1024/1024:.2f} MB")
    print(f"    比特率：{output_stream.get('bit_rate')} b/s")
    
    # 提取输出帧证据
    output_frame = os.path.join(workdir, 'output_frame.png')
    extract_frame(output_path, output_frame)
    output_frame_size = os.path.getsize(output_frame) if os.path.exists(output_frame) else 0
    print(f"    帧图大小：{output_frame_size/1024:.1f} KB (最终画面证据)")
    
    # 7. 生成证据报告
    print(f"\n【7. 证据报告】")
    report = {
        'task_id': task_id,
        'timestamp': datetime.now().isoformat(),
        'fix_description': '使用真实素材替代测试占位文件',
        'root_cause': '原素材位于 uploads/test/ 目录，是纯色测试视频 (5kb/s, 44KB/60s)',
        'source_materials': source_info,
        'processed_materials': processed_paths,
        'clips_count': len(all_clips),
        'clips': [{'path': c['path'], 'duration': c['duration']} for c in all_clips],
        'tts_duration': tts_duration,
        'output': {
            'path': output_path,
            'duration': output_fmt.get('duration'),
            'size': output_fmt.get('size'),
            'resolution': f"{output_stream.get('width')}x{output_stream.get('height')}",
            'bitrate': output_stream.get('bit_rate')
        },
        'evidence_frames': {
            'source': [os.path.join(workdir, f"source_{i+1}.png") for i in range(len(source_info))],
            'processed': [os.path.join(workdir, f"processed_{i+1}.png") for i in range(len(processed_paths))],
            'clips': [os.path.join(workdir, f"clip_{i+1}.png") for i in range(len(all_clips))],
            'output': output_frame
        }
    }
    
    report_path = os.path.join(workdir, 'fix_report.json')
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    
    print(f"  报告已保存：{report_path}")
    print(f"\n{'='*60}")
    print(f"✅ 修复完成！输出视频：{output_path}")
    print(f"{'='*60}")
    
    return output_path, report

if __name__ == '__main__':
    output_path, report = main()
