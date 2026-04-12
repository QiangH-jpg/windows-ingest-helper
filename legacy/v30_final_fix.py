#!/usr/bin/env python3
"""
V3.0 最终收口修复（禁止补丁式修复）

核心规则重构：
1. 视频结束时间 = TTS 结束时间（严格一致，误差≤0.1s）
2. 禁止循环最后镜头
3. 禁止延长镜头补时长
4. 禁止额外拼接片段
5. 禁止视频超过音频

正确做法：
1. 在生成阶段就保证每个镜头时长之和 ≈ TTS 时长
2. 通过微调每段±0.2~0.5 秒来调整，而不是补镜头
3. 最后一个镜头自然结束在 TTS 结束点

字幕保护：
1. 禁止删除字幕句子
2. 字幕必须完整覆盖音频文本
"""
from typing import List, Dict

def enforce_exact_duration(clips: List[Dict], target_duration: float, tolerance: float = 0.1) -> Dict:
    """
    强制精确时长对齐（误差≤tolerance）
    
    策略：
    1. 计算当前总时长
    2. 如果不足：按比例增加每个镜头时长（允许超过±0.5 秒）
    3. 如果超出：按比例减少每个镜头时长
    4. 确保所有镜头≥1.5 秒
    5. 禁止循环、禁止拼接
    
    Args:
        clips: 镜头列表
        target_duration: 目标时长（TTS 时长）
        tolerance: 允许误差（默认 0.1 秒）
    
    Returns:
        {'passed': True/False, 'clips': [...], 'total_duration': ..., 'error': ...}
    """
    current_total = sum(c['duration'] for c in clips)
    error = current_total - target_duration
    
    # 检查是否已满足要求
    if abs(error) <= tolerance:
        return {
            'passed': True,
            'clips': clips,
            'total_duration': current_total,
            'error': error,
            'adjustment': 'none'
        }
    
    # 需要调整：按比例缩放每个镜头
    num_clips = len(clips)
    scale_factor = target_duration / current_total
    
    # 微调每个镜头时长（按比例缩放）
    adjusted_clips = []
    for clip in clips:
        adjusted_clip = clip.copy()
        new_duration = clip['duration'] * scale_factor
        
        # 确保镜头时长≥1.5 秒
        if new_duration < 1.5:
            new_duration = 1.5
        
        adjusted_clip['duration'] = new_duration
        adjusted_clip['optimized_duration'] = new_duration
        adjusted_clips.append(adjusted_clip)
    
    # 重新计算总时长
    new_total = sum(c['duration'] for c in adjusted_clips)
    new_error = new_total - target_duration
    
    # 如果仍有误差，进行二次微调
    if abs(new_error) > tolerance:
        # 计算剩余误差，平均分配到每个镜头
        remaining_error_per_clip = new_error / num_clips
        for clip in adjusted_clips:
            new_duration = clip['duration'] - remaining_error_per_clip
            if new_duration < 1.5:
                new_duration = 1.5
            clip['duration'] = new_duration
            clip['optimized_duration'] = new_duration
        
        new_total = sum(c['duration'] for c in adjusted_clips)
        new_error = new_total - target_duration
    
    return {
        'passed': abs(new_error) <= tolerance,
        'clips': adjusted_clips,
        'total_duration': new_total,
        'error': new_error,
        'adjustment': 'proportional_scale',
        'scale_factor': scale_factor
    }

def verify_no_loop_or_extension(clips: List[Dict]) -> Dict:
    """
    验证无循环、无延长
    
    检查项：
    1. 无 is_loop 标记的镜头
    2. 无 is_extended 标记的镜头
    3. 无重复素材连续出现
    
    Args:
        clips: 镜头列表
    
    Returns:
        {'passed': True/False, 'issues': [...]}
    """
    issues = []
    
    for i, clip in enumerate(clips):
        # 检查循环标记
        if clip.get('is_loop', False):
            issues.append(f"镜头{i+1}: 存在循环标记 (is_loop=True)")
        
        # 检查延长标记
        if clip.get('is_extended', False):
            issues.append(f"镜头{i+1}: 存在延长标记 (is_extended=True)")
    
    # 检查重复素材连续出现（最后 2 个镜头）
    if len(clips) >= 2:
        last_clip = clips[-1]
        second_last_clip = clips[-2]
        
        if last_clip.get('source_name') == second_last_clip.get('source_name'):
            issues.append(f"镜头{len(clips)-1}和{len(clips)}: 连续使用同一素材 ({last_clip['source_name']})")
    
    return {
        'passed': len(issues) == 0,
        'issues': issues
    }

