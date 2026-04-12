#!/usr/bin/env python3
"""
语义选片 V1.5 演示脚本 - 动作标签优先

核心验证：
- "发放资料" → 必须选到"递发资料"镜头
- "互动环节" → 必须选到"投掷互动"或"问答互动"镜头
- "领导交流" → 必须选到"领导对骑手讲话"镜头
"""
import os, sys, json, uuid, asyncio
sys.path.insert(0, '/home/admin/.openclaw/workspace/video-tool')

from core.storage import storage
from pipeline.tts_provider import generate_tts
from pipeline.project_state import validate_task
from pipeline.semantic_selector_v15 import load_material_tags_v15, load_semantic_units, select_best_material_v15
from pipeline.memory_guard import enforce_pre_check, get_guard

FIXED_MATERIALS = [
    '/home/admin/.openclaw/workspace/video-tool/uploads/394A0108.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/394A0109.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115140223_0109_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115140336_0110_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115142627_0112_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115143401_0119_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115143406_0120_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115143625_0127_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115143827_0133_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115144146_0143_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115144241_0146_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115144510_0148_D.MP4',
]

def main():
    enforce_pre_check()
    guard = get_guard()
    
    print("=" * 60)
    print("语义选片 V1.5（动作标签优先）- 演示")
    print("=" * 60)
    
    # 1. 加载素材标签
    print("\n[1] 加载素材动作标签...")
    materials = load_material_tags_v15()
    print(f"  已加载 {len(materials)} 个素材（含动作标签）")
    
    # 2. 加载语义单元
    print("\n[2] 加载语义单元...")
    units = load_semantic_units()
    print(f"  已加载 {len(units)} 个语义单元（细粒度）")
    
    # 3. 验证任务
    print("\n[3] 验证任务...")
    validation = validate_task('语义选片 V1.5 演示')
    if validation['decision'] == 'reject':
        print(f"  ✗ 任务被拒绝：{validation['reason']}")
        return
    print("  ✓ 任务验证通过")
    
    # 4. 生成 task_id
    task_id = str(uuid.uuid4())
    print(f"\n[4] 任务 ID: {task_id}")
    
    # 5. TTS 合成
    print("\n[5] TTS 合成...")
    full_script = ' '.join(unit['text'] for unit in units)
    tts_path = os.path.join(storage.workdir, f"{task_id}_tts.mp3")
    tts_meta_path = os.path.join(storage.workdir, f"{task_id}_tts_meta.json")
    tts_meta = asyncio.run(generate_tts(full_script, tts_path, tts_meta_path))
    tts_duration = tts_meta['total_duration']
    print(f"  TTS 时长：{tts_duration:.2f} 秒")
    
    # 6. 语义选片 V1.5
    print("\n[6] 语义选片 V1.5（动作标签优先）...")
    
    selected_clips = []
    used_materials = {}
    last_material = None
    
    for i, unit in enumerate(units):
        print(f"\n  单元{i+1}: {unit['text'][:30]}...")
        print(f"    目标动作：{unit['target_actions']}")
        print(f"    目标标签：{unit['target_tags']}")
        
        # 构建候选素材
        candidates = []
        for path in FIXED_MATERIALS:
            filename = os.path.basename(path)
            if last_material and filename == last_material:
                continue
            candidates.append({'path': path, 'name': filename})
        
        if not candidates:
            candidates = [{'path': p, 'name': os.path.basename(p)} for p in FIXED_MATERIALS]
        
        # 选择最佳素材（动作标签优先）
        best, reason = select_best_material_v15(
            candidates, 
            unit['target_tags'], 
            unit['target_actions'], 
            used_materials, 
            last_material
        )
        
        if best:
            print(f"    选中素材：{best['name']}")
            print(f"    说明：{reason}")
            
            selected_clips.append({
                'unit': unit,
                'material': best,
                'reason': reason
            })
            
            filename = best['name']
            used_materials[filename] = used_materials.get(filename, 0) + 1
            last_material = filename
    
    # 7. 输出选片明细
    print("\n" + "=" * 60)
    print("【语义选片 V1.5 明细】")
    print("=" * 60)
    
    action_match_count = 0
    for i, item in enumerate(selected_clips):
        unit = item['unit']
        material = item['material']
        reason = item['reason']
        
        # 检查是否命中动作标签
        has_action_match = '动作匹配' in reason
        
        print(f"\n单元{i+1}:")
        print(f"  稿件：{unit['text']}")
        print(f"  目标动作：{unit['target_actions']}")
        print(f"  选中素材：{material['name']}")
        print(f"  说明：{reason}")
        print(f"  动作命中：{'✅ 是' if has_action_match else '❌ 否'}")
        
        if has_action_match:
            action_match_count += 1
    
    # 8. 统计
    print("\n" + "=" * 60)
    print("【统计】")
    print("=" * 60)
    print(f"  总语义单元：{len(units)}个")
    print(f"  动作标签命中：{action_match_count}个 ({action_match_count/len(units)*100:.0f}%)")
    print(f"  使用素材数：{len(used_materials)}个")
    
    # 9. 保存结果
    result_path = os.path.join(storage.workdir, f"{task_id}_v15_selection.json")
    with open(result_path, 'w', encoding='utf-8') as f:
        json.dump({
            'task_id': task_id,
            'total_units': len(units),
            'action_match_count': action_match_count,
            'action_match_rate': action_match_count / len(units),
            'selections': [
                {
                    'unit_id': item['unit']['id'],
                    'text': item['unit']['text'],
                    'target_actions': item['unit']['target_actions'],
                    'selected_material': item['material']['name'],
                    'reason': item['reason']
                }
                for item in selected_clips
            ]
        }, f, ensure_ascii=False, indent=2)
    
    print(f"\n选片结果已保存：{result_path}")
    print("\n✅ 语义选片 V1.5 演示完成")
    
    return task_id, selected_clips

if __name__ == '__main__':
    main()
