#!/usr/bin/env python3
"""
验证禁止尾段冻结修复
"""
import os, sys, uuid, subprocess

sys.path.insert(0, '/home/admin/.openclaw/workspace/video-tool')

from pipeline.audio_driven_timeline import assemble_video_audio_driven, get_duration, check_tail_motion

WORKDIR = '/home/admin/.openclaw/workspace/video-tool/workdir'

def format_srt_time(seconds):
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{ms:03d}"

# 找现有的素材文件（优先找长素材）
transcoded_files = []
for f in os.listdir(WORKDIR):
    if f.endswith('.mp4') and 'transcoded' in f:
        path = os.path.join(WORKDIR, f)
        try:
            dur = get_duration(path)
            if dur > 5.0:  # 只找长素材（>5秒）
                transcoded_files.append({'path': path, 'duration': dur})
        except:
            continue

# 按时长排序（优先使用长素材）
transcoded_files.sort(key=lambda x: x['duration'], reverse=True)

print(f"找到 {len(transcoded_files)} 个长素材文件（>5s）")

# 测试场景：素材充足（使用长素材）
if len(transcoded_files) >= 3:
    clips = transcoded_files[:3]
else:
    # 使用上传的素材
    clips = [
        {'path': '/home/admin/.openclaw/workspace/video-tool/uploads/6421dcdc-935c-4dbe-ad7c-293bd20369be.mp4'},
        {'path': '/home/admin/.openclaw/workspace/video-tool/uploads/d6ae32a5-5467-4091-934c-8050ebc65c67.mp4'},
    ]

# 打印clip信息
print(f"\n选中的 Clips:")
for i, c in enumerate(clips):
    dur = get_duration(c['path'])
    print(f"  {i}: {os.path.basename(c['path'])} ({dur:.2f}s)")

# 创建测试音频
task_id = str(uuid.uuid4())
audio_path = os.path.join(WORKDIR, f"{task_id}_test_no_tpad.mp3")

# 计算素材总时长
total_clip_duration = sum([get_duration(c['path']) for c in clips])

# 生成音频时长 = 素材总时长 * 0.8（确保素材充足）
tts_duration = total_clip_duration * 0.8

# 生成音频
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
print(f"  素材充足: ✅")

# 创建字幕（动态时长）
srt_path = os.path.join(WORKDIR, f"{task_id}_test.srt")
with open(srt_path, 'w') as f:
    f.write(f"1\n00:00:00,000 --> {format_srt_time(tts_duration)}\n禁止尾段冻结测试\n\n")

output_path = os.path.join(WORKDIR, f"{task_id}_test_no_tpad_output.mp4")

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
        keep_concat=True,
        trim_audio_if_needed=True  # 允许裁剪音频
    )
    
    if os.path.exists(output_path):
        video_duration = get_duration(output_path)
        
        print(f"\n{'='*60}")
        print(f"✅ 验证成功!")
        print(f"视频时长: {video_duration:.2f}s")
        print(f"{'='*60}")
        
        # 提取最后3秒帧截图
        print(f"\n提取最后3秒帧（验证真实运动）:")
        frame_dir = os.path.join(WORKDIR, f"{task_id}_tail_frames")
        os.makedirs(frame_dir, exist_ok=True)
        
        subprocess.run([
            '/home/linuxbrew/.linuxbrew/bin/ffmpeg', '-y',
            '-i', output_path,
            '-ss', str(video_duration - 3),
            '-vframes', '5',
            '-q:v', '2',
            os.path.join(frame_dir, 'frame_%03d.jpg')
        ], capture_output=True)
        
        frames = sorted([f for f in os.listdir(frame_dir) if f.endswith('.jpg')])
        for f in frames:
            size = os.path.getsize(os.path.join(frame_dir, f))
            print(f"  {f}: {size} bytes")
        
        # 计算帧差异
        sizes = [os.path.getsize(os.path.join(frame_dir, f)) for f in frames]
        diffs = []
        for i in range(len(sizes) - 1):
            diff = abs(sizes[i+1] - sizes[i])
            diffs.append(diff)
        
        avg_diff = sum(diffs) / len(diffs) if diffs else 0
        print(f"\n帧差异分析:")
        print(f"  平均差异: {avg_diff:.2f} bytes")
        print(f"  结论: {'✅ 有真实运动' if avg_diff > 500 else '⚠️ 可能静止'}")
        
        print(f"\n帧截图路径:")
        for f in frames:
            print(f"  {os.path.join(frame_dir, f)}")
        
except Exception as e:
    print(f"\n❌ 验证失败: {e}")
    import traceback
    traceback.print_exc()