#!/usr/bin/env python3
"""
正式样片生成脚本 - 片段节奏重构版

核心规则：
1. 语音只作参考，不绑定镜头
2. 镜头长度区间：3~7秒
3. 长句子拆成多个镜头
4. 节奏不均匀（避免机械）
"""
import os, sys, json, uuid, asyncio, subprocess, random
from datetime import datetime
import time

sys.path.insert(0, '/home/admin/.openclaw/workspace/video-tool')

from core.storage import storage
from pipeline.tts_provider import generate_tts, create_subtitle_srt_from_meta
from pipeline.audio_driven_timeline import assemble_video_audio_driven, get_duration
from pipeline.video_cache import get_or_create_processed

# 固定素材
FIXED_MATERIALS = [
    '/home/admin/.openclaw/workspace/video-tool/uploads/394A0108.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/394A0109.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115142627_0112_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115144146_0143_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115144510_0148_D.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/DJI_20001115140336_0110_D.MP4',
]

# 固定口播稿
OFFICIAL_SCRIPT = """3月26日，济南市人社局开展"人社服务大篷车"活动。

活动以"走进奔跑者——保障与你同行"为主题，把人社服务送到外卖骑手等一线劳动者。

现场通过发放资料、面对面讲解，向小哥介绍社保参保、权益保障等政策。

还有互动环节，让大家在轻松氛围中了解政策。

济南市人社局持续推动服务走近新就业形态劳动者，打通保障"最后一公里"。"""

FFPROBE_PATH = '/home/linuxbrew/.linuxbrew/bin/ffprobe'
FFMPEG_PATH = '/home/linuxbrew/.linuxbrew/bin/ffmpeg'

# 镜头长度区间
MIN_CLIP_DURATION = 3.0
MAX_CLIP_DURATION = 7.0
TARGET_CLIP_RANGE = (4.0, 6.0)  # 目标区间


def split_sentence_into_clips(sentence_duration, target_range=(4.0, 6.0)):
    """
    将句子拆分成多个镜头
    
    规则：
    - 时长 < 3秒：不拆，保持原样（避免碎片）
    - 时长 3~6秒：一个镜头
    - 时长 > 6秒：必须拆分
    
    Returns:
        List[float]: 每个镜头的时长列表
    """
    if sentence_duration < MIN_CLIP_DURATION:
        # 太短，不拆分（避免碎片）
        return [sentence_duration]
    
    if sentence_duration <= MAX_CLIP_DURATION:
        # 在区间内，一个镜头
        return [sentence_duration]
    
    # 需要拆分
    clips = []
    remaining = sentence_duration
    
    # 拆分策略：随机时长，但都在3~6秒区间
    while remaining > MAX_CLIP_DURATION:
        # 随机选择一个目标时长
        clip_dur = random.uniform(TARGET_CLIP_RANGE[0], TARGET_CLIP_RANGE[1])
        clip_dur = min(clip_dur, remaining - MIN_CLIP_DURATION)  # 确保剩余部分不会太短
        clips.append(clip_dur)
        remaining -= clip_dur
    
    # 剩余部分
    if remaining >= MIN_CLIP_DURATION:
        clips.append(remaining)
    else:
        # 剩余太短，合并到最后一个镜头
        if clips:
            clips[-1] += remaining
        else:
            clips.append(remaining)
    
    return clips


def distribute_clips_by_sentences(sentences, total_duration_target):
    """
    根据句子分配镜头
    
    Args:
        sentences: 句子列表（含时长）
        total_duration_target: 目标总时长
    
    Returns:
        List[Dict]: 镜头列表，每个包含时长和对应的句子
    """
    all_clips = []
    
    for sent in sentences:
        sent_duration = sent['duration']
        sent_idx = sent['index']
        sent_text = sent['text']
        
        # 拆分成镜头
        clip_durations = split_sentence_into_clips(sent_duration)
        
        for clip_dur in clip_durations:
            all_clips.append({
                'duration': clip_dur,
                'sentence_index': sent_idx,
                'sentence_text': sent_text,
                'is_split': len(clip_durations) > 1  # 标记是否为拆分镜头
            })
    
    return all_clips


def create_variable_length_clip(source_path, start_time, duration, output_path):
    """创建可变时长片段"""
    cmd = [
        FFMPEG_PATH, '-y',
        '-i', source_path,
        '-ss', str(start_time),
        '-t', str(duration),
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
        '-c:a', 'aac', '-b:a', '128k',
        output_path
    ]
    
    result = subprocess.run(cmd, capture_output=True)
    
    if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
        return output_path
    return None


