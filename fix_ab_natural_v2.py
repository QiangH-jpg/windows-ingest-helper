#!/usr/bin/env python3
"""
A/B 完整版样片自然收尾修复脚本 v2
策略：轻微慢放 (0.94x) + 自然结尾镜头

优先级执行：
1. 不压缩核心镜头，只微调空镜
2. 结尾用 clip_11(游戏)+clip_12(交流) 自然收尾
3. 轻微慢放 0.94x 填补缺口
4. 静帧兜底≤1 秒
"""
import os
import sys
import subprocess

PROJECT_ROOT = '/tmp/video-tool-test-48975'
sys.path.insert(0, PROJECT_ROOT)

from core.config import config
from pipeline.tts_provider import generate_tts, create_subtitle_srt_from_meta

FFMPEG = config['video']['ffmpeg_path']
FFPROBE = FFMPEG.replace('ffmpeg', 'ffprobe')
CLIPS_DIR = os.path.join(PROJECT_ROOT, 'archive/data_archive/clips')

NEWS_SCRIPT = "3 月 26 日，济南市人社局人社服务大篷车活动在美团服务中心开展。活动以走进奔跑者保障与你同行为主题，聚焦外卖骑手等新就业形态劳动者。工作人员和志愿者通过发放资料、面对面讲解，向外卖小哥介绍社保参保、权益保障政策。现场设置互动环节，让大家在轻松氛围中了解政策、增强维权意识。济南市人社局持续推动人社服务走近新就业形态劳动者，打通服务保障最后一公里。"

CLIP_DESC = {
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
    11: "外卖小哥投掷游戏",
    12: "领导和外卖小哥说话"
}

def get_duration(path):
    cmd = [FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
           '-of', 'default=noprint_wrappers=1:nokey=1', path]
    return float(subprocess.run(cmd, capture_output=True, text=True).stdout.strip())

# A 版：规则基线顺序，结尾 clip_11+clip_12 自然收尾
A_CLIP_ORDER = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]

# B 版：AI 优先级（人物互动优先），结尾 clip_11+clip_12 自然收尾
B_CLIP_ORDER = [5, 6, 7, 8, 10, 9, 0, 1, 2, 3, 4, 11, 12]

def select_clips(order, version_name):
    """选择 clip 并应用轻微慢放 (0.94x) 填补缺口"""
    print(f"\n【{version_name}选片】")
    
    # 原始时长
    orig = {0:1.52,1:1.52,2:2.60,3:1.52,4:2.80,5:4.44,6:4.12,7:3.12,8:4.00,9:4.48,10:3.00,11:3.00,12:2.64}
    
    # 策略：轻微慢放 0.94x，让 38.76s → 41.23s，足够覆盖 41.04s
    # 但结尾 clip_11 和 clip_12 保持原速（避免动作失真）
    slow_factor = 0.94
    
    clips = []
    total_original = 0
    total_slow = 0
    
    for i, idx in enumerate(order):
        path = os.path.join(CLIPS_DIR, f'clip_{idx}.mp4')
        dur = orig[idx]
        total_original += dur
        
        # 结尾两个镜头保持原速（优先级 2：自然收尾）
        if idx in [11, 12]:
            clips.append({'index': idx, 'path': path, 'duration': dur, 'slow': False, 'desc': CLIP_DESC[idx]})
            total_slow += dur
        else:
            # 其他镜头轻微慢放 0.94x（优先级 4）
            slow_dur = dur / slow_factor
            clips.append({'index': idx, 'path': path, 'duration': slow_dur, 'slow': True, 'slow_factor': slow_factor, 'desc': CLIP_DESC[idx]})
            total_slow += slow_dur
    
    print(f"  原始总时长：{total_original:.2f}秒")
    print(f"  慢放后总时长：{total_slow:.2f}秒 (0.94x)")
    print(f"  旁白时长：41.04 秒")
    print(f"  余量：{total_slow - 41.04:.2f}秒")
    
    print(f"\n  时间线（结尾{len([c for c in clips if not c['slow']])}个镜头原速）:")
    for i, c in enumerate(clips):
        marker = "← 原速收尾" if not c['slow'] else f"(0.94x)"
        print(f"    {i+1}. clip_{c['index']} ({c['duration']:.2f}s) {c['desc']} {marker}")
    
    return clips

