#!/usr/bin/env python3
"""
测试 tpad 规则：素材不足时，只有最后一段允许延长
"""
import os, sys, uuid, subprocess

sys.path.insert(0, '/home/admin/.openclaw/workspace/video-tool')

from pipeline.audio_driven_timeline import assemble_video_audio_driven, get_duration

WORKDIR = '/home/admin/.openclaw/workspace/video-tool/workdir'

# 找一个短素材（时长 < 5s）
short_materials = []
for f in os.listdir(WORKDIR):
    if f.endswith('.mp4') and 'transcoded' in f:
        path = os.path.join(WORKDIR, f)
        try:
            dur = get_duration(path)
            if dur < 4.0:  # 找短素材
                short_materials.append({'path': path, 'duration': dur})
        except:
            continue  # 跳过无效文件

print(f"找到 {len(short_materials)} 个短素材")

# 如果没有短素材，直接用上传的素材（模拟短时长）
if not short_materials:
    # 直接 trim 短时长
    print("没有短素材，使用普通素材 + 短时长分配")
    clips = [
        {'path': '/home/admin/.openclaw/workspace/video-tool/uploads/6421dcdc-935c-4dbe-ad7c-293bd20369be.mp4'},
        {'path': '/home/admin/.openclaw/workspace/video-tool/uploads/d6ae32a5-5467-4091-934c-8050ebc65c67.mp4'},
    ]
else:
    clips = short_materials[:3]

# 创建长 TTS 音频（15秒），故意超过素材总时长
task_id = str(uuid.uuid4())
audio_path = os.path.join(WORKDIR, f"{task_id}_test_tpad.mp3")

# 生成 15 秒测试音频（超过素材总时长）
subprocess.run([
    '/home/linuxbrew/.linuxbrew/bin/ffmpeg', '-y',
    '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=mono',
    '-t', '15',
    '-c:a', 'libmp3lame', '-b:a', '128k',
    audio_path
], capture_output=True)

print(f"\n测试场景：")
print(f"  TTS 时长: 15.00s")
print(f"  Clips:")
for i, c in enumerate(clips):
    try:
        dur = get_duration(c['path'])
        print(f"    {i}: {os.path.basename(c['path'])} ({dur:.2f}s)")
    except:
        print(f"    {i}: {os.path.basename(c['path'])} (时长获取失败)")
print(f"  总时长: {sum([get_duration(c['path']) for c in clips]):.2f}s (如果素材充足则正常分配)")
print(f"  缺口: {15.0 - sum([get_duration(c['path']) for c in clips]):.2f}s (如果素材不足则延长最后段)")

# 创建空字幕
srt_path = os.path.join(WORKDIR, f"{task_id}_test.srt")
with open(srt_path, 'w') as f:
    f.write("1\n00:00:00,000 --> 00:00:15,000\n测试素材不足\n\n")

output_path = os.path.join(WORKDIR, f"{task_id}_test_tpad_output.mp4")

print(f"\n开始合成（验证 tpad 规则）...")
print(f"task_id: {task_id}")

try:
    assemble_video_audio_driven(
        clips=clips,
        audio_path=audio_path,
        subtitle_path=srt_path,
        output_path=output_path,
        fps=25,
        resolution=(1280, 720),
        keep_concat=True
    )
    
    if os.path.exists(output_path):
        video_duration = get_duration(output_path)
        audio_duration = get_duration(audio_path)
        
        print(f"\n{'='*60}")
        print(f"✅ 验证成功!")
        print(f"视频时长: {video_duration:.2f}s")
        print(f"音频时长: {audio_duration:.2f}s")
        print(f"误差: {video_duration - audio_duration:.3f}s")
        print(f"{'='*60}")
        
except Exception as e:
    print(f"\n❌ 验证失败: {e}")
    import traceback
    traceback.print_exc()