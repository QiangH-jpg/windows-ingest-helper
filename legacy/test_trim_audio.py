#!/usr/bin/env python3
"""
测试素材不足场景：策略C（裁剪音频）
"""
import os, sys, uuid, subprocess

sys.path.insert(0, '/home/admin/.openclaw/workspace/video-tool')

from pipeline.audio_driven_timeline import assemble_video_audio_driven, get_duration

WORKDIR = '/home/admin/.openclaw/workspace/video-tool/workdir'

def format_srt_time(seconds):
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{ms:03d}"

# 找短素材（<5秒）
short_materials = []
for f in os.listdir(WORKDIR):
    if f.endswith('.mp4') and 'transcoded' in f:
        path = os.path.join(WORKDIR, f)
        try:
            dur = get_duration(path)
            if dur < 5.0:
                short_materials.append({'path': path, 'duration': dur})
        except:
            continue

print(f"找到 {len(short_materials)} 个短素材文件（<5s）")

# 选3个短素材
clips = short_materials[:3] if len(short_materials) >= 3 else short_materials[:2]

# 计算素材总时长
total_clip_duration = sum([get_duration(c['path']) for c in clips])

print(f"\n选中的 Clips:")
for i, c in enumerate(clips):
    dur = get_duration(c['path'])
    print(f"  {i}: {os.path.basename(c['path'])} ({dur:.2f}s)")
print(f"  总时长: {total_clip_duration:.2f}s")

# 创建测试音频（故意超过素材总时长）
task_id = str(uuid.uuid4())
audio_path = os.path.join(WORKDIR, f"{task_id}_test_trim_audio.mp3")

# TTS时长 = 素材总时长 * 1.5（故意不足）
tts_duration = total_clip_duration * 1.5

subprocess.run([
    '/home/linuxbrew/.linuxbrew/bin/ffmpeg', '-y',
    '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=mono',
    '-t', str(tts_duration),
    '-c:a', 'libmp3lame', '-b:a', '128k',
    audio_path
], capture_output=True)

print(f"\n测试场景：")
print(f"  TTS 时长: {tts_duration:.2f}s")
print(f"  Clips 总时长: {total_clip_duration:.2f}s")
print(f"  缺口: {tts_duration - total_clip_duration:.2f}s")
print(f"  策略: C（裁剪音频）")

# 创建字幕
srt_path = os.path.join(WORKDIR, f"{task_id}_test.srt")
with open(srt_path, 'w') as f:
    f.write(f"1\n00:00:00,000 --> {format_srt_time(tts_duration)}\n素材不足测试\n\n")

output_path = os.path.join(WORKDIR, f"{task_id}_test_trim_audio_output.mp4")

print(f"\n开始合成验证...")
print(f"task_id: {task_id}")

try:
    assemble_video_audio_driven(
        clips=clips,
        audio_path=audio_path,
        subtitle_path=srt_path,
        output_path=output_path,
        fps=25,
        resolution=(1280, 720),
        trim_audio_if_needed=True  # 允许裁剪音频
    )
    
    if os.path.exists(output_path):
        video_duration = get_duration(output_path)
        
        print(f"\n{'='*60}")
        print(f"✅ 验证成功!")
        print(f"原始TTS时长: {tts_duration:.2f}s")
        print(f"视频时长: {video_duration:.2f}s")
        print(f"音频裁剪: {tts_duration:.2f}s → {video_duration:.2f}s")
        print(f"{'='*60}")
        
except Exception as e:
    print(f"\n❌ 验证失败: {e}")
    import traceback
    traceback.print_exc()