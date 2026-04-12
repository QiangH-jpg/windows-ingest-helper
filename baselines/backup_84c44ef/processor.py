import os
import subprocess
import json
import edge_tts
import sys
import threading

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# ============================================
# 缓存强制保护机制
# ============================================
_cache_context = threading.local()
_cache_context.active = False

def enter_cache_context():
    """进入缓存上下文（由 video_cache 调用）"""
    _cache_context.active = True

def exit_cache_context():
    """退出缓存上下文"""
    _cache_context.active = False

def _check_cache_protection(func_name):
    """检查是否通过缓存入口"""
    if not getattr(_cache_context, 'active', False):
        raise RuntimeError(
            f"\n"
            f"{'='*60}\n"
            f"❌ 缓存保护违规！\n"
            f"{'='*60}\n"
            f"函数 {func_name}() 被直接调用，绕过了缓存机制。\n"
            f"\n"
            f"正确调用方式:\n"
            f"  from pipeline.video_cache import get_or_create_processed\n"
            f"  from pipeline.video_cache import get_processed_clips\n"
            f"\n"
            f"  processed_path = get_or_create_processed(source_path)\n"
            f"  clips = get_processed_clips(source_path, ...)\n"
            f"\n"
            f"禁止直接调用:\n"
            f"  processor.transcode_to_h264()  ❌\n"
            f"  processor.extract_clips()      ❌\n"
            f"{'='*60}"
        )

# ============================================
# MVP 质量收口统一参数
# ============================================

# 视频输出参数
VIDEO_OUTPUT_WIDTH = 1280  # 统一宽度 1280x720
VIDEO_OUTPUT_HEIGHT = 720
VIDEO_FPS = 30  # 统一 30fps（禁止 59.94）
VIDEO_CRF = 23  # 统一 crf 23

# 音频参数
AUDIO_VOLUME = 2.0  # 音量提升 6dB
AUDIO_SAMPLE_RATE = 44100  # 统一 44.1kHz

# 字幕参数（drawtext）
SUBTITLE_FONT = '/usr/share/fonts/wqy-microhei/wqy-microhei.ttc'  # 统一字体
SUBTITLE_FONTSIZE = 48  # 字号
SUBTITLE_COLOR = 'white'  # 白色
SUBTITLE_BORDER = 2  # 描边宽度
SUBTITLE_X = '(w-text_w)/2'  # 底部居中
SUBTITLE_Y = 'h-100'  # 距离底部 100px

# 时长控制
TARGET_DURATION_MIN = 8
TARGET_DURATION_MAX = 15

# 文件大小限制
MAX_OUTPUT_SIZE_MB = 20

# ============================================

from core.config import config

STORAGE = config['storage']
VIDEO = config['video']
TTS = config['tts']

