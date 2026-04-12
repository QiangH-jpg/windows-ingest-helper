#!/usr/bin/env python3
"""
视频流字幕方案 - 尝试使用 ASS 格式字幕
需要先把 SRT 转为 ASS，然后用 ffmpeg 烧录
"""
import os
import sys
import subprocess

def srt_to_ass(srt_path, ass_path, font_name="WenQuanYi Micro Hei", font_size=56):
    """把 SRT 转为 ASS 格式"""
    
    # 读取 SRT
    with open(srt_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # ASS 文件头
    ass_header = """[Script Info]
Title: Chinese Subtitles
ScriptType: v4.00+
Collisions: Normal
PlayDepth: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},56,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""".format(font=font_name)
    
    # 解析 SRT 并转为 ASS 格式
    events = []
    for block in content.strip().split('\n\n'):
        lines = block.strip().split('\n')
        if len(lines) < 3:
            continue
        
        start = lines[1].replace(',', '.')
        end = lines[1].split(' --> ')[1].replace(',', '.')
        text = '\\N'.join(lines[2:])  # ASS 用 \N 换行
        
        # 转换时间格式
        start_ass = srt_to_ass_time(start)
        end_ass = srt_to_ass_time(end)
        
        events.append(f"Dialogue: 0,{start_ass},{end_ass},Default,,0,0,0,,{text}")
    
    # 写入 ASS 文件
    with open(ass_path, 'w', encoding='utf-8') as f:
        f.write(ass_header + '\n'.join(events))

def srt_to_ass_time(srt_time):
    """SRT时间转ASS时间"""
    # 00:00:00.000 -> 0:00:00.00
    parts = srt_time.split(':')
    return f"{parts[0]}:{parts[1]}:{parts[2]}"

def burn_ass_subtitles(video_path, ass_path, output_path, font_path=None):
    """使用 ffmpeg 烧录 ASS 字幕"""
    
    cmd = [
        '/home/linuxbrew/.linuxbrew/bin/ffmpeg', '-y',
        '-i', video_path,
        '-vf', f"ass={ass_path}",
        '-c:a', 'copy',
        output_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ASS burn failed: {result.stderr[-500:]}")
        return False
    return True

# 测试
if __name__ == '__main__':
    import uuid
    
    # 测试 SRT 转 ASS
    srt_path = '/home/admin/.openclaw/workspace/video-tool/workdir/82efe88a.srt'
    ass_path = f'/tmp/test_{uuid.uuid4()}.ass'
    
    if os.path.exists(srt_path):
        srt_to_ass(srt_path, ass_path)
        print(f"ASS created: {ass_path}")
        
        # 检查内容
        with open(ass_path, 'r') as f:
            print(f.read()[:500])
    else:
        print(f"SRT not found: {srt_path}")