def main():
    start_time = time.time()
    task_id = str(uuid.uuid4())
    random.seed(int(time.time()))  # 随机种子，确保每次节奏不同
    print(f"[task_id] {task_id}")
    
    # 1. 验证素材
    print("\n[1/7] 验证素材...")
    materials = []
    for path in FIXED_MATERIALS:
        if os.path.exists(path):
            materials.append(path)
            print(f"  ✓ {os.path.basename(path)}")
    print(f"  有效素材: {len(materials)} 个")
    
    # 2. TTS先行
    print("\n[2/7] TTS合成...")
    tts_start = time.time()
    tts_path = os.path.join(storage.workdir, f"{task_id}_tts.mp3")
    tts_meta_path = os.path.join(storage.workdir, f"{task_id}_tts_meta.json")
    tts_meta = asyncio.run(generate_tts(OFFICIAL_SCRIPT, tts_path, tts_meta_path))
    tts_duration = tts_meta['total_duration']
    print(f"  TTS 总时长: {tts_duration:.2f}s")
    print(f"  TTS 耗时: {time.time() - tts_start:.1f}s")
    
    # 打印句子时长
    print(f"\n  句子时长分配:")
    for sent in tts_meta.get('sentences', []):
        print(f"    {sent['index']+1}. [{sent['duration']:.2f}s] {sent['text'][:25]}...")
    
    # 3. 获取标准化素材
    print("\n[3/7] 获取标准化素材（缓存）...")
    transcode_start = time.time()
    processed_materials = []
    for path in materials:
        processed_path = get_or_create_processed(path)
        processed_materials.append(processed_path)
    print(f"  素材预处理耗时: {time.time() - transcode_start:.1f}s")
    
    # 4. 按节奏规则分配镜头（核心修改）
    print("\n[4/7] 按节奏规则分配镜头...")
    
    sentences = tts_meta.get('sentences', [])
    
    # 按句子拆分镜头
    clip_plan = distribute_clips_by_sentences(sentences, tts_duration)
    
    print(f"\n  镜头分配计划:")
    print(f"    句子数: {len(sentences)}")
    print(f"    镜头数: {len(clip_plan)}")
    
    for i, clip in enumerate(clip_plan):
        split_mark = " [拆分]" if clip.get('is_split') else ""
        print(f"    镜头{i+1}: {clip['duration']:.2f}s ← 句子{clip['sentence_index']+1}{split_mark}")
    
    # 统计
    durations = [c['duration'] for c in clip_plan]
    print(f"\n  镜头时长统计:")
    print(f"    最短: {min(durations):.2f}s")
    print(f"    最长: {max(durations):.2f}s")
    print(f"    平均: {sum(durations)/len(durations):.2f}s")
    print(f"    预计总时长: {sum(durations):.2f}s")
    
    # 5. 切镜头
    print("\n[5/7] 切镜头...")
    
    clip_dir = os.path.join(storage.workdir, task_id, 'clips')
    os.makedirs(clip_dir, exist_ok=True)
    
    # 获取素材可用时长
    material_durations = []
    for path in processed_materials:
        probe_cmd = [
            FFPROBE_PATH, '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            path
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True)
        dur = float(result.stdout.strip())
        material_durations.append(dur)
    
    # 分配镜头到素材（新闻视频风格调度规则）
    # 核心：主素材 + 辅助素材，非平均轮换
    
    clips = []
    n_materials = len(processed_materials)
    n_clips = len(clip_plan)
    
    # Step 1: 选择主素材（选择时长最长的1-2个素材）
    # 按时长排序
    sorted_by_duration = sorted(
        range(n_materials),
        key=lambda i: material_durations[i],
        reverse=True
    )
    
    # 主素材：前1-2个最长素材
    main_materials = sorted_by_duration[:min(2, n_materials)]
    # 辅助素材：其余素材
    auxiliary_materials = sorted_by_duration[min(2, n_materials):]
    
    print(f"\n  素材角色划分:")
    print(f"    主素材: {main_materials} (时长最长)")
    print(f"    辅助素材: {auxiliary_materials}")
    
    # Step 2: 构建调度计划（新闻风格）
    # 规则：
    # - 开头（前2-3个镜头）：用不同素材建立信息
    # - 中段：回到主素材
    # - 结尾：主素材收尾
    # - 禁止连续相同素材
    
    # 初始化素材使用记录
    material_usage = {
        i: {
            'count': 0,
            'last_clip_idx': -1,
            'used_ranges': []
        }
        for i in range(n_materials)
    }
    
    # 调度逻辑
    # 前3个镜头：用不同素材（优先主素材，然后辅助素材）
    # 后续镜头：优先主素材，辅助素材穿插
    
    main_idx = 0  # 当前主素材索引
    aux_idx = 0   # 当前辅助素材索引
    
    for clip_idx, clip_plan_item in enumerate(clip_plan):
        target_duration = clip_plan_item['duration']
        
        best_material_idx = -1
        best_start_offset = 0
        
        # 调度规则
        if clip_idx < 3:
            # 开头（前3个镜头）：用不同素材建立信息
            if clip_idx == 0:
                # 第1个镜头：用第一个主素材
                best_material_idx = main_materials[0]
            elif clip_idx == 1:
                # 第2个镜头：用另一个主素材或第一个辅助素材
                if len(main_materials) > 1:
                    best_material_idx = main_materials[1]
                elif auxiliary_materials:
                    best_material_idx = auxiliary_materials[0]
                else:
                    best_material_idx = main_materials[0]
            else:
                # 第3个镜头：用第一个辅助素材或主素材
                if auxiliary_materials:
                    best_material_idx = auxiliary_materials[0]
                else:
                    best_material_idx = main_materials[0]
        else:
            # 中段和结尾：回到主素材，穿插辅助素材
            # 策略：每2-3个镜头回到主素材
            
            # 检查上一个镜头是否是主素材
            last_was_main = any(
                clips[-1]['source_index'] == m for m in main_materials
            ) if clips else False
            
            if last_was_main:
                # 上一个已是主素材，这次用辅助素材
                if auxiliary_materials:
                    # 找一个未使用的辅助素材
                    for aux_mat in auxiliary_materials:
                        if (material_usage[aux_mat]['count'] == 0 and 
                            material_durations[aux_mat] >= target_duration):
                            best_material_idx = aux_mat
                            break
                    
                    # 如果辅助素材都用过或不够，用主素材的另一个
                    if best_material_idx == -1:
                        for main_mat in main_materials:
                            if (main_mat != clips[-1]['source_index'] and
                                material_usage[main_mat]['count'] < 3):
                                best_material_idx = main_mat
                                break
                else:
                    # 没有辅助素材，用主素材的另一个
                    for main_mat in main_materials:
                        if (main_mat != clips[-1]['source_index'] and
                            material_usage[main_mat]['count'] < 3):
                            best_material_idx = main_mat
                            break
            else:
                # 上一个不是主素材，这次回到主素材
                for main_mat in main_materials:
                    if material_usage[main_mat]['count'] < 3:
                        best_material_idx = main_mat
                        break
        
        # 如果还没找到（兜底）
        if best_material_idx == -1:
            # 找使用次数最少的素材
            min_count = min(material_usage[i]['count'] for i in range(n_materials))
            for i in range(n_materials):
                if (material_usage[i]['count'] == min_count and
                    material_usage[i]['last_clip_idx'] != clip_idx - 1):
                    best_material_idx = i
                    break
        
        # 计算起始偏移（避开已用区间）
        used_ranges = material_usage[best_material_idx]['used_ranges']
        mat_duration = material_durations[best_material_idx]
        
        if not used_ranges:
            best_start_offset = 0
        else:
            # 找已用区间之后的可用空间
            last_end = max(r[1] for r in used_ranges)
            if mat_duration - last_end >= target_duration:
                best_start_offset = last_end
            else:
                # 尝试区间之间的空隙
                sorted_ranges = sorted(used_ranges, key=lambda x: x[0])
                prev_end = 0
                for start, end in sorted_ranges:
                    if start - prev_end >= target_duration:
                        best_start_offset = prev_end
                        break
                    prev_end = end
                else:
                    # 没有合适空隙，从头开始
                    best_start_offset = 0
        
        # 检查连续素材规则
        if clips and clips[-1]['source_index'] == best_material_idx:
            # 连续相同素材，换一个
            for i in range(n_materials):
                if (i != best_material_idx and
                    material_usage[i]['count'] < 3 and
                    material_durations[i] >= target_duration):
                    best_material_idx = i
                    # 重新计算偏移
                    used_ranges = material_usage[best_material_idx]['used_ranges']
                    mat_duration = material_durations[best_material_idx]
                    if not used_ranges:
                        best_start_offset = 0
                    else:
                        last_end = max(r[1] for r in used_ranges)
                        best_start_offset = last_end if mat_duration - last_end >= target_duration else 0
                    break
        
        # 切镜头
        clip_path = os.path.join(clip_dir, f"clip_{clip_idx}.mp4")
        result = create_variable_length_clip(
            processed_materials[best_material_idx],
            best_start_offset,
            target_duration,
            clip_path
        )
        
        if result:
            end_offset = best_start_offset + target_duration
            
            clips.append({
                'path': clip_path,
                'duration': target_duration,
                'sentence_index': clip_plan_item['sentence_index'],
                'source_index': best_material_idx,
                'source_offset': best_start_offset
            })
            
            # 更新素材使用记录
            material_usage[best_material_idx]['count'] += 1
            material_usage[best_material_idx]['last_clip_idx'] = clip_idx
            material_usage[best_material_idx]['used_ranges'].append((best_start_offset, end_offset))
            
            role = "主" if best_material_idx in main_materials else "辅"
            print(f"    镜头{clip_idx+1}: {target_duration:.2f}s ← 素材{best_material_idx}[{best_start_offset:.1f}s-{end_offset:.1f}s] ({role})")
        else:
            print(f"    ⚠️ 镜头{clip_idx+1}: 切片失败")
    
    print(f"\n  切镜头完成: {len(clips)} 个")
    
    # 素材调度分析（新闻风格验证）
    print(f"\n  素材调度分析:")
    
    # 镜头序列
    sequence = [c['source_index'] for c in clips]
    print(f"    镜头序列: {' → '.join([f'{s}' for s in sequence])}")
    
    # 主素材使用次数
    print(f"\n    主素材使用次数:")
    for main_mat in main_materials:
        count = material_usage[main_mat]['count']
        print(f"      素材{main_mat}: {count}次")
    
    # 辅助素材使用次数
    print(f"\n    辅助素材使用次数:")
    for aux_mat in auxiliary_materials:
        count = material_usage[aux_mat]['count']
        if count > 0:
            print(f"      素材{aux_mat}: {count}次")
    
    # 验证调度规则
    print(f"\n  调度规则验证:")
    
    # 检查是否有回切主素材
    main_appearances = [i for i, s in enumerate(sequence) if s in main_materials]
    if len(main_appearances) >= 2:
        print(f"    ✅ 主素材多次出现（有回切）")
    else:
        print(f"    ⚠️ 主素材只出现{len(main_appearances)}次")
    
    # 检查是否平均轮换
    unique_count = len(set(sequence))
    if unique_count < len(sequence) * 0.5:
        print(f"    ⚠️ 可能存在平均轮换")
    else:
        print(f"    ✅ 非平均轮换")
    
    # 检查开头和结尾是否是主素材
    if sequence[0] in main_materials:
        print(f"    ✅ 开头用主素材（素材{sequence[0]}）")
    else:
        print(f"    ⚠️ 开头未用主素材")
    
    if sequence[-1] in main_materials:
        print(f"    ✅ 结尾用主素材（素材{sequence[-1]}）")
    else:
        print(f"    ⚠️ 结尾未用主素材")
    
    # 详细素材使用统计
    print(f"\n  素材详细使用:")
    for mat_idx, usage in material_usage.items():
        if usage['count'] > 0:
            role = "主" if mat_idx in main_materials else "辅"
            print(f"    素材{mat_idx} ({role}): 使用{usage['count']}次")
            for i, (start, end) in enumerate(usage['used_ranges'], 1):
                print(f"      第{i}次: [{start:.1f}s - {end:.1f}s]")
    
    # 检查连续素材
    has_consecutive = False
    for i in range(1, len(clips)):
        if clips[i]['source_index'] == clips[i-1]['source_index']:
            has_consecutive = True
            print(f"    ⚠️ 镜头{i}和{i+1}连续使用素材{clips[i]['source_index']}")
    
    if not has_consecutive:
        print(f"\n  ✅ 无连续相同素材")
    
    # 6. 字幕
    print("\n[6/7] 生成字幕...")
    srt_path = os.path.join(storage.workdir, f"{task_id}.srt")
    create_subtitle_srt_from_meta(tts_meta, srt_path)
    
    # 7. 合成
    print("\n[7/7] 音频驱动视频合成...")
    compose_start = time.time()
    output_path = os.path.join(storage.outputs_dir, f"{task_id}.mp4")
    
    try:
        assemble_video_audio_driven(
            clips=clips,
            audio_path=tts_path,
            subtitle_path=srt_path,
            output_path=output_path,
            fps=25,
            resolution=(1280, 720),
            keep_concat=True,
            trim_audio_if_needed=False
        )
    except RuntimeError as e:
        print(f"\n❌ 合成失败: {e}")
        return None
    
    compose_elapsed = time.time() - compose_start
    print(f"  合成耗时: {compose_elapsed:.1f}s")
    
    # 结果
    total_elapsed = time.time() - start_time
    
    if os.path.exists(output_path):
        size_mb = os.path.getsize(output_path) / 1024 / 1024
        actual_duration = get_duration(output_path)
        
        print(f"\n{'='*60}")
        print(f"task_id: {task_id}")
        print(f"输出: {output_path}")
        print(f"大小: {size_mb:.2f} MB")
        print(f"TTS 时长: {tts_duration:.2f}s")
        print(f"视频时长: {actual_duration:.2f}s")
        print(f"误差: {actual_duration - tts_duration:.3f}s")
        print(f"下载: http://47.93.194.154:8088/api/download/{task_id}")
        print(f"{'='*60}")
        
        # 验证镜头节奏
        print(f"\n[镜头节奏验证]")
        clip_durations = [c['duration'] for c in clips]
        print(f"  镜头数量: {len(clips)}")
        print(f"  镜头时长列表: {' / '.join([f'{d:.1f}s' for d in clip_durations])}")
        print(f"  最短镜头: {min(clip_durations):.2f}s")
        print(f"  最长镜头: {max(clip_durations):.2f}s")
        print(f"  平均镜头: {sum(clip_durations)/len(clip_durations):.2f}s")
        
        # 检查是否满足要求
        if max(clip_durations) <= MAX_CLIP_DURATION:
            print(f"  ✅ 最长镜头 ≤ {MAX_CLIP_DURATION}s")
        else:
            print(f"  ❌ 最长镜头 > {MAX_CLIP_DURATION}s")
        
        if min(clip_durations) >= MIN_CLIP_DURATION:
            print(f"  ✅ 最短镜头 ≥ {MIN_CLIP_DURATION}s")
        else:
            print(f"  ⚠️ 最短镜头 < {MIN_CLIP_DURATION}s")
        
        # 检查节奏是否均匀
        avg = sum(clip_durations) / len(clip_durations)
        variance = sum((d - avg) ** 2 for d in clip_durations) / len(clip_durations)
        std_dev = variance ** 0.5
        
        print(f"  时长标准差: {std_dev:.2f}s (越大越不均匀，节奏越自然)")
        
        if std_dev > 0.5:
            print(f"  ✅ 节奏不均匀（避免机械）")
        else:
            print(f"  ⚠️ 节奏过于均匀")
        
        # 性能统计
        print(f"\n[性能统计]")
        print(f"  TTS 耗时: {time.time() - tts_start:.1f}s")
        print(f"  素材预处理耗时: {time.time() - transcode_start:.1f}s")
        print(f"  合成耗时: {compose_elapsed:.1f}s")
        print(f"  总耗时: {total_elapsed:.1f}s ({total_elapsed/60:.1f}分钟)")
        
        # 校验
        print("\n[校验]")
        if abs(actual_duration - tts_duration) <= 0.3:
            print("  ✅ 视频时长 = TTS 时长 (误差 ≤ 0.3s)")
        else:
            print(f"  ⚠️ 时长误差: {abs(actual_duration - tts_duration):.3f}s")
        
        # 保存任务信息（必须！否则web app无法下载）
        task_info = {
            'id': task_id,
            'status': 'completed',
            'script': OFFICIAL_SCRIPT,
            'script_source': 'fixed_official',
            'materials': [os.path.basename(m) for m in materials],
            'output_path': output_path,
            'output_size_mb': size_mb,
            'tts_duration_sec': tts_duration,
            'video_duration_sec': actual_duration,
            'duration_error_sec': actual_duration - tts_duration,
            'clip_count': len(clips),
            'sentence_count': len(sentences),
            'fps': 25,
            'resolution': '1280x720',
            'created_at': datetime.now().isoformat(),
            'performance': {
                'total_seconds': total_elapsed,
                'transcode_seconds': time.time() - transcode_start,
                'compose_seconds': compose_elapsed
            },
            'clip_allocation': [
                {
                    'duration': c['duration'],
                    'sentence_index': c.get('sentence_index', -1),
                    'source_idx': c.get('source_index', -1)
                }
                for c in clips
            ]
        }
        
        os.makedirs(os.path.join(storage.workdir, 'tasks'), exist_ok=True)
        task_json_path = os.path.join(storage.workdir, 'tasks', f'{task_id}.json')
        with open(task_json_path, 'w', encoding='utf-8') as f:
            json.dump(task_info, f, ensure_ascii=False, indent=2)
        
        print(f"\n  任务信息已保存: {task_json_path}")
        
        return task_id, output_path
    else:
        print("合成失败!")
        return None, None

if __name__ == '__main__':
    main()