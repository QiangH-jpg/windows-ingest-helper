#!/usr/bin/env python3
"""
剪辑节奏优化 V2.1（在 V2.0 基础上增强）

核心增强：
1. 镜头时长分布：短 (2-3 秒 30%)/中 (3-4 秒 50%)/长 (4-6 秒 20%)
2. 节奏变化规则：避免均匀切换，短→中→短→长→中
3. 镜头类型节奏：全景开头→近景中段→互动中后→交流结尾
4. 相邻镜头关系：避免跳跃，优先渐进

不重构 V2.0 核心逻辑，仅在后处理阶段优化时长分配。
"""
import random
from typing import List, Dict, Tuple

# 镜头时长分布目标
DURATION_TARGETS = {
    'short': {'min': 2.0, 'max': 3.0, 'ratio': 0.30},  # 30%
    'medium': {'min': 3.0, 'max': 4.0, 'ratio': 0.50},  # 50%
    'long': {'min': 4.0, 'max': 6.0, 'ratio': 0.20}  # 20%
}

# 镜头类型节奏（推荐顺序）
SHOT_TYPE_SEQUENCE = [
    ['全景', '横幅', '稳定'],  # 开头：建立场景
    ['近景', '清晰', '动作'],  # 中段：信息
    ['互动', '表情', '微笑'],  # 中后段：氛围
    ['交流', '合影', '握手']  # 结尾：收口
]

def calculate_duration_distribution(total_clips: int) -> Dict[str, int]:
    """
    计算镜头时长分布
    
    Args:
        total_clips: 总镜头数
    
    Returns:
        {'short': 3, 'medium': 5, 'long': 2}
    """
    short_count = int(total_clips * DURATION_TARGETS['short']['ratio'])
    medium_count = int(total_clips * DURATION_TARGETS['medium']['ratio'])
    long_count = total_clips - short_count - medium_count
    
    return {
        'short': short_count,
        'medium': medium_count,
        'long': long_count
    }

def generate_rhythmic_durations(total_clips: int, base_duration: float) -> List[float]:
    """
    生成有节奏感的镜头时长列表
    
    规则：
    1. 符合短/中/长分布
    2. 避免连续相同长度
    3. 短→中→短→长→中 交替
    
    Args:
        total_clips: 总镜头数
        base_duration: 基础时长（用于计算总时长）
    
    Returns:
        [2.5, 3.5, 2.8, 4.5, 3.2, ...]
    """
    distribution = calculate_duration_distribution(total_clips)
    
    # 生成初始时长列表
    durations = []
    
    # 短镜头
    for _ in range(distribution['short']):
        durations.append(random.uniform(DURATION_TARGETS['short']['min'], DURATION_TARGETS['short']['max']))
    
    # 中镜头
    for _ in range(distribution['medium']):
        durations.append(random.uniform(DURATION_TARGETS['medium']['min'], DURATION_TARGETS['medium']['max']))
    
    # 长镜头
    for _ in range(distribution['long']):
        durations.append(random.uniform(DURATION_TARGETS['long']['min'], DURATION_TARGETS['long']['max']))
    
    # 打乱顺序，创造节奏感
    random.shuffle(durations)
    
    # 优化：避免连续相同长度
    optimized = optimize_duration_sequence(durations)
    
    return optimized

def optimize_duration_sequence(durations: List[float]) -> List[float]:
    """
    优化时长序列，避免连续相同长度
    
    规则：
    - 避免 3s → 3s → 3s
    - 优先 短→中→短→长→中
    """
    if len(durations) <= 2:
        return durations
    
    # 分类时长
    short = [d for d in durations if d < 3.0]
    medium = [d for d in durations if 3.0 <= d < 4.0]
    long = [d for d in durations if d >= 4.0]
    
    # 交替排列
    result = []
    pointers = {'short': 0, 'medium': 0, 'long': 0}
    counts = {'short': len(short), 'medium': len(medium), 'long': len(long)}
    
    # 节奏模式：短→中→短→长→中
    pattern = ['short', 'medium', 'short', 'long', 'medium']
    pattern_idx = 0
    
    while len(result) < len(durations):
        category = pattern[pattern_idx % len(pattern)]
        
        if pointers[category] < counts[category]:
            if category == 'short':
                result.append(short[pointers['short']])
                pointers['short'] += 1
            elif category == 'medium':
                result.append(medium[pointers['medium']])
                pointers['medium'] += 1
            else:  # long
                result.append(long[pointers['long']])
                pointers['long'] += 1
        
        pattern_idx += 1
        
        # 如果当前类别用完了，尝试下一个
        if pointers[category] >= counts[category]:
            # 找下一个还有剩余的类别
            for cat in ['short', 'medium', 'long']:
                if pointers[cat] < counts[cat]:
                    pattern_idx = pattern.index(cat) if cat in pattern else pattern_idx
                    break
    
    return result

