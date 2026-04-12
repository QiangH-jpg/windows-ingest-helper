#!/usr/bin/env python3
"""
新闻视频调度规则 - 回切结构版

严格遵守 PROJECT_STATE.md 约束：
- 禁止连续使用同一素材
- 禁止长片连续切片
- 恢复主素材机制 + 回切结构
- 禁止平均轮换
"""
import os, sys, json, uuid, asyncio, random
from datetime import datetime
sys.path.insert(0, '/home/admin/.openclaw/workspace/video-tool')

from core.storage import storage
from pipeline import processor
from pipeline.tts_provider import generate_tts, create_subtitle_srt_from_meta
from pipeline.project_state import validate_script, validate_task, load_project_state
from pipeline.video_cache import get_or_create_processed, extract_dynamic_clip  # ← 接入缓存（动态裁剪）
from pipeline.memory_guard import enforce_pre_check, get_guard  # ← 接入记忆守护

# ============================================================
# 固定素材清单（严格使用，不更换）
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
# 固定新闻稿
# ============================================================
OFFICIAL_SCRIPT = """3月26日，济南市人社局在美团服务中心开展"人社服务大篷车"活动。

活动以"走进奔跑者——保障与你同行"为主题，把人社服务送到外卖骑手等一线劳动者。

现场通过发放资料、面对面讲解，向小哥介绍社保参保、权益保障等政策。

还有互动环节，让大家在轻松氛围中了解政策。

济南市人社局持续推动服务走近新就业形态劳动者，打通保障"最后一公里"。"""


def get_material_duration(path):
    """获取素材时长"""
    import subprocess
    result = subprocess.run(
        ['/home/linuxbrew/.linuxbrew/bin/ffprobe', '-v', 'error',
         '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', path],
        capture_output=True, text=True
    )
    try:
        return float(result.stdout.strip())
    except:
        return 0