def verify_subtitle_completeness(subtitle_path: str, tts_meta_path: str) -> Dict:
    """
    验证字幕完整性
    
    检查项：
    1. 字幕句子数 = TTS 句子数
    2. 无删除字幕句子
    
    Args:
        subtitle_path: SRT 字幕文件路径
        tts_meta_path: TTS 元数据文件路径
    
    Returns:
        {'passed': True/False, 'subtitle_count': ..., 'tts_sentence_count': ..., 'issues': [...]}
    """
    import json
    
    issues = []
    
    # 读取 TTS 元数据
    try:
        with open(tts_meta_path, 'r', encoding='utf-8') as f:
            tts_meta = json.load(f)
        tts_sentence_count = len(tts_meta.get('sentences', []))
    except Exception as e:
        return {
            'passed': False,
            'subtitle_count': 0,
            'tts_sentence_count': 0,
            'issues': [f'无法读取 TTS 元数据：{e}']
        }
    
    # 读取 SRT 字幕
    try:
        with open(subtitle_path, 'r', encoding='utf-8') as f:
            srt_content = f.read()
        
        # 计算字幕块数
        subtitle_blocks = [b for b in srt_content.strip().split('\n\n') if b.strip()]
        subtitle_count = len(subtitle_blocks)
    except Exception as e:
        return {
            'passed': False,
            'subtitle_count': 0,
            'tts_sentence_count': tts_sentence_count,
            'issues': [f'无法读取 SRT 字幕：{e}']
        }
    
    # 比较数量
    if subtitle_count < tts_sentence_count:
        issues.append(f'字幕缺失：字幕{subtitle_count}句 < TTS{tts_sentence_count}句')
    elif subtitle_count > tts_sentence_count:
        issues.append(f'字幕过多：字幕{subtitle_count}句 > TTS{tts_sentence_count}句')
    
    return {
        'passed': subtitle_count == tts_sentence_count,
        'subtitle_count': subtitle_count,
        'tts_sentence_count': tts_sentence_count,
        'issues': issues
    }

def final_safety_check(clips: List[Dict], audio_duration: float, tolerance: float = 0.1) -> Dict:
    """
    最终安全检查（拼接前必须通过）
    
    检查项：
    1. 视频时长 == 音频时长（误差≤tolerance）
    2. 无循环镜头
    3. 无延长镜头
    4. 无连续重复素材
    
    Args:
        clips: 镜头列表
        audio_duration: 音频时长
        tolerance: 允许误差
    
    Returns:
        {'passed': True/False, 'issues': [...], 'clips': [...]}
    """
    issues = []
    
    # 1. 检查时长对齐
    total_duration = sum(c['duration'] for c in clips)
    error = total_duration - audio_duration
    
    if abs(error) > tolerance:
        issues.append(f'时长不匹配：视频{total_duration:.2f}s vs 音频{audio_duration:.2f}s (误差{error:.2f}s > {tolerance}s)')
    
    # 2. 检查无循环/延长
    loop_check = verify_no_loop_or_extension(clips)
    if not loop_check['passed']:
        issues.extend(loop_check['issues'])
    
    # 3. 如果时长不匹配，尝试微调
    if abs(error) > tolerance:
        adjustment_result = enforce_exact_duration(clips, audio_duration, tolerance)
        
        if adjustment_result['passed']:
            clips = adjustment_result['clips']
            issues = []  # 清除时长问题
        else:
            issues.append(adjustment_result.get('reason', '微调失败'))
    
    return {
        'passed': len(issues) == 0,
        'issues': issues,
        'clips': clips,
        'total_duration': sum(c['duration'] for c in clips),
        'audio_duration': audio_duration
    }

if __name__ == '__main__':
    # 测试
    print("=== V3.0 最终收口修复模块测试 ===")
    
    # 测试精确时长对齐
    print("\n[1] 精确时长对齐测试：")
    test_clips = [
        {'duration': 3.8, 'name': 'clip1'},
        {'duration': 3.8, 'name': 'clip2'},
        {'duration': 3.8, 'name': 'clip3'}
    ]
    target = 12.0  # 目标 12 秒
    result = enforce_exact_duration(test_clips, target, 0.1)
    print(f"  原始总时长：{sum(c['duration'] for c in test_clips):.2f}s")
    print(f"  目标时长：{target:.2f}s")
    print(f"  检查结果：{'✅ 通过' if result['passed'] else '❌ 失败'}")
    print(f"  调整后总时长：{result['total_duration']:.2f}s")
    print(f"  误差：{result['error']:.3f}s")
    
    # 测试无循环验证
    print("\n[2] 无循环/延长验证测试：")
    test_clips_with_loop = [
        {'duration': 3.8, 'source_name': 'clip1'},
        {'duration': 3.8, 'source_name': 'clip2', 'is_loop': True}  # 循环标记
    ]
    result = verify_no_loop_or_extension(test_clips_with_loop)
    print(f"  检查结果：{'✅ 通过' if result['passed'] else '❌ 失败'}")
    print(f"  问题：{result['issues']}")
    
    # 测试最终安全检查
    print("\n[3] 最终安全检查测试：")
    test_clips = [
        {'duration': 3.8, 'source_name': 'clip1'},
        {'duration': 3.8, 'source_name': 'clip2'},
        {'duration': 3.8, 'source_name': 'clip3'}
    ]
    audio_duration = 11.4  # 11.4 秒
    result = final_safety_check(test_clips, audio_duration, 0.1)
    print(f"  检查结果：{'✅ 通过' if result['passed'] else '❌ 失败'}")
    if result['issues']:
        print(f"  问题：{result['issues']}")
    print(f"  视频时长：{result['total_duration']:.2f}s / 音频时长：{result['audio_duration']:.2f}s")
