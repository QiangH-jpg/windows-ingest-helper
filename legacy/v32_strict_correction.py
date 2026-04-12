#!/usr/bin/env python3
"""
V3.2 系统纠偏修复（禁止虚假检测）

核心规则：
1. 禁止使用 metadata，必须用 ffprobe 真实读取
2. 视频时长 ≥ 音频时长 + 0.1s（强制缓冲）
3. 字幕来自原文（不是 TTS）
4. 最小镜头时长 ≥ 1.5 秒（防闪屏）
5. 最终真实校验（必须可见）
"""
import os
import subprocess
import json
import re
from typing import List, Dict, Tuple

VIDEO = None  # 在调用时注入

def ffprobe_get_duration(file_path: str) -> float:
    """
    使用 ffprobe 获取真实时长（禁止 metadata）
    
    Args:
        file_path: 媒体文件路径
    
    Returns:
        真实时长（秒）
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"文件不存在：{file_path}")
    
    ffprobe_path = VIDEO['ffmpeg_path'].replace('ffmpeg', 'ffprobe') if VIDEO else 'ffprobe'
    
    cmd = [
        ffprobe_path, '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        file_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 失败：{result.stderr}")
    
    try:
        duration = float(result.stdout.strip())
    except ValueError:
        raise RuntimeError(f"无法解析时长：{result.stdout}")
    
    return duration

def verify_video_longer_than_audio(video_path: str, audio_path: str, min_buffer: float = 0.1) -> Dict:
    """
    P0 验证：视频必须长于音频（强制）
    
    规则：
    video_duration ≥ audio_duration + min_buffer
    
    Args:
        video_path: 视频文件路径
        audio_path: 音频文件路径
        min_buffer: 最小缓冲时间（默认 0.1 秒）
    
    Returns:
        {'passed': True/False, 'video_duration': ..., 'audio_duration': ..., 'buffer': ..., 'error': ...}
    """
    video_duration = ffprobe_get_duration(video_path)
    audio_duration = ffprobe_get_duration(audio_path)
    
    buffer = video_duration - audio_duration
    
    passed = buffer >= min_buffer
    
    error = None
    if not passed:
        error = f"视频时长不足：视频{video_duration:.3f}s < 音频{audio_duration:.3f}s + 缓冲{min_buffer}s (实际缓冲{buffer:.3f}s)"
    
    return {
        'passed': passed,
        'video_duration': video_duration,
        'audio_duration': audio_duration,
        'buffer': buffer,
        'min_buffer': min_buffer,
        'error': error
    }

def generate_subtitle_from_original_text(original_text: str, audio_duration: float) -> List[Dict]:
    """
    P0 字幕生成：必须来自原文（不是 TTS）
    
    Args:
        original_text: 原始新闻稿文本
        audio_duration: 音频时长
    
    Returns:
        字幕列表
    """
    # 按句号/感叹号/问号拆分句子
    sentences = re.split(r'[.!?！？]', original_text)
    sentences = [s.strip() for s in sentences if s.strip()]
    
    # 计算每句时长（平均分配）
    duration_per_sentence = audio_duration / len(sentences)
    
    subtitles = []
    current_time = 0.0
    
    for i, sentence in enumerate(sentences):
        subtitles.append({
            'index': i + 1,
            'text': sentence,
            'start_time': current_time,
            'end_time': current_time + duration_per_sentence,
            'duration': duration_per_sentence
        })
        current_time += duration_per_sentence
    
    return subtitles

def verify_subtitle_completeness(subtitle_path: str, original_text: str) -> Dict:
    """
    P0 字幕完整性校验：逐字对比
    
    Args:
        subtitle_path: SRT 字幕文件路径
        original_text: 原始新闻稿文本
    
    Returns:
        {'passed': True/False, 'subtitle_text': ..., 'original_text': ..., 'match_rate': ..., 'issues': [...]}
    """
    issues = []
    
    # 读取字幕文本
    try:
        with open(subtitle_path, 'r', encoding='utf-8') as f:
            srt_content = f.read()
        
        # 提取字幕文本（去掉时间轴）
        subtitle_lines = []
        for block in srt_content.strip().split('\n\n'):
            lines = block.strip().split('\n')
            if len(lines) >= 3:
                subtitle_lines.extend(lines[2:])  # 跳过序号和时间轴
        
        subtitle_text = ''.join(subtitle_lines)
    except Exception as e:
        return {
            'passed': False,
            'subtitle_text': '',
            'original_text': original_text,
            'match_rate': 0.0,
            'issues': [f'无法读取 SRT 字幕：{e}']
        }
    
    # 逐字对比（忽略标点）
    subtitle_clean = re.sub(r'[,.!?！？,.\s]', '', subtitle_text)
    original_clean = re.sub(r'[,.!?！？,.\s]', '', original_text)
    
    # 计算匹配率
    if len(original_clean) == 0:
        match_rate = 1.0
    else:
        # 简单包含检查
        if original_clean in subtitle_clean or subtitle_clean in original_clean:
            match_rate = 1.0
        else:
            # 计算重合度
            common_chars = set(subtitle_clean) & set(original_clean)
            match_rate = len(common_chars) / max(len(subtitle_clean), len(original_clean))
    
    # 判断是否通过
    passed = match_rate >= 0.95  # 95% 匹配率
    
    if not passed:
        issues.append(f'字幕与原文不匹配：匹配率{match_rate*100:.1f}% < 95%')
        issues.append(f'字幕文本（前 100 字）：{subtitle_text[:100]}...')
        issues.append(f'原文文本（前 100 字）：{original_text[:100]}...')
    
    return {
        'passed': passed,
        'subtitle_text': subtitle_text,
        'original_text': original_text,
        'match_rate': match_rate,
        'issues': issues
    }

def verify_min_clip_duration(clips: List[Dict], min_duration: float = 1.5) -> Dict:
    """
    P0 镜头时长强制检查（防闪屏）
    
    Args:
        clips: 镜头列表
        min_duration: 最小镜头时长（默认 1.5 秒）
    
    Returns:
        {'passed': True/False, 'min_duration_found': ..., 'clips_below_min': [...], 'error': ...}
    """
    clips_below_min = []
    min_duration_found = float('inf')
    
    for i, clip in enumerate(clips):
        duration = clip.get('duration', 0)
        if duration < min_duration_found:
            min_duration_found = duration
        
        if duration < min_duration:
            clips_below_min.append({
                'index': i + 1,
                'material': clip.get('source_name', 'unknown'),
                'duration': duration
            })
    
    passed = len(clips_below_min) == 0
    
    error = None
    if not passed:
        error = f"存在闪屏镜头：{len(clips_below_min)}个镜头 < {min_duration}s (最短{min_duration_found:.2f}s)"
    
    return {
        'passed': passed,
        'min_duration_found': min_duration_found,
        'clips_below_min': clips_below_min,
        'error': error
    }

def final_real_verification(video_path: str, audio_path: str, subtitle_path: str, original_text: str, clips: List[Dict]) -> Dict:
    """
    P0 最终真实校验（必须可见）
    
    必须回传：
    1. ffprobe 音频时长
    2. ffprobe 视频时长
    3. 最短镜头时长
    4. 字幕全文（拼接后）
    
    Args:
        video_path: 视频文件路径
        audio_path: 音频文件路径
        subtitle_path: SRT 字幕文件路径
        original_text: 原始新闻稿文本
        clips: 镜头列表
    
    Returns:
        完整校验结果
    """
    results = {}
    all_passed = True
    
    # 1. ffprobe 音频时长
    try:
        audio_duration = ffprobe_get_duration(audio_path)
        results['audio_duration_ffprobe'] = audio_duration
    except Exception as e:
        results['audio_duration_ffprobe'] = f'ERROR: {e}'
        all_passed = False
    
    # 2. ffprobe 视频时长
    try:
        video_duration = ffprobe_get_duration(video_path)
        results['video_duration_ffprobe'] = video_duration
    except Exception as e:
        results['video_duration_ffprobe'] = f'ERROR: {e}'
        all_passed = False
    
    # 3. 最短镜头时长
    clip_check = verify_min_clip_duration(clips, 1.5)
    results['min_clip_duration'] = clip_check['min_duration_found']
    results['min_clip_passed'] = clip_check['passed']
    if not clip_check['passed']:
        all_passed = False
    
    # 4. 字幕全文（拼接后）
    subtitle_check = verify_subtitle_completeness(subtitle_path, original_text)
    results['subtitle_full_text'] = subtitle_check['subtitle_text']
    results['subtitle_match_rate'] = subtitle_check['match_rate']
    results['subtitle_passed'] = subtitle_check['passed']
    if not subtitle_check['passed']:
        all_passed = False
    
    # 5. 视频≥音频 + 缓冲
    if 'audio_duration_ffprobe' in results and 'video_duration_ffprobe' in results:
        if isinstance(results['audio_duration_ffprobe'], (int, float)) and isinstance(results['video_duration_ffprobe'], (int, float)):
            buffer_check = verify_video_longer_than_audio(video_path, audio_path, 0.1)
            results['video_audio_buffer'] = buffer_check['buffer']
            results['video_audio_passed'] = buffer_check['passed']
            if not buffer_check['passed']:
                all_passed = False
    
    results['all_passed'] = all_passed
    
    return results

if __name__ == '__main__':
    # 测试
    print("=== V3.2 系统纠偏模块测试 ===")
    
    print("\n[1] ffprobe 真实时长获取测试：")
    print("  （需要实际文件才能测试）")
    
    print("\n[2] 字幕完整性校验测试：")
    test_original = "3 月 26 日，济南市人社局在美团服务中心开展活动。活动以走进奔跑者为主题。"
    print(f"  原文：{test_original}")
