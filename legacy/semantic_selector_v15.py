#!/usr/bin/env python3
"""
语义选片 V1.5（动作标签优先版）- 细粒度匹配

核心升级：
1. 动作标签匹配分 > 泛标签匹配分
2. 句子/短语级细粒度匹配
3. "发放资料"必须优先选到递资料镜头

不引入：
- 大模型
- 图像识别
"""
import os
import json
from typing import List, Dict, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(PROJECT_ROOT, 'config')

def load_material_tags_v15() -> Dict[str, Dict]:
    """加载 V1.5 素材标签（含动作标签）"""
    tags_path = os.path.join(CONFIG_DIR, 'material_tags_v15.json')
    with open(tags_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return data['materials']

def load_semantic_units() -> List[Dict]:
    """加载 V1.5 语义单元"""
    units_path = os.path.join(CONFIG_DIR, 'script_semantic_units_v15.json')
    with open(units_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return data['semantic_units']

def calculate_action_score(material_actions: List[str], target_actions: List[str]) -> Tuple[int, List[str]]:
    """
    计算动作标签匹配分（高优先级）
    
    评分规则：
    - 完全命中动作标签：+5 分/个（最高优先级）
    """
    score = 0
    matched = []
    
    for target in target_actions:
        if target in material_actions:
            score += 5  # 动作标签权重最高
            matched.append(target)
    
    return score, matched

def calculate_tag_score_v15(material_tags: List[str], target_tags: List[str]) -> Tuple[int, List[str]]:
    """
    计算泛标签匹配分（低优先级）
    
    评分规则：
    - 命中泛标签：+1 分/个
    """
    score = 0
    matched = []
    
    for target in target_tags:
        if target in material_tags:
            score += 1  # 泛标签权重较低
            matched.append(target)
    
    return score, matched

def select_best_material_v15(
    candidates: List[Dict],
    target_tags: List[str],
    target_actions: List[str],
    used_materials: Dict[str, int],
    last_material: str = None
) -> Tuple[Dict, str]:
    """
    V1.5 选片核心：动作标签优先
    
    评分优先级：
    1. 动作标签匹配分（最高，5 分/个）
    2. 泛标签匹配分（较低，1 分/个）
    3. 多样性分（未使用 +2）
    4. 连续重复惩罚（-10）
    """
    materials = load_material_tags_v15()
    
    scored_candidates = []
    
    for candidate in candidates:
        filename = candidate['name']
        info = materials.get(filename, {})
        tags = info.get('tags', [])
        actions = info.get('action_tags', [])
        
        # 1. 动作标签匹配分（最高优先级）
        action_score, matched_actions = calculate_action_score(actions, target_actions)
        
        # 2. 泛标签匹配分（较低优先级）
        tag_score, matched_tags = calculate_tag_score_v15(tags, target_tags)
        
        # 3. 多样性分
        diversity_score = 0
        if filename not in used_materials:
            diversity_score += 2
        elif used_materials[filename] < 2:
            diversity_score += 1
        
        # 4. 连续重复惩罚
        if last_material and filename == last_material:
            action_score -= 10  # 大幅减分
        
        # 综合得分（动作标签权重最高）
        total_score = action_score + tag_score + diversity_score
        
        scored_candidates.append({
            'candidate': candidate,
            'filename': filename,
            'tags': tags,
            'actions': actions,
            'matched_actions': matched_actions,
            'matched_tags': matched_tags,
            'action_score': action_score,
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
            reason_parts.append(f"动作匹配{len(best['matched_actions'])}个 ({', '.join(best['matched_actions'])})")
        if best['matched_tags']:
            reason_parts.append(f"标签匹配{len(best['matched_tags'])}个 ({', '.join(best['matched_tags'])})")
        if best['diversity_score'] > 0:
            reason_parts.append(f"多样性 +{best['diversity_score']}")
        
        reason = "，".join(reason_parts) if reason_parts else "默认选择"
        return best['candidate'], reason
    else:
        return candidates[0] if candidates else None, "无候选"

def semantic_select_v15(
    materials: List[str],
    semantic_units: List[Dict],
    target_duration: float
) -> List[Dict]:
    """
    V1.5 语义选片主函数
    
    Args:
        materials: 素材路径列表
        semantic_units: 语义单元列表（细粒度）
        target_duration: 目标总时长
    
    Returns:
        选中的镜头列表（含详细匹配信息）
    """
    selected_clips = []
    used_materials = {}
    last_material = None
    
    # 为每个语义单元选择素材
    for i, unit in enumerate(semantic_units):
        target_tags = unit['target_tags']
        target_actions = unit['target_actions']
        
        # 计算该单元需要的时长（按总时长平均分配）
        unit_duration = target_duration / len(semantic_units)
        
        # 构建候选素材（排除刚用过的）
        candidates = []
        for path in materials:
            filename = os.path.basename(path)
            if last_material and filename == last_material:
                continue  # 跳过连续重复
            candidates.append({
                'path': path,
                'name': filename
            })
        
        if not candidates:
            # 所有素材都用过了，重置
            candidates = [{'path': p, 'name': os.path.basename(p)} for p in materials]
        
        # 选择最佳素材（动作标签优先）
        best, reason = select_best_material_v15(
            candidates, 
            target_tags, 
            target_actions, 
            used_materials, 
            last_material
        )
        
        if best:
            selected_clips.append({
                'unit': unit,
                'material': best,
                'reason': reason,
                'duration': unit_duration
            })
            
            # 更新使用记录
            filename = best['name']
            used_materials[filename] = used_materials.get(filename, 0) + 1
            last_material = filename
    
    return selected_clips

if __name__ == '__main__':
    # 测试
    print("=== 语义选片 V1.5 测试 ===")
    
    materials = load_material_tags_v15()
    print(f"\n素材标签：{len(materials)}个素材")
    for filename, info in list(materials.items())[:3]:
        print(f"  {filename}:")
        print(f"    标签：{info['tags']}")
        print(f"    动作：{info['action_tags']}")
    
    units = load_semantic_units()
    print(f"\n语义单元：{len(units)}个单元")
    for unit in units[:3]:
        print(f"  单元{unit['id']}: {unit['text'][:20]}...")
        print(f"    目标动作：{unit['target_actions']}")
