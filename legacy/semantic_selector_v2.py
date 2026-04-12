#!/usr/bin/env python3
"""
语义选片 V2.0（三层标签 + 镜头质量评分）

核心升级：
1. 标签从单层升级为三层：类型_镜头类型_画面质量
2. 评分增加镜头质量权重：近景 +2，清晰 +2，特写 +3
3. 在同一标签下优先选"更好的镜头"

不引入：
- 大模型
- 图像识别
"""
import os
import json
from typing import List, Dict, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(PROJECT_ROOT, 'config')

# 镜头质量权重
QUALITY_WEIGHTS = {
    '近景': 2,
    '清晰': 2,
    '特写': 3,
    '全景': 1,
    '稳定': 1,
    '流畅': 1,
    '中景': 1,
    '远景': 0
}

def load_material_tags_v2() -> Dict[str, Dict]:
    """加载 V2.0 三层素材标签"""
    tags_path = os.path.join(CONFIG_DIR, 'material_tags_v2.json')
    with open(tags_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return data['materials']

def load_scoring_rules() -> Dict:
    """加载评分规则"""
    tags_path = os.path.join(CONFIG_DIR, 'material_tags_v2.json')
    with open(tags_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return data['scoring_rules']

def parse_tag_v2(tag: str) -> Dict:
    """
    解析三层标签
    
    示例：
    "递发资料_近景_清晰" → {
        'type': '递发资料',
        'shot_type': '近景',
        'quality': '清晰',
        'full': '递发资料_近景_清晰'
    }
    """
    parts = tag.split('_')
    result = {
        'full': tag,
        'type': parts[0] if len(parts) > 0 else '',
        'shot_type': parts[1] if len(parts) > 1 else '',
        'quality': parts[2] if len(parts) > 2 else ''
    }
    return result

def calculate_quality_score(parsed_tags: List[Dict]) -> int:
    """
    计算镜头质量分
    
    评分规则：
    - 近景 +2
    - 清晰 +2
    - 特写 +3
    - 全景 +1
    - 稳定 +1
    """
    score = 0
    for tag in parsed_tags:
        # 镜头类型分
        if tag['shot_type'] in QUALITY_WEIGHTS:
            score += QUALITY_WEIGHTS[tag['shot_type']]
        # 画面质量分
        if tag['quality'] in QUALITY_WEIGHTS:
            score += QUALITY_WEIGHTS[tag['quality']]
    return score

def select_best_material_v2(
    candidates: List[Dict],
    target_tags: List[str],
    target_actions: List[str],
    used_materials: Dict[str, int],
    last_material: str = None
) -> Tuple[Dict, str, int]:
    """
    V2.0 选片核心：三层标签 + 镜头质量评分
    
    评分优先级：
    1. 动作标签匹配分（最高，5 分/个）
    2. 镜头质量分（近景 +2，清晰 +2，特写 +3）
    3. 泛标签匹配分（较低，1 分/个）
    4. 多样性分（未使用 +2）
    5. 连续重复惩罚（-10）
    """
    materials = load_material_tags_v2()
    
    scored_candidates = []
    
    for candidate in candidates:
        filename = candidate['name']
        info = materials.get(filename, {})
        tags_v2 = info.get('tags_v2', [])
        
        # 解析所有标签
        parsed_tags = [parse_tag_v2(tag) for tag in tags_v2]
        
        # 1. 动作标签匹配分（最高优先级）
        action_score = 0
        matched_actions = []
        for target in target_actions:
            for parsed in parsed_tags:
                if target in parsed['type'] or parsed['type'] in target:
                    action_score += 5
                    matched_actions.append(parsed['full'])
        
        # 2. 镜头质量分
        quality_score = calculate_quality_score(parsed_tags)
        
        # 3. 泛标签匹配分（较低优先级）
        tag_score = 0
        matched_tags = []
        for target in target_tags:
            for parsed in parsed_tags:
                if target in parsed['type'] or parsed['type'] in target:
                    tag_score += 1
                    matched_tags.append(parsed['full'])
        
        # 4. 多样性分
        diversity_score = 0
        if filename not in used_materials:
            diversity_score += 2
        elif used_materials[filename] < 2:
            diversity_score += 1
        
        # 5. 连续重复惩罚
        if last_material and filename == last_material:
            action_score -= 10
        
        # 综合得分
        total_score = action_score + quality_score + tag_score + diversity_score
        
        scored_candidates.append({
            'candidate': candidate,
            'filename': filename,
            'tags_v2': tags_v2,
            'matched_actions': matched_actions,
            'matched_tags': matched_tags,
            'action_score': action_score,
            'quality_score': quality_score,
            'tag_score': tag_score,
            'diversity_score': diversity_score,
            'total_score': total_score
        })
    
    # 按总分排序
    scored_candidates.sort(key=lambda x: x['total_score'], reverse=True)
    
    if scored_candidates:
        best = scored_candidates[0]
        reason_parts = []
        if best['matched_actions']:
            reason_parts.append(f"动作匹配{len(best['matched_actions'])}个 ({', '.join(best['matched_actions'][:2])})")
        if best['quality_score'] > 0:
            reason_parts.append(f"镜头质量 +{best['quality_score']}")
        if best['diversity_score'] > 0:
            reason_parts.append(f"多样性 +{best['diversity_score']}")
        
        reason = "，".join(reason_parts) if reason_parts else "默认选择"
        return best['candidate'], reason, best['quality_score']
    else:
        return candidates[0] if candidates else None, "无候选", 0

if __name__ == '__main__':
    # 测试
    print("=== 语义选片 V2.0 测试 ===")
    
    materials = load_material_tags_v2()
    print(f"\n素材标签：{len(materials)}个素材")
    for filename, info in list(materials.items())[:3]:
        print(f"  {filename}:")
        print(f"    三层标签：{info['tags_v2']}")
    
    rules = load_scoring_rules()
    print(f"\n评分规则：")
    print(f"  镜头类型权重：{rules['镜头类型权重']}")
    
    # 测试标签解析
    print(f"\n标签解析测试：")
    test_tags = ["递发资料_近景_清晰", "面对面讲解_特写_清晰", "现场全景_稳定"]
    for tag in test_tags:
        parsed = parse_tag_v2(tag)
        quality_score = calculate_quality_score([parsed])
        print(f"  {tag} → 类型:{parsed['type']}, 镜头:{parsed['shot_type']}, 质量:{parsed['quality']}, 质量分:{quality_score}")
