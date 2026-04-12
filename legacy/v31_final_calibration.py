#!/usr/bin/env python3
"""
V3.1 最终校准修复（真实音频时长对齐）

核心修复：
1. 禁止使用 TTS metadata duration
2. 使用 ffprobe 获取真实音频文件时长
3. 视频时长 = 音频时长 + 0.15~0.3 秒缓冲
4. 字幕从原始文本生成（禁止删除）
"""
import os
import subprocess
import json
from typing import List, Dict

VIDEO = None  # 在调用时注入

def get_real_audio_duration(audio_path: str) -> float:
    """
    获取真实音频时长（ffprobe）
    
    Args:
        audio_path: 音频文件路径
    
    Returns:
        真实时长（秒）
    """
    if not os.path.exists(audio_path):
        raise FileNotFoundError(f"音频文件不存在：{audio_path}")
    
    ffprobe_path = VIDEO['ffmpeg_path'].replace('ffmpeg', 'ffprobe') if VIDEO else 'ffprobe'
    
    cmd = [
        ffprobe_path, '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        audio_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 失败：{result.stderr}")
    
    try:
        duration = float(result.stdout.strip())
    except ValueError:
        raise RuntimeError(f"无法解析音频时长：{result.stdout}")
    
    return duration

def calculate_target_duration(audio_duration: float, buffer: float = 0.2) -> float:
    """
    计算目标视频时长（音频 + 缓冲）
    
    Args:
        audio_duration: 真实音频时长
        buffer: 缓冲时间（默认 0.2 秒）
    
    Returns:
        目标视频时长
    """
    return audio_duration + buffer

def verify_audio_video_sync(audio_path: str, video_path: str) -> Dict:
    """
    验证音视频同步
    
    Args:
        audio_path: 音频文件路径
        video_path: 视频文件路径
    
    Returns:
        {'audio_duration': ..., 'video_duration': ..., 'diff': ..., 'passed': ...}
    """
    audio_duration = get_real_audio_duration(audio_path)
    
    ffprobe_path = VIDEO['ffmpeg_path'].replace('ffmpeg', 'ffprobe') if VIDEO else 'ffprobe'
    
    # 获取视频时长
    cmd = [
        ffprobe_path, '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        video_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    video_duration = float(result.stdout.strip())
    
    diff = video_duration - audio_duration
    
    # 视频应该略长于音频（缓冲）
    passed = 0 <= diff <= 0.5
    
    return {
        'audio_duration': audio_duration,
        'video_duration': video_duration,
        'diff': diff,
        'passed': passed
    }

def generate_subtitle_from_text(script_text: str, audio_duration: float) -> List[Dict]:
    """
    从原始文本生成字幕（禁止删除）
    
    Args:
        script_text: 原始文本
        audio_duration: 音频时长
    
    Returns:
        字幕列表
    """
    # 按句号/感叹号/问号拆分句子
    import re
    sentences = re.split(r'[。！？!?]', script_text)
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

def check_subtitle_completeness(subtitle_path: str, original_text: str) -> Dict:
    """
    检查字幕完整性
    
    Args:
        subtitle_path: SRT 字幕文件路径
        original_text: 原始文本
    
    Returns:
        {'subtitle_count': ..., 'original_sentence_count': ..., 'passed': ..., 'issues': [...]}
    """
    import re
    
    issues = []
    
    # 计算原始文本句子数
    original_sentences = re.split(r'[。！？!?]', original_text)
    original_sentences = [s.strip() for s in original_sentences if s.strip()]
    original_sentence_count = len(original_sentences)
    
    # 计算字幕数量
    try:
        with open(subtitle_path, 'r', encoding='utf-8') as f:
            srt_content = f.read()
        
        subtitle_blocks = [b for b in srt_content.strip().split('\n\n') if b.strip()]
        subtitle_count = len(subtitle_blocks)
    except Exception as e:
        return {
            'subtitle_count': 0,
            'original_sentence_count': original_sentence_count,
            'passed': False,
            'issues': [f'无法读取 SRT 字幕：{e}']
        }
    
    # 比较数量
    if subtitle_count < original_sentence_count:
        issues.append(f'字幕缺失：字幕{subtitle_count}句 < 原文{original_sentence_count}句')
    elif subtitle_count > original_sentence_count:
        issues.append(f'字幕过多：字幕{subtitle_count}句 > 原文{original_sentence_count}句')
    
    return {
        'subtitle_count': subtitle_count,
        'original_sentence_count': original_sentence_count,
        'passed': subtitle_count == original_sentence_count,
        'issues': issues
    }

if __name__ == '__main__':
    # 测试
    print("=== V3.1 最终校准模块测试 ===")
    
    # 测试真实音频时长获取
    print("\n[1] 真实音频时长获取测试：")
    print("  （需要实际音频文件才能测试）")
    
    # 测试字幕生成
    print("\n[2] 从原始文本生成字幕测试：")
    test_text = "3 月 26 日，济南市人社局在美团服务中心开展活动。活动以走进奔跑者为主题。"
    subtitles = generate_subtitle_from_text(test_text, 10.0)
    print(f"  原文句子数：{len(test_text.split('。'))-1}")
    print(f"  生成字幕数：{len(subtitles)}")
    for sub in subtitles:
        print(f"    {sub['index']}. {sub['text']} ({sub['start_time']:.2f}s-{sub['end_time']:.2f}s)")