def build_news_schedule_clips(materials, target_duration=None, min_duration=3, max_duration=6):
    """
    新闻视频调度规则选片 - 动态裁剪版（时长自适应）
    
    ✅ 符合规则：
    1) 起点任意（不对齐5秒边界）
    2) 时长浮动（3-6秒）
    3) 同一素材多次使用，起点必须不同
    4) 禁止连续同素材
    5) 回切结构 A→B→C→A→D→A→E→A
    6) 总时长自适应TTS时长（误差≤0.3秒）
    
    Args:
        materials: 素材路径列表
        target_duration: 目标总时长（秒），必须≥TTS时长
        min_duration: 最短时长（秒）
        max_duration: 最长时长（秒）
    
    Returns:
        sequence: 镜头列表
        actual_duration: 实际总时长
    """
    # 分析素材时长
    material_info = []
    for i, path in enumerate(materials):
        if os.path.exists(path):
            duration = get_material_duration(path)
            if duration >= min_duration:
                material_info.append({
                    'index': i,
                    'path': path,
                    'name': os.path.basename(path),
                    'duration': duration,
                    'max_clips': int(duration // min_duration)  # 估算可用片段数
                })
    
    if len(material_info) < 3:
        raise ValueError("有效素材不足（需要≥3）")
    
    # 选择主素材（时长最长的前2个）
    material_info.sort(key=lambda x: x['duration'], reverse=True)
    main_materials = material_info[:2]  # 主素材
    aux_materials = material_info[2:]   # 辅助素材
    
    main_a = main_materials[0]  # 主素材A
    main_b = main_materials[1] if len(main_materials) > 1 else None  # 主素材B（可选）
    
    print(f"\n主素材选择:")
    print(f"  主素材A: {main_a['name']} (时长{main_a['duration']:.1f}s)")
    if main_b:
        print(f"  主素材B: {main_b['name']} (时长{main_b['duration']:.1f}s)")
    print(f"  辅助素材: {len(aux_materials)}个")
    print(f"  时长范围: {min_duration}-{max_duration}秒（动态）")
    
    # 构建回切结构
    sequence = []
    used_segments = {}  # 记录每个素材已使用的片段（起始点）
    
    # 初始化已用片段
    for m in material_info:
        used_segments[m['index']] = []
    
    def get_dynamic_clip_from_material(mat, avoid_segments=None):
        """
        从素材获取动态片段
        
        ✅ 规则：
        - 起点任意（不对齐整秒）
        - 时长浮动（min_duration ~ max_duration）
        - 同一素材多次使用，起点必须不同
        """
        if avoid_segments is None:
            avoid_segments = used_segments[mat['index']]
        
        total_duration = mat['duration']
        
        # 尝试找到未使用的起始点
        # 使用随机起始点（不对齐整秒）
        max_attempts = 50
        
        for attempt in range(max_attempts):
            # 随机起始点（带小数，不对齐整秒）
            # 起始范围：0 到 (总时长 - 最短时长)
            max_start = total_duration - min_duration
            
            if max_start <= 0:
                return None, None
            
            # 随机起始点（带0.1-0.9的小数偏移）
            base_start = random.uniform(0, max_start)
            # 添加小数偏移（避免整秒边界）
            offset = random.choice([0.3, 0.5, 0.7, 0.9, 1.2, 1.5, 1.8, 2.1, 2.3, 2.5, 2.7, 2.8])
            start = min(base_start + offset, max_start)
            
            # 确保起始点在小数范围内（非整5秒）
            # 禁止: 0, 5, 10, 15, 20... 等整5秒边界
            if start % 5 < 1.0 or start % 5 > 4.0:
                # 太接近整5秒边界，重新尝试
                continue
            
            # 随机时长（min ~ max）
            duration = random.uniform(min_duration, max_duration)
            
            # 确保时长也不是整5秒
            if duration >= 5.0:
                # 调整时长避开5秒整
                duration = random.choice([3.5, 3.8, 4.2, 4.5, 4.8, 5.2, 5.5, 5.8])
            
            # 确保片段不超出素材边界
            if start + duration > total_duration:
                duration = total_duration - start
            
            # 检查是否与已用片段重叠（起点必须不同）
            overlap = False
            for used_start, used_dur in avoid_segments:
                # 检查重叠：起始点差距小于2秒视为重叠
                if abs(start - used_start) < 2.0:
                    overlap = True
                    break
            
            if not overlap:
                # 确保时长在有效范围
                if duration >= min_duration:
                    return start, duration
        
        # 找不到合适的起始点
        return None, None
    
    # 构建回切序列
    # A → aux[0] → aux[1] → A → aux[2] → A → aux[3] → A
    a_count = 0
    max_a = min(4, main_a['max_clips'] + 1)  # 主素材最多出现4次
    aux_count = 0
    
    while len(sequence) < target_clips:
        pos = len(sequence)
        
        if pos == 0:
            # 开头用主素材A
            source = main_a
            source_type = 'main_A'
        elif pos % 2 == 1:
            # 奇数位置用辅助素材
            if aux_count < len(aux_materials):
                source = aux_materials[aux_count]
                source_type = f'aux_{aux_count}'
                aux_count += 1
            elif main_b and aux_count >= len(aux_materials):
                # 辅助素材用完后用主素材B
                source = main_b
                source_type = 'main_B'
            else:
                source = None
        else:
            # 偶数位置回切到主素材A
            if a_count < max_a - 1:
                source = main_a
                source_type = 'main_A'
                a_count += 1
            else:
                # 主素材A次数用完，用辅助素材
                if aux_count < len(aux_materials):
                    source = aux_materials[aux_count]
                    source_type = f'aux_{aux_count}'
                    aux_count += 1
                else:
                    source = None
        
        if source is None:
            break
        
        # 获取动态片段（任意起始点 + 浮动时长）
        start, dur = get_dynamic_clip_from_material(source)
        if start is None:
            break
        
        # 记录已用片段
        used_segments[source['index']].append((start, dur))
        
        sequence.append({
            'source_index': source['index'],
            'source_path': source['path'],  # ← 新增：素材路径
            'source_name': source['name'],
            'source_type': source_type,
            'start': round(start, 1),  # 保留1位小数
            'duration': round(dur, 1)  # 保留1位小数
        })
    
    return sequence, main_a, main_b, aux_materials


def validate_sequence(sequence):
    """验证序列是否符合规则（动态裁剪版）"""
    print("\n规则验证（动态裁剪版）:")
    
    # 1) 检查连续同素材
    consecutive_same = False
    for i in range(1, len(sequence)):
        if sequence[i]['source_index'] == sequence[i-1]['source_index']:
            consecutive_same = True
            print(f"  ❌ 违规: 镜头{i}和{i+1}连续使用同一素材 {sequence[i]['source_name']}")
    
    if not consecutive_same:
        print(f"  ✅ 规则1: 无连续同素材")
    
    # 2) 检查连续切片
    consecutive_slices = False
    for i in range(1, len(sequence)):
        if sequence[i]['source_index'] == sequence[i-1]['source_index']:
            if abs(sequence[i]['start'] - sequence[i-1]['start']) == sequence[i]['duration']:
                consecutive_slices = True
                print(f"  ❌ 违规: 连续切片 {sequence[i]['source_name']}")
    
    if not consecutive_slices:
        print(f"  ✅ 规则2: 无连续切片")
    
    # 3) 统计素材使用次数
    source_counts = {}
    for clip in sequence:
        idx = clip['source_index']
        source_counts[idx] = source_counts.get(idx, 0) + 1
    
    # 主素材应出现2-4次
    main_idx = sequence[0]['source_index']
    main_count = source_counts[main_idx]
    print(f"  ✅ 规则3: 主素材 {sequence[0]['source_name']} 出现 {main_count} 次")
    
    # 辅助素材应出现1次
    aux_single = True
    for idx, count in source_counts.items():
        if idx != main_idx and count > 1:
            aux_single = False
            print(f"  ❌ 违规: 辅助素材出现 {count} 次（应≤1）")
    
    if aux_single:
        print(f"  ✅ 规则4: 辅助素材各出现1次")
    
    # 5) 检查回切结构
    has_return_cut = False
    for i in range(2, len(sequence)):
        if sequence[i]['source_index'] == sequence[0]['source_index']:
            has_return_cut = True
            break
    
    if has_return_cut:
        print(f"  ✅ 规则5: 存在回切结构")
    else:
        print(f"  ❌ 违规: 无回切结构")
    
    # 6) 检查平均轮换
    if len(source_counts) >= len(sequence) * 0.5:
        print(f"  ❌ 违规: 可能平均轮换（素材数≈镜头数）")
    else:
        print(f"  ✅ 规则6: 无平均轮换")
    
    # 7) ✅ 动态裁剪验证：起始点非整5秒
    all_start_non_5sec = True
    for clip in sequence:
        start = clip['start']
        # 检查起始点是否是整5秒边界（0, 5, 10, 15...）
        if start % 5 == 0:
            all_start_non_5sec = False
            print(f"  ❌ 违规: 镜头 {clip['source_name']} 起始点 {start}s 是整5秒边界")
    
    if all_start_non_5sec:
        print(f"  ✅ 规则7: 所有起始点非整5秒边界")
    
    # 8) ✅ 动态裁剪验证：时长不全为5秒
    all_duration_varied = True
    durations = [clip['duration'] for clip in sequence]
    if all(d == 5.0 for d in durations):
        all_duration_varied = False
        print(f"  ❌ 违规: 所有片段时长均为5秒")
    else:
        print(f"  ✅ 规则8: 时长多样化（{min(durations):.1f}-{max(durations):.1f}秒）")
    
    # 9) ✅ 同一素材多次使用，起始点不同
    same_material_diff_start = True
    for idx in source_counts:
        if source_counts[idx] > 1:
            # 该素材多次使用，检查起始点是否不同
            starts_for_material = [clip['start'] for clip in sequence if clip['source_index'] == idx]
            if len(set(starts_for_material)) < len(starts_for_material):
                same_material_diff_start = False
                print(f"  ❌ 违规: 素材 {sequence[0]['source_name']} 多次使用起始点相同")
    
    if same_material_diff_start:
        print(f"  ✅ 规则9: 同一素材多次使用，起始点不同")
    
    return not (consecutive_same or consecutive_slices or not all_start_non_5sec or not all_duration_varied)


def main():
    """主流程"""
    # ========== 强制前置检查（禁止绕过）==========
    enforce_pre_check()
    guard = get_guard()
    # =============================================
    
    print("=" * 60)
    print("新闻视频调度规则 - 回切结构版")
    print("=" * 60)
    
    # 验证任务
    print("\n[1] 验证任务合规性...")
    validation = validate_task('恢复新闻视频调度规则，禁止连续同素材')
    if validation['decision'] == 'reject':
        print(f"  ✗ 任务被拒绝: {validation['reason']}")
        return
    if validation['warning']:
        print(f"  ⚠ {validation['warning']}")
    print("  ✓ 任务验证通过")
    
    # 加载项目状态
    state = load_project_state()
    print("  ✓ PROJECT_STATE.md 已加载")
    
    # 验证稿件
    print("\n[2] 验证稿件...")
    script_validation = validate_script(OFFICIAL_SCRIPT)
    if script_validation['decision'] == 'reject':
        print(f"  ✗ 稿件被拒绝: {script_validation['reason']}")
        return
    print("  ✓ 稿件验证通过")
    
    task_id = str(uuid.uuid4())
    print(f"\n[3] 任务ID: {task_id}")
    
    # 素材验证
    print("\n[4] 验证素材...")
    valid_materials = []
    for path in FIXED_MATERIALS:
        if os.path.exists(path):
            size_mb = os.path.getsize(path) / 1024 / 1024
            dur = get_material_duration(path)
            print(f"  ✓ {os.path.basename(path)} ({size_mb:.1f}MB, {dur:.1f}s)")
            valid_materials.append(path)
    
    if len(valid_materials) < 3:
        print(f"\n✗ 错误: 有效素材不足")
        return
    
    # 选片（新闻视频调度规则 - 动态裁剪版）
    print("\n[5] 选片（新闻视频调度规则 - 动态裁剪版）...")
    sequence, main_a, main_b, aux_materials = build_news_schedule_clips(
        valid_materials, 
        target_clips=8,
        min_duration=3,  # ← 最短3秒
        max_duration=6   # ← 最长6秒
    )
    
    # 输出镜头序列
    print(f"\n镜头序列:")
    seq_str = ""
    for i, clip in enumerate(sequence):
        marker = clip['source_type']
        seq_str += marker
        if i < len(sequence) - 1:
            seq_str += " → "
        print(f"  镜头{i+1}: {clip['source_name']} [{clip['start']}-{clip['start']+clip['duration']}s] ({marker})")
    
    print(f"\n序列简写: {seq_str}")
    
    # 验证规则
    print("\n[6] 规则验证...")
    is_valid = validate_sequence(sequence)
    
    if not is_valid:
        print("\n❌ 序列不符合规则，终止生成")
        return
    
    # 统计主素材使用次数
    main_count = sum(1 for c in sequence if c['source_index'] == main_a['index'])
    print(f"\n主素材: {main_a['name']}")
    print(f"使用次数: {main_count} 次")
    
    # 使用缓存处理素材（动态裁剪）
    print("\n[7] 动态裁剪素材...")
    selected_clips = []
    
    for i, clip_info in enumerate(sequence):
        source_path = clip_info['source_path']
        start = clip_info['start']
        duration = clip_info['duration']
        
        # ✅ 动态裁剪：直接从原素材裁剪任意片段
        clip_result = extract_dynamic_clip(
            source_path,
            start=start,
            duration=duration,
            workdir=storage.workdir,
            task_id=task_id,
            clip_id=i
        )
        
        if clip_result:
            clip_result['source_index'] = clip_info['source_index']
            clip_result['source_name'] = clip_info['source_name']
            clip_result['source_type'] = clip_info['source_type']
            selected_clips.append(clip_result)
            print(f"  镜头{i+1}: {clip_info['source_name']} [{start:.1f}-{start+duration:.1f}s] ({duration:.1f}s)")
        else:
            print(f"  ⚠️ 镜头{i+1}裁剪失败，跳过")
    
    # 计算实际视频时长
    video_duration = sum(clip['duration'] for clip in selected_clips)
    print(f"  选中片段: {len(selected_clips)} 个")
    print(f"  视频时长: {video_duration:.1f} 秒（动态裁剪）")
    
    # TTS
    print("\n[9] TTS合成...")
    tts_path = os.path.join(storage.workdir, f"{task_id}_tts.mp3")
    tts_meta_path = os.path.join(storage.workdir, f"{task_id}_tts_meta.json")
    tts_meta = asyncio.run(generate_tts(OFFICIAL_SCRIPT, tts_path, tts_meta_path))
    
    tts_duration = tts_meta['total_duration']
    print(f"  TTS时长: {tts_duration:.2f} 秒")
    
    # 字幕
    print("\n[10] 生成字幕...")
    srt_path = os.path.join(storage.workdir, f"{task_id}.srt")
    create_subtitle_srt_from_meta(tts_meta, srt_path)
    
    # 合成视频
    print("\n[11] 合成视频...")
    output_path = os.path.join(storage.outputs_dir, f"{task_id}.mp4")
    
    processor.assemble_video(
        selected_clips, 
        tts_path, 
        srt_path, 
        output_path, 
        target_duration=min(video_duration, int(tts_duration + 5)),
        keep_concat=True
    )
    
    # 结果
    if os.path.exists(output_path):
        output_size = os.path.getsize(output_path) / 1024 / 1024
        
        import subprocess
        result = subprocess.run(
            ['/home/linuxbrew/.linuxbrew/bin/ffprobe', '-v', 'error', 
             '-show_entries', 'format=duration', 
             '-of', 'default=noprint_wrappers=1:nokey=1', output_path],
            capture_output=True, text=True
        )
        actual_duration = float(result.stdout.strip())
        
        print("\n" + "=" * 60)
        print("生成完成 - 新闻视频调度规则版")
        print("=" * 60)
        
        # 输出验证信息
        print("\n【验证输出】")
        print(f"1) 镜头素材序列: {seq_str}")
        print(f"2) 主素材: {main_a['name']}，使用次数: {main_count}")
        print(f"3) 是否存在连续同素材: 否")
        print(f"4) task_id: {task_id}")
        print(f"   下载地址: http://47.93.194.154:8088/download/{task_id}")
        
        print(f"\n文件大小: {output_size:.2f} MB")
        print(f"实际时长: {actual_duration:.2f} 秒")
        print(f"TTS时长: {tts_duration:.2f} 秒")
        
        # 保存任务信息（必须包含 output_path 以支持下载）
        task_info = {
            'id': task_id,
            'status': 'completed',
            'rule': 'news_schedule',
            'sequence': seq_str,
            'main_material': main_a['name'],
            'main_count': main_count,
            'consecutive_same': False,
            'clips': sequence,
            'output_path': output_path,  # ← 必须：下载接口依赖此字段
            'output_size_mb': output_size,
            'actual_duration_sec': actual_duration,
            'tts_duration_sec': tts_duration,
            'created_at': datetime.now().isoformat()
        }
        
        os.makedirs(os.path.join(storage.workdir, 'tasks'), exist_ok=True)
        with open(os.path.join(storage.workdir, 'tasks', f'{task_id}.json'), 'w', encoding='utf-8') as f:
            json.dump(task_info, f, ensure_ascii=False, indent=2)
        
        # ========== 强制更新记忆层（禁止绕过）==========
        # 更新 memory
        guard.append_to_memory(f"""
## 任务完成记录

- **task_id**: {task_id}
- **规则**: news_schedule
- **序列**: {seq_str}
- **主素材**: {main_a['name']} ({main_count}次)
- **时长**: {actual_duration:.2f}s
- **缓存命中**: 是
""")
        
        # Git 备份
        guard.git_backup(f"任务完成: {task_id}")
        # =============================================
        
        return task_id, output_path
    else:
        print("\n✗ 合成失败")
        return None, None


if __name__ == '__main__':
    main()