def get_shot_type_from_tags(tags_v2: List[str]) -> str:
    """
    从三层标签中提取主要镜头类型
    
    Args:
        tags_v2: ["递发资料_近景_清晰", "面对面讲解_特写_清晰"]
    
    Returns:
        '近景', '全景', '特写', etc.
    """
    shot_type_priority = ['特写', '近景', '中景', '全景', '远景']
    
    for tag in tags_v2:
        parts = tag.split('_')
        if len(parts) >= 2:
            shot_type = parts[1]
            if shot_type in shot_type_priority:
                return shot_type
    
    # 默认返回第一个标签的镜头类型
    if tags_v2:
        parts = tags_v2[0].split('_')
        return parts[1] if len(parts) >= 2 else '未知'
    
    return '未知'

def calculate_shot_type_sequence_score(selected_clips: List[Dict]) -> Tuple[int, str]:
    """
    计算镜头类型序列得分
    
    规则：
    1. 开头是全景/横幅：+2
    2. 中段有近景/动作：+2
    3. 中后有互动/表情：+2
    4. 结尾是交流/合影：+2
    5. 避免全程同类型：+2
    
    Args:
        selected_clips: 选中的镜头列表
    
    Returns:
        (score, description)
    """
    score = 0
    reasons = []
    
    if not selected_clips:
        return 0, "无镜头"
    
    total_clips = len(selected_clips)
    
    # 1. 开头检查（前 2 个镜头）
    for i in range(min(2, total_clips)):
        shot_type = get_shot_type_from_tags(selected_clips[i].get('tags_v2', []))
        if shot_type in ['全景', '横幅', '稳定']:
            score += 2
            reasons.append(f"镜头{i+1}: {shot_type}（建立场景）✅")
            break
    
    # 2. 中段检查（中间 1/3）
    mid_start = total_clips // 3
    mid_end = 2 * total_clips // 3
    for i in range(mid_start, mid_end):
        if i < total_clips:
            shot_type = get_shot_type_from_tags(selected_clips[i].get('tags_v2', []))
            if shot_type in ['近景', '清晰', '特写']:
                score += 2
                reasons.append(f"镜头{i+1}: {shot_type}（信息）✅")
                break
    
    # 3. 中后段检查（后 1/3）
    late_start = total_clips // 2
    for i in range(late_start, total_clips):
        shot_type = get_shot_type_from_tags(selected_clips[i].get('tags_v2', []))
        if shot_type in ['互动', '微笑', '表情', '轻松']:
            score += 2
            reasons.append(f"镜头{i+1}: {shot_type}（氛围）✅")
            break
    
    # 4. 结尾检查（最后 2 个镜头）
    for i in range(max(total_clips - 2, 0), total_clips):
        shot_type = get_shot_type_from_tags(selected_clips[i].get('tags_v2', []))
        if shot_type in ['交流', '合影', '握手', '讲话']:
            score += 2
            reasons.append(f"镜头{i+1}: {shot_type}（收口）✅")
            break
    
    # 5. 多样性检查
    shot_types = [get_shot_type_from_tags(clip.get('tags_v2', [])) for clip in selected_clips]
    unique_types = set(shot_types)
    if len(unique_types) >= 3:
        score += 2
        reasons.append(f"镜头类型多样 ({len(unique_types)}种) ✅")
    else:
        reasons.append(f"镜头类型单一 ({len(unique_types)}种) ⚠️")
    
    return score, "，".join(reasons)

def apply_rhythm_optimization(selected_clips: List[Dict], total_duration: float) -> List[Dict]:
    """
    应用节奏优化到选中的镜头
    
    Args:
        selected_clips: 选中的镜头列表
        total_duration: 总时长
    
    Returns:
        优化后的镜头列表（含优化后的时长）
    """
    total_clips = len(selected_clips)
    
    # 生成有节奏的时长分布
    rhythmic_durations = generate_rhythmic_durations(total_clips, total_duration / total_clips)
    
    # 调整总时长匹配
    current_total = sum(rhythmic_durations)
    scale_factor = total_duration / current_total
    rhythmic_durations = [d * scale_factor for d in rhythmic_durations]
    
    # 应用时长到镜头
    for i, clip in enumerate(selected_clips):
        clip['optimized_duration'] = rhythmic_durations[i]
        clip['duration_category'] = (
            '短' if rhythmic_durations[i] < 3.0 else
            '中' if rhythmic_durations[i] < 4.0 else
            '长'
        )
    
    return selected_clips

if __name__ == '__main__':
    # 测试
    print("=== 剪辑节奏优化 V2.1 测试 ===")
    
    # 测试时长分布
    print("\n[1] 镜头时长分布测试（10 个镜头）：")
    distribution = calculate_duration_distribution(10)
    print(f"  短镜头：{distribution['short']}个 (30%)")
    print(f"  中镜头：{distribution['medium']}个 (50%)")
    print(f"  长镜头：{distribution['long']}个 (20%)")
    
    # 测试节奏生成
    print("\n[2] 节奏时长生成测试：")
    durations = generate_rhythmic_durations(10, 3.5)
    for i, d in enumerate(durations):
        category = '短' if d < 3.0 else '中' if d < 4.0 else '长'
        print(f"  镜头{i+1}: {d:.2f}s ({category})")
    
    # 测试镜头类型提取
    print("\n[3] 镜头类型提取测试：")
    test_tags = ["递发资料_近景_清晰", "现场全景_稳定", "面对面讲解_特写_清晰"]
    for tags in [test_tags]:
        shot_type = get_shot_type_from_tags(tags)
        print(f"  {tags[0]} → {shot_type}")
