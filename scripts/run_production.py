#!/usr/bin/env python3
"""
生产级唯一主链入口（V2 语义选片安全接回版）

调用链：
V1 → [V2 语义选片] → V3 → V3.5 → V4 → V5

开关：
ENABLE_V2_SEMANTIC = True/False
"""
import os
import sys
import json
import uuid
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import init_directories, DATA_DIR, OUTPUTS_APPROVED_DIR, TASK_DIR, UPLOADS_DIR
init_directories()

from v1_materials.material_pool import MaterialPool
from v2_semantic.semantic_planner import SemanticPlanner
from v3_timeline.timeline_orchestrator import TimelineOrchestrator
from v3_timeline.clip_extractor import ClipExtractor
from v4_render.render_engine import RenderEngine
from v5_gate.quality_gate import QualityGate

# ============================================
# 从 baseline.yaml 加载配置（禁止硬编码）
# ============================================
import yaml
baseline_path = PROJECT_ROOT / 'config' / 'baseline.yaml'
with open(baseline_path, 'r') as f:
    BASELINE = yaml.safe_load(f)

ENABLE_V2_SEMANTIC = BASELINE.get('ENABLE_V2', True)
ENABLE_V3_EXTEND = BASELINE.get('ENABLE_V3_EXTEND', True)

# 环境变量可覆盖（调试用）
if os.getenv('ENABLE_V2_SEMANTIC'):
    ENABLE_V2_SEMANTIC = os.getenv('ENABLE_V2_SEMANTIC').lower() == 'true'

ORIGINAL_SCRIPT = """3 月 26 日，济南市人社局在美团服务中心开展"人社服务大篷车"活动。

活动以"走进奔跑者——保障与你同行"为主题，把人社服务送到外卖骑手等一线劳动者。

现场通过发放资料、面对面讲解，向小哥介绍社保参保、权益保障等政策。

还有互动环节，让大家在轻松氛围中了解政策。

济南市人社局持续推动服务走近新就业形态劳动者，打通保障"最后一公里"。"""

def main():
    print("=" * 60)
    mode = "V2 语义选片" if ENABLE_V2_SEMANTIC else "基线（顺序选片）"
    print(f"生产级唯一主链 — 模式：{mode}")
    print("=" * 60)
    
    task_id = str(uuid.uuid4())
    timestamp = datetime.now().isoformat()
    print(f"\n任务 ID: {task_id}")
    print(f"ENABLE_V2_SEMANTIC: {ENABLE_V2_SEMANTIC}")
    
    # 检查素材
    materials_dir = UPLOADS_DIR / 'materials'
    materials = list(materials_dir.glob('*.MP4')) + list(materials_dir.glob('*.mov'))
    if not materials:
        print(f"\n❌ 未找到素材")
        return False, task_id
    print(f"\n找到素材：{len(materials)}个")
    
    # ========================================
    # V1 素材层
    # ========================================
    print("\n[V1 素材层] 标准化素材...")
    material_pool = MaterialPool()
    standardized_materials = material_pool.get_all_standardized()
    print(f"  标准化素材：{len(standardized_materials)}个")
    
    # ========================================
    # V3 时序层（先 TTS 获取音频时长）
    # ========================================
    print("\n[V3 时序层] 生成 TTS + 时间轴...")
    render_engine = RenderEngine()
    tts_path, tts_meta = render_engine.generate_tts(ORIGINAL_SCRIPT)
    audio_duration = tts_meta['total_duration']
    print(f"  TTS 时长：{audio_duration:.2f}s")
    
    timeline_orchestrator = TimelineOrchestrator()
    
    if ENABLE_V2_SEMANTIC:
        # ========================================
        # V2 语义选片层（启用）
        # ========================================
        print("\n[V2 语义选片层] 生成 shot_plan...")
        semantic_planner = SemanticPlanner()
        shot_plan = semantic_planner.generate_shot_plan(standardized_materials)
        print(f"  语义单元：{len(shot_plan)}个")
        for i, shot in enumerate(shot_plan[:3]):
            print(f"    单元{shot['unit_id']}: {shot['selected_material']} — {shot['reason']}")
        if len(shot_plan) > 3:
            print(f"    ... (共{len(shot_plan)}个)")
        
        # V3 从 shot_plan 生成 timeline
        rhythm_opt = BASELINE.get('ENABLE_RHYTHM_OPT', False)
        timeline = timeline_orchestrator.generate_timeline_from_shot_plan(shot_plan, audio_duration, rhythm_opt=rhythm_opt)
    else:
        # ========================================
        # 基线模式（顺序选片）
        # ========================================
        print("\n[V2 跳过] 使用基线模式（顺序选片）")
        timeline = timeline_orchestrator.generate_timeline(standardized_materials, audio_duration)
    
    print(f"  时间轴：{len(timeline)}个镜头")
    
    # ========================================
    # V3.5 裁剪层
    # ========================================
    print("\n[V3.5 裁剪层] 裁剪子片段...")
    clip_extractor = ClipExtractor()
    extracted_clips = clip_extractor.extract_clips(timeline_orchestrator.timeline_path)
    print(f"  裁剪完成：{len(extracted_clips)}个子片段")
    
    total_clip_duration = sum(c['actual_duration'] for c in extracted_clips)
    print(f"  子片段总时长：{total_clip_duration:.2f}s（目标：{audio_duration:.2f}s）")
    
    # ========================================
    # V4 渲染层
    # ========================================
    print("\n[V4 渲染层] 合成视频...")
    subtitle_path = render_engine.generate_subtitles(tts_meta)
    candidate_path = render_engine.assemble_video(extracted_clips, tts_path, subtitle_path)
    print(f"  待校验成片：{candidate_path}")
    
    if not os.path.exists(candidate_path):
        print(f"\n❌ 视频合成失败")
        return False, task_id
    
    # ========================================
    # V5 校验发布层
    # ========================================
    print("\n[V5 校验发布层] 真正门禁校验...")
    quality_gate = QualityGate()
    passed, result = quality_gate.approve(
        candidate_path=candidate_path,
        audio_path=tts_path,
        task_id=task_id,
        clips_dir=clip_extractor.clips_dir,
        subtitle_path=subtitle_path,
        original_text=ORIGINAL_SCRIPT
    )
    
    if passed:
        print(f"\n✅ 校验通过")
        print(f"  合格成片：{result}")
    else:
        print(f"\n❌ 校验失败")
        print(f"  失败报告：{result}")
    
    # 保存任务记录（双写）
    task_record = {
        'id': task_id,
        'timestamp': timestamp,
        'status': 'approved' if passed else 'rejected',
        'mode': 'semantic' if ENABLE_V2_SEMANTIC else 'baseline',
        'materials_count': len(standardized_materials),
        'timeline_clips_count': len(timeline),
        'audio_duration': audio_duration,
        'clip_total_duration': total_clip_duration,
        'output_path': str(result) if passed else str(candidate_path),
        'result_path': str(result),
        'passed': passed
    }
    
    for task_dir in [TASK_DIR, Path('/home/admin/.openclaw/workspace/video-tool/workdir/tasks')]:
        task_dir.mkdir(parents=True, exist_ok=True)
        with open(task_dir / f'{task_id}.json', 'w', encoding='utf-8') as f:
            json.dump(task_record, f, ensure_ascii=False, indent=2)
    
    print(f"\n" + "=" * 60)
    print(f"生产链执行完成 {'✅' if passed else '❌'} (模式：{mode})")
    print("=" * 60)
    
    return passed, task_id

if __name__ == '__main__':
    success, task_id = main()
    sys.exit(0 if success else 1)
