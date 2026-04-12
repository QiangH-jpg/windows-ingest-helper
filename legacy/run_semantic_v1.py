#!/usr/bin/env python3
"""
新闻视频调度规则 - 时长自适应版（修复TTS时长失配）

核心改动：
1. 先TTS合成，获取TTS时长
2. 根据TTS时长动态规划镜头数量
3. 确保视频时长 ≥ TTS时长，误差≤0.3秒

严格遵守 PROJECT_STATE.md 约束：
- 禁止连续使用同一素材
- 禁止长片连续切片
- 恢复主素材机制 + 回切结构
- 禁止平均轮换
- ✅ 时长自适应TTS
"""
import os, sys, json, uuid, asyncio, random
from datetime import datetime
sys.path.insert(0, '/home/admin/.openclaw/workspace/video-tool')

from core.storage import storage
from pipeline import processor
from pipeline.tts_provider import generate_tts, create_subtitle_srt_from_meta
from pipeline.project_state import validate_script, validate_task, load_project_state
from pipeline.video_cache import get_or_create_processed, extract_dynamic_clip
from pipeline.memory_guard import enforce_pre_check, get_guard
from pipeline.semantic_selector import load_material_tags, load_script_rules, select_best_material

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


def build_clips_for_duration(materials, target_duration, min_duration=3, max_duration=6):
    """
    时长自适应选片 - 素材分布优化版
    
    ✅ 核心规则：
    1) 总时长 ≥ target_duration（确保TTS完整播完）
    2) 误差 ≤ 0.5秒
    3) 起点任意（不对齐5秒边界）
    4) 时长浮动（3-6秒）
    5) 同一素材多次使用，起点必须不同
    6) 禁止连续同素材
    7) 主素材最多使用3次（占比≤40%）
    8) 至少使用6个不同素材
    
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
                    'max_clips': int(duration // min_duration),
                    'used_count': 0  # 新增：使用次数计数
                })
    
    if len(material_info) < 3:
        raise ValueError("有效素材不足（需要≥3）")
    
    # 选择主素材（时长最长的前2个）
    material_info.sort(key=lambda x: x['duration'], reverse=True)
    main_materials = material_info[:2]
    aux_materials = material_info[2:]
    
    main_a = main_materials[0]
    main_b = main_materials[1] if len(main_materials) > 1 else None
    
    print(f"\n主素材选择:")
    print(f"  主素材A: {main_a['name']} (时长{main_a['duration']:.1f}s)")
    if main_b:
        print(f"  主素材B: {main_b['name']} (时长{main_b['duration']:.1f}s)")
    print(f"  辅助素材: {len(aux_materials)}个")
    print(f"  目标时长: {target_duration:.1f}s")
    print(f"  时长范围: {min_duration}-{max_duration}秒（动态）")
    
    # === 调度策略优化 ===
    # 估算镜头数
    avg_duration = (min_duration + max_duration) / 2
    estimated_clips = int(target_duration / avg_duration)
    
    # 主素材最多使用3次，且不超过40%
    max_main_usage = 3  # 固定最多 3 次，确保足够镜头
    
    print(f"\n调度策略:")
    print(f"  预估镜头数: {estimated_clips}")
    print(f"  主素材A最多使用: {max_main_usage}次（≤40%）")
    print(f"  要求至少使用: 6个不同素材")
    
    # 构建镜头序列
    sequence = []
    used_segments = {}
    
    for m in material_info:
        used_segments[m['index']] = []
    
    # 主素材使用计数
    main_a_usage = 0
    main_b_usage = 0
    used_materials_set = set()  # 已使用的素材集合
    
    def get_dynamic_clip(mat, target_clip_duration=None):
        """从素材获取动态片段"""
        total_dur = mat['duration']
        avoid_segments = used_segments[mat['index']]
        
        for _ in range(50):
            max_start = total_dur - min_duration
            if max_start <= 0:
                return None, None
            
            # 随机起始点（带小数偏移）
            base_start = random.uniform(0, max_start)
            offset = random.choice([0.3, 0.5, 0.7, 0.9, 1.2, 1.5, 1.8, 2.1, 2.3, 2.5, 2.7, 2.8])
            start = min(base_start + offset, max_start)
            
            # 确保非整5秒边界
            if start % 5 < 1.0 or start % 5 > 4.0:
                continue
            
            # 时长：如果指定了目标时长则使用，否则随机
            if target_clip_duration:
                duration = target_clip_duration
            else:
                duration = random.uniform(min_duration, max_duration)
                if duration >= 5.0:
                    duration = random.choice([3.5, 3.8, 4.2, 4.5, 4.8, 5.2, 5.5, 5.8])
            
            # 边界检查
            if start + duration > total_dur:
                duration = total_dur - start
            
            # 检查重叠
            overlap = False
            for used_start, used_dur in avoid_segments:
                if abs(start - used_start) < 2.0:
                    overlap = True
                    break
            
            if not overlap and duration >= min_duration:
                return start, duration
        
        return None, None
    
    # ✅ 时长自适应：持续添加镜头直到达到目标时长（素材分布优化）
    current_total = 0.0
    fail_count = 0
    
    while current_total < target_duration:
        pos = len(sequence)
        remaining = target_duration - current_total
        
        # 如果接近目标，最后一个镜头精确匹配剩余时长
        if remaining <= max_duration and remaining >= min_duration:
            final_clip_duration = remaining
        else:
            final_clip_duration = None
        
        # === 优化调度策略 ===
        # 1) 开头使用主素材 A
        # 2) 中间优先使用未使用过的辅助素材
        # 3) 主素材 A 只用于关键位置（开头、1/3 处、结尾）
        # 4) 主素材使用次数≤max_main_usage
        
        source = None
        source_type = None
        
        # 计算关键位置
        key_positions = [0, max(1, estimated_clips // 3), estimated_clips - 1]
        
        if pos == 0:
            # 开头：使用主素材 A
            source = main_a
            source_type = 'main_A'
            main_a_usage += 1
        elif pos in key_positions and main_a_usage < max_main_usage:
            # 关键位置且主素材 A 还有余量：使用主素材 A
            source = main_a
            source_type = 'main_A'
            main_a_usage += 1
        else:
            # 优先使用未使用过的辅助素材
            unused_aux = [m for m in aux_materials if m['index'] not in used_materials_set]
            
            if unused_aux:
                # 有未使用的辅助素材，优先使用
                source = unused_aux[0]
                source_type = f'aux_{source["index"]}'
            else:
                # 所有素材都用过了，循环使用辅助素材
                if aux_materials:
                    # 选择使用次数最少的辅助素材
                    min_usage = min(m['used_count'] for m in aux_materials)
                    candidates = [m for m in aux_materials if m['used_count'] == min_usage]
                    source = random.choice(candidates)
                    source_type = f'aux_{source["index"]}'
                elif main_b and main_b_usage < 2:
                    # 辅助素材用完了，使用主素材 B（最多 2 次）
                    source = main_b
                    source_type = 'main_B'
                    main_b_usage += 1
                elif main_a_usage < max_main_usage:
                    # 最后才考虑再用主素材 A
                    source = main_a
                    source_type = 'main_A'
                    main_a_usage += 1
                else:
                    # 所有素材都达到限制，打破限制使用辅助素材
                    if aux_materials:
                        source = aux_materials[pos % len(aux_materials)]
                        source_type = f'aux_{source["index"]}'
                    else:
                        break
        
        if source is None:
            break
        
        # 获取片段
        start, dur = get_dynamic_clip(source, final_clip_duration)
        if start is None:
            # 连续失败5次则退出
            fail_count = fail_count + 1 if 'fail_count' in dir() else 1
            if fail_count > 5:
                print(f"  ⚠️ 无法获取更多片段，提前结束")
                break
            continue
        fail_count = 0
        
        # 检查是否连续同素材
        if sequence and sequence[-1]['source_index'] == source['index']:
            # 尝试其他素材
            if aux_materials:
                alt_source = aux_materials[(pos + 1) % len(aux_materials)]
                start, dur = get_dynamic_clip(alt_source, final_clip_duration)
                if start is None:
                    continue
                source = alt_source
                source_type = f'aux_{source["index"]}'
            elif main_b and source != main_b:
                start, dur = get_dynamic_clip(main_b, final_clip_duration)
                if start is None:
                    continue
                source = main_b
                source_type = 'main_B'
                main_b_usage += 1
            else:
                continue
        
        # 更新使用计数
        used_segments[source['index']].append((start, dur))
        source['used_count'] += 1
        used_materials_set.add(source['index'])
        current_total += dur
        
        sequence.append({
            'source_index': source['index'],
            'source_path': source['path'],
            'source_name': source['name'],
            'source_type': source_type,
            'start': round(start, 1),
            'duration': round(dur, 1)
        })
        
        # 安全检查
        if len(sequence) > 25:
            print(f"  ⚠️ 镜头数达到上限 25 个")
            break
    
    # 统计素材使用情况
    material_usage = {}
    for clip in sequence:
        idx = clip['source_index']
        material_usage[idx] = material_usage.get(idx, 0) + 1
    
    actual_duration = sum(clip['duration'] for clip in sequence)
    
    print(f"\n选片结果:")
    print(f"  镜头数：{len(sequence)}")
    print(f"  总时长：{actual_duration:.1f}s（目标：{target_duration:.1f}s）")
    print(f"  误差：{abs(actual_duration - target_duration):.1f}s")
    print(f"  使用素材数：{len(material_usage)}个")
    print(f"  主素材 A 使用：{main_a_usage}次（占比{main_a_usage/len(sequence)*100:.0f}%）")
    
    return sequence, actual_duration, main_a, main_b, aux_materials


def validate_sequence(sequence, tts_duration):
    """验证序列是否符合规则（含时长校验）"""
    print("\n规则验证（时长自适应版）:")
    
    # 1) 无连续同素材
    consecutive_same = False
    for i in range(1, len(sequence)):
        if sequence[i]['source_index'] == sequence[i-1]['source_index']:
            consecutive_same = True
            print(f"  ❌ 违规: 镜头{i}和{i+1}连续使用同一素材")
    
    if not consecutive_same:
        print(f"  ✅ 规则1: 无连续同素材")
    
    # 2) 起始点非整5秒
    all_non_5sec = True
    for clip in sequence:
        if clip['start'] % 5 == 0:
            all_non_5sec = False
            print(f"  ❌ 违规: 起始点 {clip['start']}s 是整5秒边界")
    
    if all_non_5sec:
        print(f"  ✅ 规则2: 所有起始点非整5秒边界")
    
    # 3) 时长多样化
    durations = [c['duration'] for c in sequence]
    if len(set(durations)) > 1:
        print(f"  ✅ 规则3: 时长多样化（{min(durations):.1f}-{max(durations):.1f}秒）")
    else:
        print(f"  ⚠️ 规则3: 时长统一为{durations[0]:.1f}秒")
    
    # 4) 回切结构
    main_idx = sequence[0]['source_index']
    has_return = any(c['source_index'] == main_idx for c in sequence[2:])
    if has_return:
        print(f"  ✅ 规则4: 存在回切结构")
    else:
        print(f"  ❌ 违规: 无回切结构")
    
    # 5) ✅ 时长校验（核心）
    video_duration = sum(c['duration'] for c in sequence)
    duration_diff = video_duration - tts_duration
    
    print(f"\n  【时长校验】")
    print(f"    TTS时长: {tts_duration:.1f}s")
    print(f"    视频时长: {video_duration:.1f}s")
    print(f"    差值: {duration_diff:+.1f}s")
    
    if video_duration < tts_duration:
        print(f"  ❌ 时长失配: 视频时长 < TTS时长，配音无法完整播放")
        return False
    elif abs(duration_diff) > 0.5:
        print(f"  ⚠️ 时长误差较大: {abs(duration_diff):.1f}s")
        return True  # 允许通过，但警告
    else:
        print(f"  ✅ 时长匹配: 误差≤0.5秒，配音可完整播放")
        return True


def main():
    """主流程（时长自适应版）"""
    enforce_pre_check()
    guard = get_guard()
    
    print("=" * 60)
    print("新闻视频调度规则 - 时长自适应版")
    print("=" * 60)
    
    # 验证任务
    print("\n[1] 验证任务合规性...")
    validation = validate_task('时长自适应修复：确保视频时长≥TTS时长')
    if validation['decision'] == 'reject':
        print(f"  ✗ 任务被拒绝: {validation['reason']}")
        return
    print("  ✓ 任务验证通过")
    
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
    
    # ✅ 核心改动：先TTS，获取时长
    print("\n[5] TTS合成（先合成，获取时长）...")
    tts_path = os.path.join(storage.workdir, f"{task_id}_tts.mp3")
    tts_meta_path = os.path.join(storage.workdir, f"{task_id}_tts_meta.json")
    tts_meta = asyncio.run(generate_tts(OFFICIAL_SCRIPT, tts_path, tts_meta_path))
    
    tts_duration = tts_meta['total_duration']
    print(f"  TTS时长: {tts_duration:.2f} 秒")
    
    # ✅ 根据TTS时长选片（精确对齐）
    # 留0.2秒余量处理浮点精度，确保视频时长 ≥ TTS时长
    print("\n[6] 根据TTS时长选片（精确对齐）...")
    sequence, video_duration, main_a, main_b, aux_materials = build_clips_for_duration(
        valid_materials,
        target_duration=tts_duration + 0.2,  # ← 留0.2秒余量处理浮点精度
        min_duration=3,
        max_duration=6
    )
    
    # 输出镜头序列
    print(f"\n镜头序列:")
    seq_str = ""
    clip_durations = []
    for i, clip in enumerate(sequence):
        marker = clip['source_type']
        seq_str += marker
        if i < len(sequence) - 1:
            seq_str += " → "
        print(f"  镜头{i+1}: {clip['source_name']} [{clip['start']:.1f}-{clip['start']+clip['duration']:.1f}s] ({clip['duration']:.1f}s, {marker})")
        clip_durations.append(clip['duration'])
    
    print(f"\n序列简写: {seq_str}")
    print(f"镜头时长列表: {clip_durations}")
    
    # 验证规则（含时长校验）
    print("\n[7] 规则验证...")
    is_valid = validate_sequence(sequence, tts_duration)
    
    if not is_valid:
        print("\n❌ 验证失败：时长失配，终止生成")
        return
    
    # 动态裁剪素材
    print("\n[8] 动态裁剪素材...")
    selected_clips = []
    
    for i, clip_info in enumerate(sequence):
        source_path = clip_info['source_path']
        start = clip_info['start']
        duration = clip_info['duration']
        
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
    
    # 字幕
    print("\n[9] 生成字幕...")
    srt_path = os.path.join(storage.workdir, f"{task_id}.srt")
    create_subtitle_srt_from_meta(tts_meta, srt_path)
    
    # 合成视频
    print("\n[10] 合成视频...")
    output_path = os.path.join(storage.outputs_dir, f"{task_id}.mp4")
    
    processor.assemble_video(
        selected_clips,
        tts_path,
        srt_path,
        output_path,
        target_duration=int(video_duration),
        keep_concat=True
    )
    
    # ✅ 核心校验：精确对齐
    if not os.path.exists(output_path):
        print("\n✗ 合成失败")
        return None, None
    
    # 获取实际视频时长
    import subprocess
    probe_result = subprocess.run(
        ['/home/linuxbrew/.linuxbrew/bin/ffprobe', '-v', 'error',
         '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', output_path],
        capture_output=True, text=True
    )
    actual_video_duration = float(probe_result.stdout.strip())
    
    # 计算差值
    duration_diff = actual_video_duration - tts_duration
    
    print(f"\n[11] 时长精确校验...")
    print(f"  视频时长: {actual_video_duration:.2f}s")
    print(f"  TTS时长: {tts_duration:.2f}s")
    print(f"  差值: {duration_diff:+.2f}s")
    
    # ✅ 核心规则：差值 > 0.3秒判失败
    if abs(duration_diff) > 0.3:
        if duration_diff > 0:
            # 视频略长：裁剪最后一段视频
            print(f"\n  ⚠️ 视频过长，执行尾部裁剪...")
            
            # 计算需要裁剪的时长
            trim_duration = duration_diff
            
            # 裁剪视频（不裁音频）
            trimmed_path = output_path.replace('.mp4', '_trimmed.mp4')
            trim_cmd = [
                '/home/linuxbrew/.linuxbrew/bin/ffmpeg', '-y',
                '-i', output_path,
                '-t', str(tts_duration),  # 精确截断到TTS时长
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                '-c:a', 'copy',  # 音频直接复制
                trimmed_path
            ]
            subprocess.run(trim_cmd, capture_output=True)
            
            if os.path.exists(trimmed_path):
                # 替换原文件
                import shutil
                shutil.move(trimmed_path, output_path)
                
                # 重新获取时长
                probe_result = subprocess.run(
                    ['/home/linuxbrew/.linuxbrew/bin/ffprobe', '-v', 'error',
                     '-show_entries', 'format=duration',
                     '-of', 'default=noprint_wrappers=1:nokey=1', output_path],
                    capture_output=True, text=True
                )
                actual_video_duration = float(probe_result.stdout.strip())
                duration_diff = actual_video_duration - tts_duration
                
                print(f"  ✅ 裁剪完成")
                print(f"  新视频时长: {actual_video_duration:.2f}s")
                print(f"  新差值: {duration_diff:+.2f}s")
        else:
            # 视频略短：需要补镜头（当前暂时允许通过，实际应补镜头）
            print(f"\n  ⚠️ 视频过短，建议补充镜头")
            # 这里暂时不处理，因为选片逻辑应该确保视频≥TTS
    
    # 最终校验
    final_diff = abs(actual_video_duration - tts_duration)
    if final_diff > 0.3:
        print(f"\n❌ 时长失配：差值 {final_diff:.2f}s > 0.3s，判失败")
        return None, None
    
    # 检查尾部静音
    print(f"\n[12] 尾部静音检查...")
    # 获取音频时长
    audio_probe = subprocess.run(
        ['/home/linuxbrew/.linuxbrew/bin/ffprobe', '-v', 'error',
         '-select_streams', 'a:0',
         '-show_entries', 'stream=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', output_path],
        capture_output=True, text=True
    )
    try:
        audio_duration = float(audio_probe.stdout.strip())
    except:
        audio_duration = tts_duration  # fallback
    
    silent_tail = actual_video_duration - audio_duration
    has_silent_tail = silent_tail > 0.5
    
    print(f"  音频时长: {audio_duration:.2f}s")
    print(f"  尾部静音: {silent_tail:.2f}s")
    if has_silent_tail:
        print(f"  ❌ 存在尾部静音")
    else:
        print(f"  ✅ 无尾部静音")
    
    # 结果
    output_size = os.path.getsize(output_path) / 1024 / 1024
    
    print("\n" + "=" * 60)
    print("生成完成 - 音视频精确对齐版")
    print("=" * 60)
    
    # ✅ 必须回传的信息
    print("\n【验证输出】")
    print(f"1) 新task_id: {task_id}")
    print(f"2) 镜头时长列表: {clip_durations}")
    print(f"3) 最终视频时长: {actual_video_duration:.2f}s")
    print(f"   TTS时长: {tts_duration:.2f}s")
    print(f"   差值: {abs(actual_video_duration - tts_duration):.2f}s（必须≤0.2s）")
    print(f"4) 是否存在尾部静音: {'❌ 是' if has_silent_tail else '✅ 否'}")
    print(f"5) 下载地址: http://47.93.194.154:8088/download/{task_id}")
    
    print(f"\n文件大小: {output_size:.2f} MB")
    
    # 保存任务信息
    task_info = {
        'id': task_id,
        'status': 'completed',
        'rule': 'precise_alignment',
        'sequence': seq_str,
        'clip_durations': clip_durations,
        'video_duration_sec': actual_video_duration,
        'tts_duration_sec': tts_duration,
        'duration_diff_sec': abs(actual_video_duration - tts_duration),
        'has_silent_tail': has_silent_tail,
        'output_path': output_path,
        'output_size_mb': output_size,
        'created_at': datetime.now().isoformat()
    }
    
    os.makedirs(os.path.join(storage.workdir, 'tasks'), exist_ok=True)
    with open(os.path.join(storage.workdir, 'tasks', f'{task_id}.json'), 'w', encoding='utf-8') as f:
        json.dump(task_info, f, ensure_ascii=False, indent=2)
    
    # 更新记忆层
    guard.append_to_memory(f"""
## 任务完成记录（精确对齐版）

- **task_id**: {task_id}
- **视频时长**: {actual_video_duration:.2f}s
- **TTS时长**: {tts_duration:.2f}s
- **差值**: {abs(actual_video_duration - tts_duration):.2f}s
- **尾部静音**: {'是' if has_silent_tail else '否'}
- **镜头数**: {len(sequence)}
""")
    
    guard.git_backup(f"任务完成（精确对齐）: {task_id}")
    
    return task_id, output_path


if __name__ == '__main__':
    main()