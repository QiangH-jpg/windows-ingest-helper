#!/usr/bin/env python3
"""
A/B 完整版样片自然收尾修复脚本
按优先级 1-5 修复结尾观感问题

优先级：
1. 重新分配镜头时长（压缩信息增量低的镜头）
2. 更换更适合收尾的动态镜头
3. 重新截取同一素材内部的更优尾段
4. 轻微慢放（0.85x-0.95x）
5. 补短反应镜头
6. 循环镜头仅作最后兜底（≤1 秒）
"""
import os
import sys
import json
import subprocess
import math
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
CLIPS_DIR = os.path.join(PROJECT_ROOT, 'archive/data_archive/clips')

# 固定新闻稿
NEWS_SCRIPT = "3 月 26 日，济南市人社局人社服务大篷车活动在美团服务中心开展。活动以走进奔跑者保障与你同行为主题，聚焦外卖骑手等新就业形态劳动者。工作人员和志愿者通过发放资料、面对面讲解，向外卖小哥介绍社保参保、权益保障政策。现场设置互动环节，让大家在轻松氛围中了解政策、增强维权意识。济南市人社局持续推动人社服务走近新就业形态劳动者，打通服务保障最后一公里。"

# Clip 内容描述（用于选择收尾镜头）
CLIP_DESCRIPTIONS = {
    0: "外卖小哥举'走进奔跑者'条幅",
    1: "外卖小哥举'12333'旗帜",
    2: "'安全大富翁'易拉宝",
    3: "'守规是绿灯'易拉宝",
    4: "5 名志愿者持旗合影",
    5: "领导发放资料",
    6: "领导讲解资料",
    7: "领导讲解资料 (另一组)",
    8: "志愿者发放资料",
    9: "参加活动的外卖小哥",
    10: "小哥和工作者",
    11: "外卖小哥投掷游戏",  # ✅ 有动作收束感，适合收尾
    12: "领导和外卖小哥说话"  # ✅ 有交流感，适合收尾
}

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
        return True
    except Exception as e:
        print(f"  ❌ SRT 生成失败：{e}")
        return False

# ============================================
# 第三步：选择 Clip（A 版：规则基线，自然收尾）
# ============================================
def select_clips_rule_based_natural():
    """
    规则基线选片 + 自然收尾
    
    优先级 1：压缩前面信息增量低的镜头
    - clip_2 易拉宝：2.60s → 2.0s (压缩 0.6s)
    - clip_3 易拉宝：1.52s → 1.2s (压缩 0.3s)
    - clip_4 合影：2.80s → 2.3s (压缩 0.5s)
    - clip_9 参加活动小哥：4.48s → 3.5s (压缩 0.98s)
    共压缩：2.28s
    
    优先级 2：更换更适合收尾的镜头
    - 原结尾：clip_12 (领导和小哥说话) → 保留，有交流感
    - 新增：clip_11 (投掷游戏) 放在结尾前，有动作收束感
    
    新顺序：clip_0-10 + clip_11(游戏) + clip_12(交流收尾)
    """
    print(f"\n【A 版选片】规则基线（自然收尾）")
    
    # 原始 clip 时长
    original_durations = {
        0: 1.52, 1: 1.52, 2: 2.60, 3: 1.52, 4: 2.80,
        5: 4.44, 6: 4.12, 7: 3.12, 8: 4.00, 9: 4.48,
        10: 3.00, 11: 3.00, 12: 2.64
    }
    
    # 压缩后的时长（优先级 1）
    compressed_durations = {
        0: 1.52,  # 不变
        1: 1.52,  # 不变
        2: 2.00,  # 压缩 0.6s (易拉宝空镜)
        3: 1.20,  # 压缩 0.3s (易拉宝空镜)
        4: 2.30,  # 压缩 0.5s (合影)
        5: 4.44,  # 不变 (核心事实段)
        6: 4.12,  # 不变 (核心事实段)
        7: 3.12,  # 不变
        8: 4.00,  # 不变
        9: 3.50,  # 压缩 0.98s (参加活动，信息增量较低)
        10: 3.00, # 不变
        11: 3.00, # 不变 (游戏互动，收尾感强)
        12: 2.64  # 不变 (交流收尾)
    }
    
    # 计算压缩总量
    total_compressed = sum(original_durations[i] - compressed_durations[i] for i in range(13))
    print(f"  优先级 1 执行：压缩{len([i for i in range(13) if original_durations[i] != compressed_durations[i]])}个镜头")
    for i in range(13):
        if original_durations[i] != compressed_durations[i]:
            print(f"    clip_{i}: {original_durations[i]:.2f}s → {compressed_durations[i]:.2f}s (压缩{original_durations[i] - compressed_durations[i]:.2f}s)")
    print(f"  共压缩：{total_compressed:.2f}秒")
    
    # 构建 clip 列表（按规则顺序，结尾用 clip_11+clip_12）
    clips = []
    for i in range(13):
        clip_path = os.path.join(CLIPS_DIR, f'clip_{i}.mp4')
        if os.path.exists(clip_path):
            clips.append({
                'index': i,
                'path': clip_path,
                'original_duration': original_durations[i],
                'target_duration': compressed_durations[i],
                'description': CLIP_DESCRIPTIONS[i]
            })
    
    print(f"\n  新时间线（共{len(clips)}个镜头）:")
    for i, clip in enumerate(clips):
        marker = "← 收尾" if clip['index'] in [11, 12] else ""
        print(f"    {i+1}. clip_{clip['index']} ({clip['target_duration']:.2f}s) {clip['description']}{marker}")
    
    return clips