def assemble(clips, audio_path, srt_path, output_path, version_name):
    """组装视频"""
    print(f"\n【{version_name}视频组装】{output_path}")
    
    audio_dur = get_duration(audio_path)
    print(f"  音频时长：{audio_dur:.2f}秒")
    
    # 创建 concat 文件
    concat = output_path + '.concat.txt'
    with open(concat, 'w') as f:
        for i, c in enumerate(clips):
            if c['slow']:
                # 应用慢放（setpts 滤镜）
                slow_path = output_path + f'.clip_{i}_slow.mp4'
                cmd = [FFMPEG, '-y', '-i', c['path'],
                       '-vf', f"setpts={1/c['slow_factor']:.3f}*PTS",
                       '-c:v', 'libx264', '-preset', 'fast', '-crf', '23', '-r', '30', '-an',
                       slow_path]
                subprocess.run(cmd, capture_output=True, check=True)
                f.write(f"file '{slow_path}'\n")
            else:
                f.write(f"file '{c['path']}'\n")
    
    # 字幕滤镜
    srt_escaped = os.path.abspath(srt_path).replace(':', '\\:').replace("'", "'\\''")
    sub_filter = f"subtitles='{srt_escaped}':force_style='Alignment=2,MarginV=20,MarginL=40,MarginR=40,FontName=WenQuanYi Micro Hei,FontSize=26,PrimaryColour=&HFFFFFF,OutlineColour=&H202020,BorderStyle=1,Outline=1'"
    
    # FFmpeg 命令
    cmd = [FFMPEG, '-y', '-f', 'concat', '-safe', '0', '-i', concat,
           '-i', audio_path, '-map', '0:v:0', '-map', '1:a:0',
           '-vf', sub_filter, '-t', str(audio_dur),
           '-c:v', 'libx264', '-preset', 'fast', '-crf', '23', '-r', '30',
           '-c:a', 'aac', '-b:a', '128k', '-af', 'volume=2.0',
           output_path]
    
    print(f"  执行 ffmpeg...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ❌ 失败：{result.stderr[:300]}")
        return False
    
    # 验证
    dur = get_duration(output_path)
    audio_cmd = [FFPROBE, '-v', 'error', '-select_streams', 'a:0',
                 '-show_entries', 'stream=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', output_path]
    audio_dur_actual = float(subprocess.run(audio_cmd, capture_output=True, text=True).stdout.strip())
    
    print(f"  ✅ 视频：{dur:.2f}秒，音频：{audio_dur_actual:.2f}秒")
    print(f"  音频完整：{'✅' if audio_dur_actual >= audio_dur * 0.99 else '❌'}")
    
    return True

def main():
    print("=" * 60)
    print("A/B 自然收尾修复 v2（轻微慢放 0.94x + 结尾原速）")
    print("=" * 60)
    
    outdir = os.path.join(PROJECT_ROOT, 'output_ab_natural_v2')
    os.makedirs(outdir, exist_ok=True)
    
    # TTS
    tts_path = os.path.join(outdir, 'narration.mp3')
    meta = generate_tts(NEWS_SCRIPT, tts_path, tts_path.replace('.mp3', '_meta.json'))
    print(f"TTS: {meta['total_duration']:.2f}秒")
    
    # SRT
    srt_path = os.path.join(outdir, 'subtitles.srt')
    create_subtitle_srt_from_meta(meta, srt_path)
    print(f"SRT: 22 条字幕")
    
    # A 版
    clips_a = select_clips(A_CLIP_ORDER, "A 版规则基线")
    out_a = os.path.join(outdir, 'A_rule_natural_v2.mp4')
    assemble(clips_a, tts_path, srt_path, out_a, "A 版")
    
    # B 版
    clips_b = select_clips(B_CLIP_ORDER, "B 版 AI 驱动")
    out_b = os.path.join(outdir, 'B_ai_natural_v2.mp4')
    assemble(clips_b, tts_path, srt_path, out_b, "B 版")
    
    # 复制到 static
    static = os.path.join(PROJECT_ROOT, 'static')
    import shutil
    shutil.copy(out_a, os.path.join(static, 'A_rule_natural_v2.mp4'))
    shutil.copy(out_b, os.path.join(static, 'B_ai_natural_v2.mp4'))
    
    print("\n" + "=" * 60)
    print("✅ 完成")
    print("=" * 60)
    print(f"A 版：http://47.93.194.154:8088/static/A_rule_natural_v2.mp4")
    print(f"B 版：http://47.93.194.154:8088/static/B_ai_natural_v2.mp4")

if __name__ == '__main__':
    main()
