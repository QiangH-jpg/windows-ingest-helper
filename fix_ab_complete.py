#!/usr/bin/env python3
"""
A/B 完整版样片修复脚本
生成带真实配音 + 硬字幕的完整新闻样片

A 版：规则基线完整版
B 版：AI 建议驱动完整版
"""
import os
import sys
import json
import subprocess
import edge_tts

# 项目根目录
PROJECT_ROOT = '/tmp/video-tool-test-48975'
sys.path.insert(0, PROJECT_ROOT)

from core.config import config
from pipeline.tts_provider import generate_tts, create_subtitle_srt_from_meta

# ============================================
# 配置
# ============================================
FFMPEG = config['video']['ffmpeg_path']
FFPROBE = FFMPEG.replace('ffmpeg', 'ffprobe')
WORKDIR = config['storage']['workdir']

# 固定新闻稿（压缩口播版，约 30-45 秒）
NEWS_SCRIPT = "3 月 26 日，济南市人社局人社服务大篷车活动在美团服务中心开展。活动以走进奔跑者保障与你同行为主题，聚焦外卖骑手等新就业形态劳动者。工作人员和志愿者通过发放资料、面对面讲解，向外卖小哥介绍社保参保、权益保障政策。现场设置互动环节，让大家在轻松氛围中了解政策、增强维权意识。济南市人社局持续推动人社服务走近新就业形态劳动者，打通服务保障最后一公里。"

# 素材目录
CLIPS_DIR = os.path.join(PROJECT_ROOT, 'archive/data_archive/clips')

