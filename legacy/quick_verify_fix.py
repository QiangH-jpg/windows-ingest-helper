#!/usr/bin/env python3
"""
快速验证修复脚本
直接使用已有素材，避免转码等待
"""
import os, sys, uuid, subprocess

sys.path.insert(0, '/home/admin/.openclaw/workspace/video-tool')

from pipeline.audio_driven_timeline import assemble_video_audio_driven, get_duration

# 使用已有的 transcoded 文件或简单素材
WORKDIR = '/home/admin/.openclaw/workspace/video-tool/workdir'

# 找现有的 transcoded 文件
transcoded_files = []
for f in os.listdir(WORKDIR):
    if f.endswith('_transcoded_0.mp4') or f.endswith('_transcoded_1.mp4'):
        transcoded_files.append(os.path.join(WORKDIR, f))

print(f"找到 {len(transcoded_files)} 个 transcoded 文件")

# 使用简单素材作为备选
simple_materials = [
    '/home/admin/.openclaw/workspace/video-tool/uploads/6421dcdc-935c-4dbe-ad7c-293bd20369be.mp4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/d6ae32a5-5467-4091-934c-8050ebc65c67.mp4',
]

# 准备 clips
clips = []
for path in (transcoded_files[:2] if transcoded_files else simple_materials[:2]):
    duration = get_duration(path)
    clips.append({
        'path': path,
        'duration': min(duration, 5.0)  # 限制 5s
    })
    print(f"  clip: {os.path.basename(path)} ({duration:.2f}s)")

# 创建测试 TTS 音频（用 ffmpeg 生成固定时长静音）
task_id = str(uuid.uuid4())
audio_path = os.path.join(WORKDIR, f"{task_id}_test_audio.mp3")

# 生成 10 秒测试音频
subprocess.run([
    '/home/linuxbrew/.linuxbrew/bin/ffmpeg', '-y',
    '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=mono',
    '-t', '10',
    '-c:a', 'libmp3lame', '-b:a', '128k',
    audio_path
], capture_output=True)

print(f"\n测试音频: {audio_path} ({get_duration(audio_path):.2f}s)")
print(f"测试 clips: {len(clips)}")

# 创建空字幕文件
srt_path = os.path.join(WORKDIR, f"{task_id}_test.srt")
with open(srt_path, 'w') as f:
    f.write("1\n00:00:00,000 --> 00:00:10,000\n测试字幕\n\n")

# 输出路径
output_path = os.path.join(WORKDIR, f"{task_id}_test_output.mp4")

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
        keep_concat=True  # 保留 concat 文件以便检查
    )
    
    # 检查结果
    if os.path.exists(output_path):
        video_duration = get_duration(output_path)
        audio_duration = get_duration(audio_path)
        
        print(f"\n{'='*60}")
        print(f"✅ 验证成功!")
        print(f"视频时长: {video_duration:.2f}s")
        print(f"音频时长: {audio_duration:.2f}s")
        print(f"误差: {video_duration - audio_duration:.3f}s")
        print(f"输出: {output_path}")
        print(f"{'='*60}")
        
        # 输出 filter_complex 内容（从 concat.txt 获取）
        concat_file = output_path + '.concat.txt'
        if os.path.exists(concat_file):
            print(f"\nConcat 文件内容:")
            with open(concat_file) as f:
                print(f.read())
        
        # 检查帧时间戳（关键）
        print(f"\n帧时间戳检查（最后 5 帧）:")
        subprocess.run([
            '/home/linuxbrew/.linuxbrew/bin/ffprobe',
            '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'packet=pts_time',
            '-read_intervals', '%+5',  # 最后 5 帧
            '-of', 'csv=p=0',
            output_path
        ])
        
except Exception as e:
    print(f"\n❌ 验证失败: {e}")
    import traceback
    traceback.print_exc()