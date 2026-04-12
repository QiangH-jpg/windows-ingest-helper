#!/usr/bin/env python3
"""
语义选片 V1（规则版）- 轻量标签匹配

核心逻辑：
1. 加载素材标签
2. 加载稿件片段规则
3. 按标签匹配度选择素材
4. 结合调度规则（不破坏现有逻辑）

不引入：
- 大模型
- 图像识别
- OCR
- Embedding
"""
import os
import json
from typing import List, Dict, Tuple

# 配置目录在项目根目录的 config 下
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(PROJECT_ROOT, 'config')

def load_material_tags() -> Dict[str, List[str]]:
    """加载素材标签"""
    tags_path = os.path.join(CONFIG_DIR, 'material_tags.json')
    with open(tags_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # 转换为 {filename: [tags]} 格式
    result = {}
    for filename, info in data['materials'].items():
        result[filename] = info['tags']
    return result

def load_script_rules() -> List[Dict]:
    """加载稿件片段规则"""
    rules_path = os.path.join(CONFIG_DIR, 'script_tag_rules.json')
    with open(rules_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return data['script_segments']

def get_current_segment(script_text: str, position: float, total_duration: float) -> Dict:
    """
    根据当前时间位置，确定属于哪个稿件片段
    
    简化版：按时间比例分配
    """
    rules = load_script_rules()
    if not rules:
        return None
    
    # 按时间比例估算当前片段
    progress = position / total_duration if total_duration > 0 else 0
    segment_index = int(progress * len(rules))
    segment_index = min(segment_index, len(rules) - 1)
    
    return rules[segment_index]

def calculate_tag_score(material_tags: List[str], target_tags: List[str]) -> int:
    """
    计算素材标签与目标标签的匹配分
    
    评分规则：
    - 完全命中主标签：+3 分/个
    - 命中次标签：+2 分/个
    - 命中泛标签：+1 分/个
    """
    score = 0
    matched_tags = []
    
    for target in target_tags:
        if target in material_tags:
            # 主标签（具体场景）
            if target in ['发放资料', '讲解交流', '互动环节', '外卖骑手', '合影', '横幅']:
                score += 3
                matched_tags.append(target)
            # 次标签（一般场景）
            elif target in ['现场全景', '政策宣传', '轻松氛围']:
                score += 2
                matched_tags.append(target)
            # 泛标签
            else:
                score += 1
                matched_tags.append(target)
    
    return score, matched_tags

def select_best_material(
    candidates: List[Dict],
    target_tags: List[str],
    used_materials: Dict[str, int],
    last_material: str = None
) -> Tuple[Dict, str]:
    """
    从候选素材中选择最佳匹配
    
    Args:
        candidates: 候选素材列表 [{'path': ..., 'name': ..., 'start': ..., 'duration': ...}]
        target_tags: 目标标签列表
        used_materials: 已使用素材计数字典 {filename: count}
        last_material: 上一个使用的素材名
    
    Returns:
        (best_material, reason)
    """
    material_tags = load_material_tags()
    
    scored_candidates = []
    
    for candidate in candidates:
        filename = os.path.basename(candidate['path'])
        tags = material_tags.get(filename, [])
        
        # 1. 标签匹配分（最高优先级）
        tag_score, matched_tags = calculate_tag_score(tags, target_tags)
        
        # 2. 多样性分（未使用过的素材加分）
        diversity_score = 0
        if filename not in used_materials:
            diversity_score += 2
        elif used_materials[filename] < 2:
            diversity_score += 1
        
        # 3. 连续重复惩罚（同素材不能连续使用）
        if last_material and filename == last_material:
            tag_score -= 10  # 大幅减分，几乎禁用
        
        # 综合得分
        total_score = tag_score + diversity_score
        
        scored_candidates.append({
            'candidate': candidate,
            'filename': filename,
            'tags': tags,
            'matched_tags': matched_tags,
            'tag_score': tag_score,
            'diversity_score': diversity_score,
            'total_score': total_score
        })
    
    # 按总分排序
    scored_candidates.sort(key=lambda x: x['total_score'], reverse=True)
    
    if scored_candidates:
        best = scored_candidates[0]
        reason = f"标签匹配{len(best['matched_tags'])}个 ({', '.join(best['matched_tags'])})，多样性 +{best['diversity_score']}"
        return best['candidate'], reason
    else:
        # 无候选，返回第一个
        return candidates[0] if candidates else None, "无候选，默认选择"

def semantic_select_clips(
    materials: List[str],
    script_segments: List[Dict],
    target_duration: float
) -> List[Dict]:
    """
    语义选片主函数
    
    Args:
        materials: 素材路径列表
        script_segments: 稿件片段列表
        target_duration: 目标总时长
    
    Returns:
        选中的镜头列表
    """
    selected_clips = []
    used_materials = {}
    last_material = None
    current_time = 0.0
    
    # 为每个稿件片段选择素材
    for i, segment in enumerate(script_segments):
        target_tags = segment['target_tags']
        
        # 计算该片段需要的时长（按字数比例估算）
        segment_duration = target_duration / len(script_segments)
        
        # 构建候选素材（排除刚用过的）
        candidates = []
        for path in materials:
            filename = os.path.basename(path)
            if last_material and filename == last_material:
                continue  # 跳过连续重复
            candidates.append({
                'path': path,
                'name': filename,
                'duration': segment_duration
            })
        
        if not candidates:
            # 所有素材都用过了，重置
            candidates = [{'path': p, 'name': os.path.basename(p), 'duration': segment_duration} for p in materials]
        
        # 选择最佳素材
        best, reason = select_best_material(candidates, target_tags, used_materials, last_material)
        
        if best:
            selected_clips.append({
                **best,
                'segment_id': segment['id'],
                'segment_text': segment['text'],
                'target_tags': target_tags,
                'selection_reason': reason
            })
            
            # 更新使用记录
            filename = best['name']
            used_materials[filename] = used_materials.get(filename, 0) + 1
            last_material = filename
            current_time += segment_duration
    
    return selected_clips

if __name__ == '__main__':
    # 测试
    print("=== 语义选片 V1 测试 ===")
    
    material_tags = load_material_tags()
    print(f"\n素材标签：{len(material_tags)}个素材")
    for filename, tags in list(material_tags.items())[:3]:
        print(f"  {filename}: {tags}")
    
    script_rules = load_script_rules()
    print(f"\n稿件片段：{len(script_rules)}个片段")
    for seg in script_rules:
        print(f"  片段{seg['id']}: {seg['text'][:20]}... → {seg['target_tags']}")
