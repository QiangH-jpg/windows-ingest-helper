#!/usr/bin/env python3
"""
A/B 完整版样片最终修复 v5
策略：移除有问题的 clip_11，用 clip_10（交流互动）替代
"""
import os, sys, subprocess
PROJECT_ROOT = '/tmp/video-tool-test-48975'
sys.path.insert(0, PROJECT_ROOT)
from core.config import config
from pipeline.tts_provider import generate_tts, create_subtitle_srt_from_meta

FFMPEG = config['video']['ffmpeg_path']
FFPROBE = FFMPEG.replace('ffmpeg', 'ffprobe')
CLIPS_DIR = os.path.join(PROJECT_ROOT, 'archive/data_archive/clips')
NEWS_SCRIPT = "3 月 26 日，济南市人社局人社服务大篷车活动在美团服务中心开展。活动以走进奔跑者保障与你同行为主题，聚焦外卖骑手等新就业形态劳动者。工作人员和志愿者通过发放资料、面对面讲解，向外卖小哥介绍社保参保、权益保障政策。现场设置互动环节，让大家在轻松氛围中了解政策、增强维权意识。济南市人社局持续推动人社服务走近新就业形态劳动者，打通服务保障最后一公里。"

ORIGINAL = {0:1.52,1:1.52,2:2.60,3:1.52,4:2.80,5:4.44,6:4.12,7:3.12,8:4.00,9:4.48,10:3.00,11:3.00,12:2.64}

def get_duration(path):
    cmd = [FFPROBE, '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', path]
    return float(subprocess.run(cmd, capture_output=True, text=True).stdout.strip())

def assemble_v5(clip_order, bonus_clip, audio_path, srt_path, output_path, version_name):
    print(f"\n【{version_name}视频组装】")
    audio_dur = get_duration(audio_path)
    print(f"  音频时长：{audio_dur:.2f}秒")
    
    # 创建补镜
    bonus_idx, bonus_start, bonus_dur = bonus_clip
    bonus_path = os.path.join(CLIPS_DIR, f'clip_{bonus_idx}.mp4')
    bonus_out = output_path + f'.bonus_clip_{bonus_idx}.mp4'
    cmd = [FFMPEG, '-y', '-i', bonus_path, '-vf', f'trim={bonus_start}:{bonus_start+bonus_dur}',
           '-c:v', 'libx264', '-preset', 'fast', '-crf', '23', '-r', '30', '-an', bonus_out]
    subprocess.run(cmd, capture_output=True, check=True)
    print(f"  补镜：clip_{bonus_idx} [{bonus_start:.2f}s-{bonus_start+bonus_dur:.2f}s] ({bonus_dur:.2f}秒)")
    
    total_dur = sum(ORIGINAL[i] for i in clip_order) + bonus_dur
    print(f"  画面总时长：{total_dur:.2f}秒")
    
    # 创建 concat 文件
    concat = output_path + '.concat.txt'
    with open(concat, 'w') as f:
        for i, idx in enumerate(clip_order):
            path = os.path.join(CLIPS_DIR, f'clip_{idx}.mp4')
            f.write(f"file '{path}'\n")
            if idx == 10:  # 在 clip_10 后插入补镜
                f.write(f"file '{bonus_out}'\n")
    
    srt_escaped = os.path.abspath(srt_path).replace(':', '\\:').replace("'", "'\\''")
    sub_filter = f"subtitles='{srt_escaped}':force_style='Alignment=2,MarginV=20,MarginL=40,MarginR=40,FontName=WenQuanYi Micro Hei,FontSize=26,PrimaryColour=&HFFFFFF,OutlineColour=&H202020,BorderStyle=1,Outline=1'"
    
    cmd = [FFMPEG, '-y', '-f', 'concat', '-safe', '0', '-i', concat, '-i', audio_path,
           '-map', '0:v:0', '-map', '1:a:0', '-vf', sub_filter, '-t', str(audio_dur),
           '-c:v', 'libx264', '-preset', 'fast', '-crf', '23', '-r', '30',
           '-c:a', 'aac', '-b:a', '128k', '-af', 'volume=2.0', output_path]
    
    print(f"  执行 ffmpeg...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ❌ 失败：{result.stderr[:300]}")
        return False
    
    dur = get_duration(output_path)
    audio_cmd = [FFPROBE, '-v', 'error', '-select_streams', 'a:0', '-show_entries', 'stream=duration', '-of', 'default=noprint_wrappers=1:nokey=1', output_path]
    audio_dur_actual = float(subprocess.run(audio_cmd, capture_output=True, text=True).stdout.strip())
    print(f"  ✅ 视频：{dur:.2f}秒，音频：{audio_dur_actual:.2f}秒")
    return True

def main():
    print("=" * 60)
    print("A/B 完整版样片最终修复 v5（移除 clip_11，用 clip_10 替代）")
    print("=" * 60)
    
    outdir = os.path.join(PROJECT_ROOT, 'output_ab_final_v5')
    os.makedirs(outdir, exist_ok=True)
    
    tts_path = os.path.join(outdir, 'narration.mp3')
    meta = generate_tts(NEWS_SCRIPT, tts_path, tts_path.replace('.mp3', '_meta.json'))
    print(f"TTS: {meta['total_duration']:.2f}秒")
    
    srt_path = os.path.join(outdir, 'subtitles.srt')
    create_subtitle_srt_from_meta(meta, srt_path)
    print(f"SRT: 22 条字幕")
    
    # A 版：移除 clip_11，顺序：0-10,12 + 补镜
    a_order = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12]  # 移除 clip_11
    a_bonus = (5, 2.44, 2.00)
    out_a = os.path.join(outdir, 'A_rule_final_v5.mp4')
    assemble_v5(a_order, a_bonus, tts_path, srt_path, out_a, "A 版规则基线")
    
    # B 版：移除 clip_11，顺序：5-10,0-4,12 + 补镜
    b_order = [5, 6, 7, 8, 10, 9, 0, 1, 2, 3, 4, 12]  # 移除 clip_11
    b_bonus = (8, 2.00, 2.00)
    out_b = os.path.join(outdir, 'B_ai_final_v5.mp4')
    assemble_v5(b_order, b_bonus, tts_path, srt_path, out_b, "B 版 AI 驱动")
    
    # 复制到 static
    static = os.path.join(PROJECT_ROOT, 'static')
    import shutil
    shutil.copy(out_a, os.path.join(static, 'A_rule_final_v5.mp4'))
    shutil.copy(out_b, os.path.join(static, 'B_ai_final_v5.mp4'))
    
    print("\n" + "=" * 60)
    print("✅ 完成")
    print("=" * 60)
    print(f"A 版：http://47.93.194.154:8088/static/A_rule_final_v5.mp4")
    print(f"B 版：http://47.93.194.154:8088/static/B_ai_final_v5.mp4")

if __name__ == '__main__':
    main()