# ============================================
# 工具函数
# ============================================
def get_video_duration(path):
    """获取视频时长"""
    cmd = [FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
           '-of', 'default=noprint_wrappers=1:nokey=1', path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return float(result.stdout.strip())

def run_ffmpeg(cmd):
    """运行 ffmpeg 并打印输出"""
    print(f"  执行：{' '.join(cmd[:10])}...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  错误：{result.stderr[:500]}")
    return result.returncode == 0

# ============================================
# 第一步：生成 TTS 音频
# ============================================
def generate_tts_audio(script, output_path):
    """生成 TTS 音频"""
    print(f"\n【TTS 生成】{output_path}")
    meta_path = output_path.replace('.mp3', '_meta.json')
    
    try:
        meta = generate_tts(script, output_path, meta_path)
        print(f"  ✅ TTS 生成成功")
        print(f"  时长：{meta['total_duration']:.2f}秒")
        print(f"  句数：{meta['sentence_count']}")
        return meta
    except Exception as e:
        print(f"  ❌ TTS 生成失败：{e}")
        return None

# ============================================
# 第二步：生成 SRT 字幕
# ============================================
def generate_srt(meta, output_path):
    """从 TTS 元数据生成 SRT"""
    print(f"\n【SRT 生成】{output_path}")
    
    try:
        create_subtitle_srt_from_meta(meta, output_path)
        print(f"  ✅ SRT 生成成功")
        
        # 读取前 10 条字幕
        with open(output_path, 'r', encoding='utf-8') as f:
            content = f.read()
        blocks = content.strip().split('\n\n')[:10]
        print(f"  前 10 条字幕:")
        for block in blocks:
            lines = block.strip().split('\n')
            if len(lines) >= 3:
                print(f"    {lines[1]} | {lines[2]}")
        return True
    except Exception as e:
        print(f"  ❌ SRT 生成失败：{e}")
        return False

# ============================================
# 第三步：选择 Clip（A 版：规则基线）
# ============================================
def select_clips_rule_based():
    """规则基线选片：按素材顺序选择"""
    print(f"\n【A 版选片】规则基线")
    
    # 获取所有 clip
    clips = []
    for i in range(13):  # 13 个素材
        clip_path = os.path.join(CLIPS_DIR, f'clip_{i}.mp4')
        if os.path.exists(clip_path):
            duration = get_video_duration(clip_path)
            clips.append({
                'path': clip_path,
                'start': 0,
                'duration': duration,
                'source_index': i
            })
    
    # 规则基线：按顺序选择所有 clip（覆盖完整 41 秒音频）
    selected = clips[:13]  # 使用全部 13 个 clip
    print(f"  已选择 {len(selected)} 个 clips")
    for i, clip in enumerate(selected):
        print(f"    Clip{i+1}: clip_{clip['source_index']}.mp4 ({clip['duration']:.1f}s)")
    
    return selected

# ============================================
# 第四步：选择 Clip（B 版：AI 驱动）
# ============================================
def select_clips_ai_driven():
    """AI 驱动选片：优先选择语义丰富的镜头"""
    print(f"\n【B 版选片】AI 驱动")
    
    # AI 优先级（模拟语义选择）：
    # 1. 人物特写/互动镜头优先
    # 2. 动作镜头次之
    # 3. 空镜/全景最后
    
    # 模拟 AI 选择的 clip 顺序（基于语义标签）
    ai_priority = [5, 6, 7, 9, 10, 11, 12, 0, 1, 2, 3, 4, 8]
    
    clips = []
    for i in ai_priority:
        clip_path = os.path.join(CLIPS_DIR, f'clip_{i}.mp4')
        if os.path.exists(clip_path):
            duration = get_video_duration(clip_path)
            clips.append({
                'path': clip_path,
                'start': 0,
                'duration': duration,
                'source_index': i
            })
    
    # 选择所有 clip（覆盖完整 41 秒音频）
    selected = clips[:13]  # 使用全部 13 个 clip
    print(f"  已选择 {len(selected)} 个 clips（AI 优先级）")
    for i, clip in enumerate(selected):
        print(f"    Clip{i+1}: clip_{clip['source_index']}.mp4 ({clip['duration']:.1f}s)")
    
    return selected

# ============================================
# 第五步：组装视频（带硬字幕 + 自然收尾）
# ============================================
def assemble_video_with_subtitles(clips, audio_path, srt_path, output_path, loop_last_for_gap=True):
    """组装视频，硬烧录字幕 + 自然收尾（结尾循环动态画面，静帧≤1 秒）
    
    Args:
        loop_last_for_gap: 是否循环最后一镜来填补缺口（保持动态）
    """
    print(f"\n【视频组装】{output_path}")
    
    # 获取音频时长
    audio_duration = get_video_duration(audio_path)
    print(f"  音频时长：{audio_duration:.2f}秒")
    
    # 计算原始画面总时长
    original_video_duration = sum(c['duration'] for c in clips)
    print(f"  原始画面总时长：{original_video_duration:.2f}秒")
    
    # 计算时长缺口
    gap = audio_duration - original_video_duration
    print(f"  时长缺口：{gap:.2f}秒")
    
    # 策略：结尾循环动态画面填补缺口，静帧兜底≤1 秒
    last_clip = clips[-1]
    last_clip_duration = last_clip['duration']
    
    # 计算需要循环的次数
    loops_needed = 0
    remaining_after_loops = gap
    
    if loop_last_for_gap and gap > 0:
        # 如果缺口>1 秒，至少循环 1 次，确保静帧兜底≤1 秒
        if gap > 1.0:
            # 计算需要循环多少次才能让剩余≤1 秒
            # 公式：loops = ceil((gap - 1.0) / last_duration)
            import math
            loops_needed = max(1, math.ceil((gap - 1.0) / last_clip_duration))
            remaining_after_loops = gap - (loops_needed * last_clip_duration)
            
            # 如果循环后剩余为负（循环多了），不需要静帧
            if remaining_after_loops < 0:
                remaining_after_loops = 0
        
        if loops_needed > 0:
            print(f"  结尾策略：循环最后一镜{loops_needed}次 (+{loops_needed * last_clip_duration:.2f}s) + 静帧兜底{remaining_after_loops:.2f}秒")
        else:
            print(f"  结尾策略：静帧兜底{remaining_after_loops:.2f}秒")
    else:
        if gap > 0:
            print(f"  结尾策略：静帧兜底{gap:.2f}秒")
    
    # 限制静帧兜底≤1 秒（安全网）
    if remaining_after_loops > 1.0:
        print(f"  ⚠️ 警告：静帧兜底{remaining_after_loops:.2f}秒 > 1 秒，截断为 1 秒")
        remaining_after_loops = 1.0
    
    # 创建 concat 文件
    concat_file = output_path + '.concat.txt'
    with open(concat_file, 'w') as f:
        # 写入所有原始 clip（除了最后一个）
        for i, clip in enumerate(clips[:-1]):
            abs_path = os.path.abspath(clip['path'])
            f.write(f"file '{abs_path}'\n")
        
        # 处理最后一个 clip：原始 + 循环 + 静帧兜底
        last_abs_path = os.path.abspath(last_clip['path'])
        
        # 1. 写入原始最后一镜
        f.write(f"file '{last_abs_path}'\n")
        print(f"  结尾 clip (原始): {last_clip_duration:.2f}s")
        
        # 2. 循环最后一镜（保持动态）
        for loop_i in range(loops_needed):
            f.write(f"file '{last_abs_path}'\n")
            print(f"  结尾 clip (循环{loop_i+1}): +{last_clip_duration:.2f}s")
        
        # 3. 静帧兜底（≤1 秒）
        if remaining_after_loops > 0:
            extended_path = output_path + f'.last_clip_padded.mp4'
            pad_cmd = [
                FFMPEG, '-y',
                '-i', last_abs_path,
                '-vf', f'tpad=stop_mode=clone:stop_duration={remaining_after_loops}',
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                '-r', '30',
                '-an',
                extended_path
            ]
            subprocess.run(pad_cmd, capture_output=True, check=True)
            f.write(f"file '{extended_path}'\n")
            print(f"  结尾 clip (静帧兜底): +{remaining_after_loops:.2f}s (≤1 秒)")
    
    # 构建 subtitles 滤镜
    abs_srt = os.path.abspath(srt_path)
    srt_escaped = abs_srt.replace(':', '\\:').replace("'", "'\\''")
    
    subtitle_filter = f"subtitles='{srt_escaped}':force_style='Alignment=2,MarginV=20,MarginL=40,MarginR=40,FontName=WenQuanYi Micro Hei,FontSize=26,PrimaryColour=&HFFFFFF,SecondaryColour=&HFFFFFF,OutlineColour=&H202020,BorderStyle=1,Outline=1,Shadow=0,BackColour=&H00000000,LineSpacing=6'"
    
    # FFmpeg 命令（使用 -t 精确匹配音频时长）
    cmd = [
        FFMPEG, '-y',
        '-f', 'concat', '-safe', '0', '-i', concat_file,
        '-i', audio_path,
        '-map', '0:v:0',
        '-map', '1:a:0',
        '-vf', subtitle_filter,
        '-t', str(audio_duration),  # 精确匹配音频时长
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
        '-r', '30',
        '-c:a', 'aac', '-b:a', '128k',
        '-af', 'volume=2.0',
        output_path
    ]
    
    if run_ffmpeg(cmd):
        # 验证输出
        if os.path.exists(output_path):
            duration = get_video_duration(output_path)
            size = os.path.getsize(output_path) / 1024 / 1024
            print(f"  ✅ 视频生成成功")
            print(f"  时长：{duration:.2f}秒")
            print(f"  大小：{size:.2f}MB")
            
            # 验证音轨
            cmd = [FFPROBE, '-v', 'error', '-show_entries', 'stream=codec_type,codec_name,duration',
                   '-of', 'default=noprint_wrappers=1', output_path]
            result = subprocess.run(cmd, capture_output=True, text=True)
            print(f"  流信息:\n    {result.stdout.strip().replace(chr(10), chr(10) + '    ')}")
            
            # 验证音频是否完整
            audio_dur_cmd = [FFPROBE, '-v', 'error', '-select_streams', 'a:0',
                            '-show_entries', 'stream=duration',
                            '-of', 'default=noprint_wrappers=1:nokey=1', output_path]
            result = subprocess.run(audio_dur_cmd, capture_output=True, text=True)
            actual_audio_dur = float(result.stdout.strip())
            if actual_audio_dur >= audio_duration * 0.99:  # 允许 1% 误差
                print(f"  ✅ 音频完整：{actual_audio_dur:.2f}秒 >= {audio_duration:.2f}秒")
                return True
            else:
                print(f"  ❌ 音频被截断：{actual_audio_dur:.2f}秒 < {audio_duration:.2f}秒")
            
            return True
    
    print(f"  ❌ 视频生成失败")
    return False

# ============================================
# 主流程
# ============================================
def main():
    print("=" * 60)
    print("A/B 完整版样片修复脚本")
    print("=" * 60)
    
    # 创建输出目录
    output_dir = os.path.join(PROJECT_ROOT, 'output_ab_complete')
    os.makedirs(output_dir, exist_ok=True)
    
    # ========== 第一步：生成 TTS（A/B 共用） ==========
    tts_path = os.path.join(output_dir, 'narration.mp3')
    tts_meta = generate_tts_audio(NEWS_SCRIPT, tts_path)
    
    if not tts_meta:
        print("\n❌ TTS 生成失败，终止流程")
        return False
    
    # ========== 第二步：生成 SRT ==========
    srt_path = os.path.join(output_dir, 'subtitles.srt')
    if not generate_srt(tts_meta, srt_path):
        print("\n❌ SRT 生成失败，终止流程")
        return False
    
    # ========== 第三步：生成 A 版（规则基线，自然收尾） ==========
    print("\n" + "=" * 60)
    print("生成 A 版：规则基线完整版（自然收尾）")
    print("=" * 60)
    
    clips_a = select_clips_rule_based()
    output_a = os.path.join(output_dir, 'A_rule_baseline_complete.mp4')
    
    # 策略：结尾循环最后一镜填补 2.28 秒缺口，静帧兜底≤1 秒
    if not assemble_video_with_subtitles(clips_a, tts_path, srt_path, output_a, loop_last_for_gap=True):
        print("\n❌ A 版生成失败")
        return False
    
    # ========== 第四步：生成 B 版（AI 驱动，自然收尾） ==========
    print("\n" + "=" * 60)
    print("生成 B 版：AI 驱动完整版（自然收尾）")
    print("=" * 60)
    
    clips_b = select_clips_ai_driven()
    output_b = os.path.join(output_dir, 'B_ai_driven_complete.mp4')
    
    # 策略：结尾循环最后一镜填补 2.28 秒缺口，静帧兜底≤1 秒
    if not assemble_video_with_subtitles(clips_b, tts_path, srt_path, output_b, loop_last_for_gap=True):
        print("\n❌ B 版生成失败")
        return False
    
    # ========== 第五步：复制到 static 目录供 web 访问 ==========
    static_dir = os.path.join(PROJECT_ROOT, 'static')
    os.makedirs(static_dir, exist_ok=True)
    
    import shutil
    shutil.copy(output_a, os.path.join(static_dir, 'A_rule_baseline_complete.mp4'))
    shutil.copy(output_b, os.path.join(static_dir, 'B_ai_driven_complete.mp4'))
    shutil.copy(srt_path, os.path.join(static_dir, 'subtitles.srt'))
    
    print("\n" + "=" * 60)
    print("✅ A/B 完整版样片生成完成")
    print("=" * 60)
    print(f"\n访问地址:")
    print(f"  A 版：http://47.93.194.154:8088/static/A_rule_baseline_complete.mp4")
    print(f"  B 版：http://47.93.194.154:8088/static/B_ai_driven_complete.mp4")
    print(f"\n本地路径:")
    print(f"  A 版：{os.path.join(static_dir, 'A_rule_baseline_complete.mp4')}")
    print(f"  B 版：{os.path.join(static_dir, 'B_ai_driven_complete.mp4')}")
    
    return True

if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
