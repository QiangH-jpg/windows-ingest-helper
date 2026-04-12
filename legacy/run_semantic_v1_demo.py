#!/usr/bin/env python3
"""
语义选片 V1 演示脚本

核心逻辑：
1. 加载素材标签
2. 加载稿件片段规则
3. 为每个片段选择标签匹配的素材
4. 输出选片明细

不破坏现有稳定链路，仅演示语义选片效果。
"""
import os, sys, json, uuid, asyncio
sys.path.insert(0, '/home/admin/.openclaw/workspace/video-tool')

from core.storage import storage
from pipeline import processor
from pipeline.tts_provider import generate_tts, create_subtitle_srt_from_meta
from pipeline.project_state import validate_script, validate_task
from pipeline.video_cache import get_or_create_processed, extract_dynamic_clip
from pipeline.semantic_selector import load_material_tags, load_script_rules, select_best_material
from pipeline.memory_guard import enforce_pre_check, get_guard

# ============================================================
# 固定素材清单
# ============================================================
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

# ============================================================
# 固定新闻稿（按片段拆分）
# ============================================================
SCRIPT_SEGMENTS = [
    {
        'id': 1,
        'text': '3 月 26 日，济南市人社局在美团服务中心开展"人社服务大篷车"活动。',
        'target_tags': ['现场全景', '横幅', '合影']
    },
    {
        'id': 2,
        'text': '活动以"走进奔跑者——保障与你同行"为主题，把人社服务送到外卖骑手等一线劳动者。',
        'target_tags': ['外卖骑手', '合影', '横幅']
    },
    {
        'id': 3,
        'text': '现场通过发放资料、面对面讲解，向小哥介绍社保参保、权益保障等政策。',
        'target_tags': ['发放资料', '讲解交流', '政策宣传']
    },
    {
        'id': 4,
        'text': '还有互动环节，让大家在轻松氛围中了解政策。',
        'target_tags': ['互动环节', '外卖骑手', '轻松氛围']
    },
    {
        'id': 5,
        'text': '济南市人社局持续推动服务走近新就业形态劳动者，打通保障"最后一公里"。',
        'target_tags': ['领导交流', '讲解交流', '现场全景']
    }
]

def main():
    """语义选片 V1 主流程"""
    enforce_pre_check()
    guard = get_guard()
    
    print("=" * 60)
    print("语义选片 V1（规则版）- 演示")
    print("=" * 60)
    
    # 1. 加载素材标签
    print("\n[1] 加载素材标签...")
    material_tags = load_material_tags()
    print(f"  已加载 {len(material_tags)} 个素材标签")
    
    # 2. 加载稿件片段规则
    print("\n[2] 加载稿件片段规则...")
    script_rules = load_script_rules()
    print(f"  已加载 {len(script_rules)} 个片段规则")
    
    # 3. 验证任务
    print("\n[3] 验证任务...")
    validation = validate_task('语义选片 V1 演示')
    if validation['decision'] == 'reject':
        print(f"  ✗ 任务被拒绝：{validation['reason']}")
        return
    print("  ✓ 任务验证通过")
    
    # 4. 生成 task_id
    task_id = str(uuid.uuid4())
    print(f"\n[4] 任务 ID: {task_id}")
    
    # 5. TTS 合成
    print("\n[5] TTS 合成...")
    full_script = ' '.join(seg['text'] for seg in SCRIPT_SEGMENTS)
    tts_path = os.path.join(storage.workdir, f"{task_id}_tts.mp3")
    tts_meta_path = os.path.join(storage.workdir, f"{task_id}_tts_meta.json")
    tts_meta = asyncio.run(generate_tts(full_script, tts_path, tts_meta_path))
    tts_duration = tts_meta['total_duration']
    print(f"  TTS 时长：{tts_duration:.2f} 秒")
    
    # 6. 语义选片
    print("\n[6] 语义选片（按标签匹配）...")
    
    selected_clips = []
    used_materials = {}
    last_material = None
    
    for i, segment in enumerate(SCRIPT_SEGMENTS):
        print(f"\n  片段{i+1}: {segment['text'][:30]}...")
        print(f"    目标标签：{segment['target_tags']}")
        
        # 构建候选素材（排除连续重复）
        candidates = []
        for path in FIXED_MATERIALS:
            filename = os.path.basename(path)
            if last_material and filename == last_material:
                continue
            candidates.append({
                'path': path,
                'name': filename
            })
        
        # 选择最佳匹配
        best, reason = select_best_material(candidates, segment['target_tags'], used_materials, last_material)
        
        if best:
            print(f"    选中素材：{best['name']}")
            print(f"    说明：{reason}")
            
            # 记录选择
            selected_clips.append({
                'segment': segment,
                'material': best,
                'reason': reason
            })
            
            # 更新使用记录
            filename = best['name']
            used_materials[filename] = used_materials.get(filename, 0) + 1
            last_material = filename
    
    # 7. 输出选片明细
    print("\n" + "=" * 60)
    print("【选片明细】")
    print("=" * 60)
    
    for i, item in enumerate(selected_clips):
        segment = item['segment']
        material = item['material']
        reason = item['reason']
        
        print(f"\n片段{i+1}:")
        print(f"  稿件：{segment['text']}")
        print(f"  目标标签：{segment['target_tags']}")
        print(f"  选中素材：{material['name']}")
        print(f"  说明：{reason}")
    
    # 8. 保存选片结果
    result_path = os.path.join(storage.workdir, f"{task_id}_selection.json")
    with open(result_path, 'w', encoding='utf-8') as f:
        json.dump({
            'task_id': task_id,
            'segments': [
                {
                    'id': item['segment']['id'],
                    'text': item['segment']['text'],
                    'target_tags': item['segment']['target_tags'],
                    'selected_material': item['material']['name'],
                    'reason': item['reason']
                }
                for item in selected_clips
            ]
        }, f, ensure_ascii=False, indent=2)
    
    print(f"\n选片结果已保存：{result_path}")
    print("\n✅ 语义选片 V1 演示完成")
    
    return task_id, selected_clips

if __name__ == '__main__':
    main()