def get_video_duration(path):
    """Get video duration in seconds using ffprobe"""
    if not os.path.exists(path):
        raise Exception(f"File not found: {path}")
    
    # Use ffprobe instead of ffmpeg
    ffprobe_path = VIDEO['ffmpeg_path'].replace('ffmpeg', 'ffprobe')
    cmd = [
        ffprobe_path, '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout.strip()
    if not output:
        raise Exception(f"ffprobe failed for {path}: {result.stderr}")
    return float(output)

def transcode_to_h264(input_path, output_path):
    """Transcode video to H.264 MP4
    
    ⚠️ 缓存保护：禁止直接调用，必须通过 video_cache.get_or_create_processed()
    
    ✅ P0 修复：添加 scale 滤镜强制 1280x720 分辨率
    """
    _check_cache_protection('transcode_to_h264')
    
    # ✅ P0 修复：强制 1280x720 分辨率
    cmd = [
        VIDEO['ffmpeg_path'], '-y',
        '-i', input_path,
        '-vf', f'scale={VIDEO_OUTPUT_WIDTH}:{VIDEO_OUTPUT_HEIGHT}:force_original_aspect_ratio=decrease,pad={VIDEO_OUTPUT_WIDTH}:{VIDEO_OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2',
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
        '-r', str(VIDEO_FPS),  # ✅ 强制 30fps
        '-c:a', 'aac', '-b:a', '128k',
        '-movflags', '+faststart',
        output_path
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return output_path

def extract_clips(input_path, clip_duration=5):
    """Extract fixed-duration clips from video
    
    ⚠️ 缓存保护：禁止直接调用，必须通过 video_cache.get_processed_clips()
    """
    _check_cache_protection('extract_clips')
    
    duration = get_video_duration(input_path)
    clips = []
    i = 0
    while i * clip_duration < duration:
        start = i * clip_duration
        output_path = f"{input_path}.clip_{i}.mp4"
        # Use re-encoding instead of copy to handle variable keyframe intervals
        # ✅ P0 修复：添加帧率参数，确保输出 ≥ 25fps
        cmd = [
            VIDEO['ffmpeg_path'], '-y',
            '-i', input_path,
            '-ss', str(start),
            '-t', str(clip_duration),
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
            '-r', str(VIDEO_FPS),  # ✅ 强制 30fps
            '-c:a', 'aac', '-b:a', '128k',
            output_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            clips.append({'start': start, 'duration': clip_duration, 'path': output_path})
        i += 1
    return clips

async def generate_tts(text, output_path):
    """Generate TTS audio using edge-tts, splitting long text into chunks"""
    import re
    
    # Split text into sentences for better TTS reliability
    sentences = re.split(r'[。！？!?]', text)
    sentences = [s.strip() + '。' for s in sentences if s.strip()]
    
    if not sentences:
        # Fallback to original text
        sentences = [text]
    
    chunk_files = []
    
    # Generate TTS for each sentence
    for i, sentence in enumerate(sentences):
        chunk_path = f"{output_path}.chunk_{i}.mp3"
        try:
            communicate = edge_tts.Communicate(sentence, TTS['voice'], rate=TTS.get('rate', '+0%'))
            await communicate.save(chunk_path)
            if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 0:
                chunk_files.append(chunk_path)
            else:
                os.remove(chunk_path) if os.path.exists(chunk_path) else None
        except Exception as e:
            print(f"TTS chunk {i} failed: {e}")
            # Skip failed chunks
    
    if not chunk_files:
        raise Exception("No audio was received. Please verify that your parameters are correct.")
    
    # Concatenate all chunks using ffmpeg
    concat_file = output_path + '.concat.txt'
    with open(concat_file, 'w') as f:
        for chunk in chunk_files:
            f.write(f"file '{chunk}'\n")
    
    cmd = [
        VIDEO['ffmpeg_path'], '-y',
        '-f', 'concat', '-safe', '0', '-i', concat_file,
        '-c', 'copy',
        output_path
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    
    # Cleanup chunk files
    for chunk in chunk_files:
        os.remove(chunk)
    os.remove(concat_file)
    
    return output_path

def create_subtitle_srt(text, audio_path, output_path):
    """Create SRT subtitle from text, aligned with audio duration"""
    # Get actual audio duration for precise alignment
    audio_duration = get_video_duration(audio_path)
    
    # Split text into sentences (by Chinese punctuation)
    import re
    sentences = re.split(r'[。！？!?]', text)
    sentences = [s.strip() for s in sentences if s.strip()]
    
    # Calculate time per sentence
    time_per_sentence = audio_duration / max(1, len(sentences))
    
    with open(output_path, 'w', encoding='utf-8') as f:
        for idx, sentence in enumerate(sentences):
            start_time = idx * time_per_sentence
            end_time = min((idx + 1) * time_per_sentence, audio_duration)
            
            f.write(f"{idx + 1}\n")
            f.write(f"{format_srt_time(start_time)} --> {format_srt_time(end_time)}\n")
            f.write(f"{sentence}\n\n")
    return output_path

def format_srt_time(seconds):
    """Format seconds to SRT time format"""
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{ms:03d}"

def build_drawtext_filter(srt_path, font_path='/usr/share/fonts/wqy-microhei/wqy-microhei.ttc', 
                           fontsize=48, fontcolor='white', x='(w-text_w)/2', y='h-100'):
    """
    从 SRT 文件构建 drawtext 滤镜链
    
    ✅ 修复渲染方框问题：
    1. 强制清洗行尾字符（去除\r、\t、零宽字符）
    2. 正确处理换行符（\n → \n）
    3. 完整转义特殊字符
    
    Args:
        srt_path: SRT 字幕文件路径
        font_path: 中文字体文件路径
        fontsize: 字体大小
        fontcolor: 字体颜色
        x, y: 字幕位置
    
    Returns:
        drawtext filter 字符串
    """
    if not os.path.exists(srt_path):
        return None
    
    # 解析 SRT
    subtitles = _parse_srt(srt_path)
    if not subtitles:
        return None
    
    # 构建 drawtext 滤镜链
    filters = []
    for sub in subtitles:
        text = sub['text']
        
        # === 1. 强制清洗行尾字符 ===
        # 去除每行末尾的\r、\t、零宽字符、不可见空格
        lines = text.split('\n')
        cleaned_lines = []
        for line in lines:
            # 去除右侧空白和控制字符
            line = line.rstrip('\r\t')
            # 去除零宽字符（U+200B-U+200F, U+FEFF 等）
            line = ''.join(c for c in line if not (0x200B <= ord(c) <= 0x200F or ord(c) == 0xFEFF))
            # 去除不可见空格（U+00A0 等）
            line = line.replace('\u00a0', ' ')
            # 标准 strip
            line = line.strip()
            if line:
                cleaned_lines.append(line)
        
        text = '\\n'.join(cleaned_lines)
        
        # === 2. 正确转义（ffmpeg drawtext 要求）===
        # 关键：ffmpeg drawtext 使用 %{n} 表示换行，不是 \n！
        # 参考：https://ffmpeg.org/ffmpeg-filters.html#drawtext
        
        # 步骤 1：先处理换行（用临时标记）
        text = text.replace('\n', '###NEWLINE###')
        
        # 步骤 2：转义其他特殊字符
        text = text.replace('\\', '\\\\')  # 反斜杠
        text = text.replace("'", "\\'")    # 单引号
        text = text.replace(':', '\\:')    # 冒号
        text = text.replace('%', '\\%')    # 百分号
        text = text.replace('{', '\\{')    # 花括号
        text = text.replace('}', '\\}')    # 花括号
        text = text.replace('$', '\\$')    # 美元符号
        text = text.replace('"', '\\"')    # 双引号
        
        # 步骤 3：恢复换行符（ffmpeg 用%{n}表示换行）
        text = text.replace('###NEWLINE###', '%{n}')
        
        # === 3. 输出字符调试信息（仅第一条字幕）===
        if len(filters) == 0:
            print(f"\n【字幕渲染调试】")
            print(f"  原始文本：{repr(sub['text'])}")
            print(f"  清洗后：{repr(text)}")
            for i, line in enumerate(cleaned_lines):
                print(f"  行{i+1}末尾 3 字符：{[hex(ord(c)) for c in line[-3:]] if len(line) >= 3 else [hex(ord(c)) for c in line]}")
        
        start = sub['start']
        end = sub['end']
        
        # 使用 enable='between(t,start,end)' 控制显示时间
        drawtext = f"drawtext=fontfile={font_path}:fontsize={fontsize}:fontcolor={fontcolor}:text='{text}':x={x}:y={y}:enable='between(t,{start},{end})'"
        filters.append(drawtext)
    
    return ','.join(filters)


def assemble_video(clips, audio_path, subtitle_path, output_path, target_duration=40, keep_concat=False):
    """Assemble clips with TTS audio + subtitles filter (ASS/SRT 正式方案)
    
    ✅ 字幕渲染链：
    - 连续视频：直接 ffmpeg concat，不抽帧
    - 字幕：使用 subtitles 滤镜烧录 SRT（支持真正多行）
    - TTS 音轨：使用 -map 1:a:0 明确指定
    
    ✅ P0 修复：使用 -shortest 参数确保视频时长与音频对齐
    """
    # Create concat file for clips
    concat_file = output_path + '.concat.txt'
    with open(concat_file, 'w') as f:
        for clip in clips:
            clip_path = clip['path']
            if not clip_path.startswith('/'):
                clip_path = os.path.abspath(clip_path)
            f.write(f"file '{clip_path}'\n")
    
    # 构建 subtitles 滤镜（正式方案）
    subtitle_filter = None
    if subtitle_path and os.path.exists(subtitle_path):
        # subtitles 滤镜支持真正的多行字幕
        # 使用绝对路径 + 正确的 ffmpeg 转义
        abs_subtitle_path = os.path.abspath(subtitle_path)
        # ffmpeg subtitles 滤镜路径转义：冒号用\:，单引号用\'
        srt_path_escaped = abs_subtitle_path.replace(':', '\\:').replace("'", "'\\''")
        subtitle_filter = f"subtitles='{srt_path_escaped}':force_style='Alignment=2,MarginV=20,MarginL=40,MarginR=40,FontName=WenQuanYi Micro Hei,FontSize=26,PrimaryColour=&HFFFFFF,SecondaryColour=&HFFFFFF,OutlineColour=&H202020,BorderStyle=1,Outline=1,Shadow=0,BackColour=&H00000000,LineSpacing=6'"
        
        print(f"\n【字幕渲染】使用 subtitles 滤镜")
        print(f"  SRT 路径：{abs_subtitle_path}")
        print(f"  转义后：{srt_path_escaped}")
        print(f"  滤镜：{subtitle_filter[:100]}...")
    
    # 构建 ffmpeg 命令
    cmd = [
        VIDEO['ffmpeg_path'], '-y',
        '-f', 'concat', '-safe', '0', '-i', concat_file,
        '-i', audio_path,
    ]
    
    # 流映射
    cmd.extend(['-map', '0:v:0', '-map', '1:a:0'])
    
    # 添加 subtitles 滤镜
    if subtitle_filter:
        cmd.extend(['-vf', subtitle_filter])
    
    # ✅ P0 修复：使用 -shortest 确保输出时长与最短流（通常是音频）对齐
    cmd.extend([
        '-shortest',  # ✅ 输出时长与音频对齐
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
        '-r', str(VIDEO_FPS),  # ✅ 强制 30fps
        '-c:a', 'aac', '-b:a', '128k',
        '-af', 'volume=2.0',
        output_path
    ])
    
    subprocess.run(cmd, check=True, capture_output=True)
    
    # Cleanup
    if not keep_concat and os.path.exists(concat_file):
        os.remove(concat_file)
    
    return output_path


def _parse_srt(srt_path):
    """Parse SRT file"""
    subtitles = []
    try:
        with open(srt_path, 'r', encoding='utf-8') as f:
            content = f.read()
        for block in content.strip().split('\n\n'):
            lines = block.strip().split('\n')
            if len(lines) < 3:
                continue
            start, end = lines[1].split(' --> ')
            subtitles.append({
                'start': _parse_time(start),
                'end': _parse_time(end),
                'text': '\n'.join(lines[2:])
            })
    except:
        pass
    return subtitles

def _parse_time(t):
    """Parse SRT time to seconds"""
    t = t.replace(',', '.')
    h, m, s = t.split(':')
    return int(h)*3600 + int(m)*60 + float(s)