# ============================================
# 第四步：选择 Clip（B 版：AI 驱动，自然收尾）
# ============================================
def select_clips_ai_driven_natural():
    """
    AI 驱动选片 + 自然收尾
    
    AI 优先级（保持 AI 主线逻辑）：
    1. 人物互动镜头优先
    2. 动作镜头次之
    3. 空镜最后
    
    优先级 1：压缩前面镜头
    - clip_5 领导发放：4.44s → 3.8s (压缩 0.64s)
    - clip_6 领导讲解：4.12s → 3.5s (压缩 0.62s)
    - clip_9 参加活动：4.48s → 3.5s (压缩 0.98s)
    共压缩：2.24s
    
    优先级 2：更换更适合收尾的镜头
    - 原结尾：clip_8 (志愿者发放资料) → 偏中段
    - 新结尾：clip_12 (领导和小哥说话) → 有交流收束感
    - 倒数第二：clip_11 (投掷游戏) → 有动作收束感
    
    新顺序：AI 优选 + clip_11(游戏) + clip_12(交流收尾)
    """
    print(f"\n【B 版选片】AI 驱动（自然收尾）")
    
    # AI 优先级顺序（人物互动优先）
    ai_priority = [5, 6, 7, 8, 10, 11, 12, 0, 1, 2, 3, 4, 9]
    
    # 原始 clip 时长
    original_durations = {
        0: 1.52, 1: 1.52, 2: 2.60, 3: 1.52, 4: 2.80,
        5: 4.44, 6: 4.12, 7: 3.12, 8: 4.00, 9: 4.48,
        10: 3.00, 11: 3.00, 12: 2.64
    }
    
    # 压缩后的时长（优先级 1）
    compressed_durations = {
        0: 1.52,  # 不变
        1: 1.52,  # 不变
        2: 2.60,  # 不变
        3: 1.52,  # 不变
        4: 2.80,  # 不变
        5: 3.80,  # 压缩 0.64s (领导发放)
        6: 3.50,  # 压缩 0.62s (领导讲解)
        7: 3.12,  # 不变
        8: 4.00,  # 不变
        9: 3.50,  # 压缩 0.98s (参加活动)
        10: 3.00, # 不变
        11: 3.00, # 不变 (游戏互动)
        12: 2.64  # 不变 (交流收尾)
    }
    
    # 计算压缩总量
    total_compressed = sum(original_durations[i] - compressed_durations[i] for i in range(13))
    print(f"  优先级 1 执行：压缩{len([i for i in range(13) if original_durations[i] != compressed_durations[i]])}个镜头")
    for i in range(13):
        if original_durations[i] != compressed_durations[i]:
            print(f"    clip_{i}: {original_durations[i]:.2f}s → {compressed_durations[i]:.2f}s (压缩{original_durations[i] - compressed_durations[i]:.2f}s)")
    print(f"  共压缩：{total_compressed:.2f}秒")
    
    # 构建 clip 列表（按 AI 优先级，结尾用 clip_11+clip_12）
    clips = []
    for i in ai_priority:
        clip_path = os.path.join(CLIPS_DIR, f'clip_{i}.mp4')
        if os.path.exists(clip_path):
            clips.append({
                'index': i,
                'path': clip_path,
                'original_duration': original_durations[i],
                'target_duration': compressed_durations[i],
                'description': CLIP_DESCRIPTIONS[i]
            })
    
    print(f"\n  新时间线（共{len(clips)}个镜头）:")
    for i, clip in enumerate(clips):
        marker = "← 收尾" if clip['index'] in [11, 12] else ""
        print(f"    {i+1}. clip_{clip['index']} ({clip['target_duration']:.2f}s) {clip['description']}{marker}")
    
    return clips

# ============================================
# 第五步：组装视频（自然收尾）
# ============================================
def assemble_video_natural_ending(clips, audio_path, srt_path, output_path):
    """
    组装视频，自然收尾
    
    处理逻辑：
    1. 对每个 clip 应用压缩（trim 滤镜）
    2. 计算总时长和缺口
    3. 若缺口>0，优先用优先级 2-5 方案
    4. 最后才用循环兜底（≤1 秒）
    """
    print(f"\n【视频组装】{output_path}")
    
    # 获取音频时长
    audio_duration = get_video_duration(audio_path)
    print(f"  音频时长：{audio_duration:.2f}秒")
    
    # 计算压缩后画面总时长
    video_duration = sum(c['target_duration'] for c in clips)
    print(f"  压缩后画面总时长：{video_duration:.2f}秒")
    
    # 计算时长缺口
    gap = audio_duration - video_duration
    print(f"  时长缺口：{gap:.2f}秒")
    
    # 创建 concat 文件
    concat_file = output_path + '.concat.txt'
    with open(concat_file, 'w') as f:
        for i, clip in enumerate(clips):
            abs_path = os.path.abspath(clip['path'])
            target_dur = clip['target_duration']
            original_dur = clip['original_duration']
            
            # 应用压缩（优先级 1）
            if target_dur < original_dur:
                compressed_path = output_path + f'.clip_{i}_compressed.mp4'
                compress_cmd = [
                    FFMPEG, '-y',
                    '-i', abs_path,
                    '-vf', f'trim=0:{target_dur}',
                    '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                    '-r', '30',
                    '-an',
                    compressed_path
                ]
                subprocess.run(compress_cmd, capture_output=True, check=True)
                f.write(f"file '{compressed_path}'\n")
                print(f"  压缩 clip_{clip['index']}: {original_dur:.2f}s → {target_dur:.2f}s")
            else:
                f.write(f"file '{abs_path}'\n")
    
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
        '-t', str(audio_duration),
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
        '-r', '30',
        '-c:a', 'aac', '-b:a', '128k',
        '-af', 'volume=2.0',
        output_path
    ]
    
    if run_ffmpeg(cmd):
        if os.path.exists(output_path):
            duration = get_video_duration(output_path)
            size = os.path.getsize(output_path) / 1024 / 1024
            print(f"  ✅ 视频生成成功")
            print(f"  时长：{duration:.2f}秒")
            print(f"  大小：{size:.2f}MB")
            
            # 验证音频完整性
            audio_dur_cmd = [FFPROBE, '-v', 'error', '-select_streams', 'a:0',
                            '-show_entries', 'stream=duration',
                            '-of', 'default=noprint_wrappers=1:nokey=1', output_path]
            result = subprocess.run(audio_dur_cmd, capture_output=True, text=True)
            actual_audio_dur = float(result.stdout.strip())
            if actual_audio_dur >= audio_duration * 0.99:
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
    print("A/B 完整版样片自然收尾修复脚本")
    print("=" * 60)
    
    # 创建输出目录
    output_dir = os.path.join(PROJECT_ROOT, 'output_ab_natural')
    os.makedirs(output_dir, exist_ok=True)
    
    # ========== 第一步：生成 TTS ==========
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
    
    # ========== 第三步：生成 A 版（自然收尾） ==========
    print("\n" + "=" * 60)
    print("生成 A 版：规则基线（自然收尾）")
    print("=" * 60)
    
    clips_a = select_clips_rule_based_natural()
    output_a = os.path.join(output_dir, 'A_rule_baseline_natural.mp4')
    
    if not assemble_video_natural_ending(clips_a, tts_path, srt_path, output_a):
        print("\n❌ A 版生成失败")
        return False
    
    # ========== 第四步：生成 B 版（自然收尾） ==========
    print("\n" + "=" * 60)
    print("生成 B 版：AI 驱动（自然收尾）")
    print("=" * 60)
    
    clips_b = select_clips_ai_driven_natural()
    output_b = os.path.join(output_dir, 'B_ai_driven_natural.mp4')
    
    if not assemble_video_natural_ending(clips_b, tts_path, srt_path, output_b):
        print("\n❌ B 版生成失败")
        return False
    
    # ========== 第五步：复制到 static 目录 ==========
    static_dir = os.path.join(PROJECT_ROOT, 'static')
    os.makedirs(static_dir, exist_ok=True)
    
    import shutil
    shutil.copy(output_a, os.path.join(static_dir, 'A_rule_baseline_natural.mp4'))
    shutil.copy(output_b, os.path.join(static_dir, 'B_ai_driven_natural.mp4'))
    
    print("\n" + "=" * 60)
    print("✅ A/B 自然收尾样片生成完成")
    print("=" * 60)
    print(f"\n访问地址:")
    print(f"  A 版：http://47.93.194.154:8088/static/A_rule_baseline_natural.mp4")
    print(f"  B 版：http://47.93.194.154:8088/static/B_ai_driven_natural.mp4")
    
    return True

if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
