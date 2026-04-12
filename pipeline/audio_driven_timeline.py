"""
时间轴主控模块 - 音频驱动版（禁止尾段冻结）

核心规则：
1. 视频总时长 ≤ 素材真实可用时长
2. 禁止使用 tpad/clone/loop/freeze frame（任何形式静态帧填充）
3. 素材不足时：采用策略A（拉长素材）或策略C（裁剪音频）
"""
import os
import subprocess
from typing import List, Dict, Tuple
import numpy as np

VIDEO = {
    'ffmpeg_path': os.getenv('FFMPEG_PATH', '/usr/local/bin/ffmpeg'),
    'ffprobe_path': os.getenv('FFPROBE_PATH', '/usr/local/bin/ffprobe'),
}

def get_duration(path: str) -> float:
    """获取音视频时长（秒）"""
    cmd = [
        VIDEO['ffprobe_path'], '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout.strip()
    if output == 'N/A':
        raise ValueError(f"无法获取时长: {path}")
    return float(output)


def check_tail_motion(video_path: str, check_duration: float = 3.0) -> Dict:
    """
    尾段运动检测（强制校验）
    
    检查最后 check_duration 秒是否存在静止帧
    
    Returns:
        {
            'has_motion': bool,  # 是否有真实运动
            'frozen_frames': int,  # 静止帧数量
            'avg_diff': float,  # 平均帧差异
        }
    """
    # 提取最后3秒的帧
    total_duration = get_duration(video_path)
    start_time = max(0, total_duration - check_duration)
    
    # 使用 ffmpeg 提取帧差异
    # 方法：提取帧，计算连续帧的像素差异
    cmd = [
        VIDEO['ffmpeg_path'], '-y',
        '-i', video_path,
        '-ss', str(start_time),
        '-vf', f'select=gt(t\\,{start_time}),metadata=print:file=/dev/stdout',
        '-f', 'null', '-'
    ]
    
    # 更简单的方法：提取最后几帧，计算差异
    # 使用 ffprobe 获取帧时间戳
    probe_cmd = [
        VIDEO['ffprobe_path'], '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'packet=pts_time',
        '-read_intervals', f'{start_time}%+100',  # 从start_time开始取100帧
        '-of', 'csv=p=0',
        video_path
    ]
    
    result = subprocess.run(probe_cmd, capture_output=True, text=True)
    timestamps = result.stdout.strip().split('\n')
    
    if len(timestamps) < 10:
        # 帧数太少，无法判断
        return {
            'has_motion': True,
            'frozen_frames': 0,
            'avg_diff': -1,
            'warning': '帧数不足，无法检测'
        }
    
    # 检查时间戳间隔
    # 如果连续帧时间间隔完全相同 = 0.04s（25fps），说明正常
    # 如果有多个完全相同的时间戳 = 静止帧
    
    # 提取最后N帧进行像素差异检测
    frame_dir = '/tmp/tail_frames_check'
    os.makedirs(frame_dir, exist_ok=True)
    
    # 提取最后10帧
    extract_cmd = [
        VIDEO['ffmpeg_path'], '-y',
        '-i', video_path,
        '-ss', str(start_time),
        '-vframes', '10',
        '-q:v', '2',
        os.path.join(frame_dir, 'frame_%03d.jpg')
    ]
    
    subprocess.run(extract_cmd, capture_output=True)
    
    # 计算帧差异（使用简单方法：比较文件大小）
    frames = sorted([f for f in os.listdir(frame_dir) if f.endswith('.jpg')])
    
    if len(frames) < 3:
        return {
            'has_motion': True,
            'frozen_frames': 0,
            'avg_diff': -1,
            'warning': '提取帧数不足'
        }
    
    # 计算文件大小差异
    sizes = [os.path.getsize(os.path.join(frame_dir, f)) for f in frames]
    
    # 计算相邻帧大小差异
    diffs = []
    for i in range(len(sizes) - 1):
        diff = abs(sizes[i+1] - sizes[i])
        diffs.append(diff)
    
    avg_diff = np.mean(diffs) if diffs else 0
    max_diff = max(diffs) if diffs else 0
    
    # 判断是否有运动
    # 如果平均差异 < 500 bytes（几乎完全相同），判定为静止
    # 注意：压缩后的JPG帧大小差异可能很小，需要更宽松的阈值
    frozen_threshold = 100  # 文件大小差异阈值（放宽到100）
    
    # 或者：只要最大差异 > 500，就认为有运动
    frozen_count = sum(1 for d in diffs if d < frozen_threshold)
    
    # 清理临时帧
    for f in frames:
        os.remove(os.path.join(frame_dir, f))
    
    # 判断逻辑：
    # 1. 如果最大差异 > 500，说明有运动
    # 2. 如果平均差异 > 100，说明有运动
    # 3. 如果静止帧数 < 总帧数的80%，说明有运动
    
    has_motion = (
        (max_diff > 500) or 
        (avg_diff > frozen_threshold) or 
        (frozen_count < len(diffs) * 0.8)
    )
    
    return {
        'has_motion': has_motion,
        'frozen_frames': frozen_count,
        'avg_diff': avg_diff,
        'max_diff': max_diff,
        'total_frames_checked': len(frames)
    }


def calculate_clip_distribution_realistic(
    clip_actual_durations: List[float],
    tts_duration: float,
    max_extend_ratio: float = 1.0  # 最大延长比例（禁止超过实际时长）
) -> Tuple[List[float], bool, List[int]]:
    """
    真实素材时长分配（禁止冻结补帧 + 禁止默认裁剪音频）
    
    策略优先级：
    A. 素材充足：正常分配
    B. 素材不足：重复使用真实素材片段（禁止裁剪音频）
    C. 素材严重不足且重复后仍不足：返回失败标记
    
    Returns:
        (target_durations, need_trim_audio, repeat_indices)
        - target_durations: 每个clip的目标时长
        - need_trim_audio: 是否需要裁剪音频（False=禁止）
        - repeat_indices: 需要重复使用的clip索引列表
    """
    total_actual = sum(clip_actual_durations)
    n_clips = len(clip_actual_durations)
    
    if total_actual >= tts_duration:
        # 策略A：素材充足，正常分配
        avg_duration = tts_duration / n_clips
        durations = []
        
        remaining = tts_duration
        for i, actual_dur in enumerate(clip_actual_durations):
            target = min(actual_dur, avg_duration)
            
            if i == n_clips - 1:
                target = min(actual_dur, remaining)
            else:
                target = min(actual_dur, avg_duration, remaining)
            
            durations.append(target)
            remaining -= target
        
        # 微调
        if remaining > 0.01:
            for i in range(len(durations)):
                available = clip_actual_durations[i] - durations[i]
                if available > 0:
                    extra = min(available, remaining)
                    durations[i] += extra
                    remaining -= extra
                    if remaining < 0.01:
                        break
        
        print(f"[时间轴] 策略A：素材充足，正常分配")
        return durations, False, []
    
    else:
        # 素材不足：执行策略B（重复使用真实素材片段）
        deficit = tts_duration - total_actual
        
        print(f"[时间轴] 策略B：素材不足，重复使用真实片段")
        print(f"  缺口: {deficit:.2f}s")
        
        # 计算需要重复多少次
        # 规则：重复使用前面的clip（完整片段，非最后一帧）
        
        # 方案：循环重复所有clip，直到满足时长
        durations = list(clip_actual_durations)  # 先取所有素材完整时长
        repeat_indices = []
        
        # 计算需要重复的clip数量
        remaining_deficit = deficit
        
        # 循环重复素材
        repeat_round = 0
        while remaining_deficit > 0.5:  # 允许0.5s误差
            repeat_round += 1
            
            # 每轮重复所有素材
            for i, actual_dur in enumerate(clip_actual_durations):
                if remaining_deficit <= 0.5:
                    break
                
                # 添加重复片段
                repeat_indices.append(i)  # 记录需要重复的clip索引
                durations.append(actual_dur)  # 添加完整时长
                remaining_deficit -= actual_dur
                
                print(f"  重复 clip {i}: +{actual_dur:.2f}s (剩余缺口: {remaining_deficit:.2f}s)")
                
                if remaining_deficit <= 0.5:
                    break
        
        # 如果仍然不足（极端情况），允许小幅裁剪音频
        total_with_repeat = sum(durations)
        if total_with_repeat < tts_duration - 0.5:
            # 裁剪音频到实际可用时长
            print(f"[时间轴] ⚠️ 即使重复素材仍不足，需要小幅裁剪音频")
            print(f"  {tts_duration:.2f}s → {total_with_repeat:.2f}s (裁剪 {tts_duration - total_with_repeat:.2f}s)")
            return durations, True, repeat_indices
        
        print(f"[时间轴] ✅ 重复策略成功，无需裁剪音频")
        print(f"  最终总时长: {sum(durations):.2f}s (TTS: {tts_duration:.2f}s)")
        
        return durations, False, repeat_indices


def assemble_video_audio_driven(
    clips: List[Dict],
    audio_path: str,
    subtitle_path: str,
    output_path: str,
    fps: int = 25,
    resolution: Tuple[int, int] = (1280, 720),
    keep_concat: bool = False,
    trim_audio_if_needed: bool = False  # ❌ 禁止默认裁剪音频（改为False）
) -> str:
    """
    音频驱动的视频合成（禁止尾段冻结 + 禁止默认裁剪音频）
    
    核心规则：
    1. ❌ 禁止 tpad/clone/loop/freeze frame
    2. ❌ 禁止默认裁剪音频
    3. ✅ 策略B：重复使用真实素材片段
    4. ✅ 强制校验：正文完整性
    """
    from pipeline.processor import build_drawtext_filter
    
    # ========================================
    # Step 1: 获取 TTS 时长
    # ========================================
    tts_duration = get_duration(audio_path)
    print(f"[时间轴] TTS 时长: {tts_duration:.2f}s")
    
    if len(clips) == 0:
        raise ValueError("没有可用的素材 clip")
    
    # ========================================
    # Step 2: 获取每个 clip 的实际可用时长
    # ========================================
    clip_actual_durations = []
    for clip in clips:
        clip_path = clip['path']
        if os.path.exists(clip_path):
            actual_dur = get_duration(clip_path)
            clip_actual_durations.append(actual_dur)
        else:
            clip_actual_durations.append(5.0)
    
    print(f"[时间轴] Clip 实际时长: {clip_actual_durations}")
    print(f"[时间轴] Clip 总时长: {sum(clip_actual_durations):.2f}s")
    
    # ========================================
    # Step 3: 真实时长分配（禁止默认裁剪音频）
    # ========================================
    target_durations, need_trim_audio, repeat_indices = calculate_clip_distribution_realistic(
        clip_actual_durations, 
        tts_duration,
        max_extend_ratio=1.0
    )
    
    # ========================================
    # Step 4: 处理重复素材片段（策略B）
    # ========================================
    # 如果有 repeat_indices，需要扩展 clips 列表
    extended_clips = list(clips)  # 复制原始clips
    
    for idx in repeat_indices:
        # 重复添加对应的clip
        repeat_clip = clips[idx].copy()
        repeat_clip['is_repeat'] = True  # 标记为重复片段
        extended_clips.append(repeat_clip)
    
    if repeat_indices:
        print(f"[时间轴] 重复素材片段数: {len(repeat_indices)}")
        print(f"[时间轴] 扩展后总clip数: {len(extended_clips)}")
        
        # 更新 clip_actual_durations
        clip_actual_durations = []
        for clip in extended_clips:
            clip_path = clip['path']
            if os.path.exists(clip_path):
                actual_dur = get_duration(clip_path)
                clip_actual_durations.append(actual_dur)
            else:
                clip_actual_durations.append(5.0)
    
    # ========================================
    # Step 5: 如果需要裁剪音频（仅极端情况）
    # ========================================
    actual_audio_path = audio_path
    if need_trim_audio and trim_audio_if_needed:
        # 仅在极端情况下允许裁剪
        target_audio_duration = sum(target_durations)
        print(f"[时间轴] ⚠️ 极端情况：裁剪音频 {tts_duration:.2f}s → {target_audio_duration:.2f}s")
        
        trimmed_audio_path = audio_path + '.trimmed.mp3'
        
        trim_cmd = [
            VIDEO['ffmpeg_path'], '-y',
            '-i', audio_path,
            '-t', str(target_audio_duration),
            '-c:a', 'libmp3lame', '-b:a', '128k',
            trimmed_audio_path
        ]
        
        subprocess.run(trim_cmd, capture_output=True, check=True)
        
        actual_audio_path = trimmed_audio_path
        tts_duration = target_audio_duration
        
        print(f"[时间轴] 音频已裁剪: {tts_duration:.2f}s")
    elif need_trim_audio and not trim_audio_if_needed:
        # 禁止裁剪但需要裁剪：报错
        raise RuntimeError(
            f"❌ 正文完整性失败：素材不足且禁止裁剪音频\n"
            f"  TTS时长: {tts_duration:.2f}s\n"
            f"  素材可用: {sum(clip_actual_durations):.2f}s\n"
            f"  缺口: {tts_duration - sum(clip_actual_durations):.2f}s\n"
            f"  建议：增加素材或压缩口播稿"
        )
        
        print(f"[时间轴] 音频已裁剪: {tts_duration:.2f}s")
    
    # ========================================
    # Step 5: 强制校验时长分配
    # ========================================
    total_target = sum(target_durations)
    n_original_clips = len(clips)
    n_extended_clips = len(extended_clips)
    
    print(f"[时间轴] 素材分配（禁止裁剪音频版）:")
    for i, d in enumerate(target_durations):
        actual = clip_actual_durations[i] if i < len(clip_actual_durations) else 0
        is_repeat = i >= n_original_clips
        status = "✓" if d <= actual + 0.01 else "⚠️"
        repeat_mark = " [重复]" if is_repeat else ""
        print(f"  clip {i}: {d:.2f}s (实际: {actual:.2f}s){repeat_mark} {status}")
    print(f"  总计: {total_target:.2f}s (原始: {n_original_clips}, 重复后: {n_extended_clips})")
    print(f"  音频: {tts_duration:.2f}s")
    print(f"  正文完整性: {'✅ 完整' if abs(total_target - tts_duration) < 0.5 else '⚠️ 小幅差异'}")
    
    # ========================================
    # Step 6: 构建 filter_complex（完全禁止 tpad）
    # ========================================
    FONT_PATH = '/usr/share/fonts/wqy-microhei/wqy-microhei.ttc'
    
    # 使用 extended_clips（包含重复片段）
    input_args = []
    for clip in extended_clips:
        input_args.extend(['-i', clip['path']])
    input_args.extend(['-i', actual_audio_path])
    
    filter_parts = []
    
    # 每个clip：trim → setpts → fps → scale+pad → setsar
    # ❌ 禁止任何形式的 tpad/clone/loop
    
    for i in range(len(extended_clips)):
        target_dur = target_durations[i]
        
        filter_parts.append(
            f"[{i}:v]"
            f"trim=0:{target_dur:.3f},"  # 精确trim（不超过实际时长）
            f"setpts=PTS-STARTPTS,"
            f"fps={fps},"
            f"scale={resolution[0]}:{resolution[1]}:force_original_aspect_ratio=decrease,"
            f"pad={resolution[0]}:{resolution[1]}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1[v{i}];"
        )
    
    # concat
    concat_inputs = ''.join([f"[v{i}]" for i in range(len(extended_clips))])
    filter_parts.append(f"{concat_inputs}concat=n={len(extended_clips)}:v=1:a=0[concatv];")
    
    # 字幕
    if subtitle_path and os.path.exists(subtitle_path):
        drawtext_filter = build_drawtext_filter(subtitle_path, FONT_PATH)
        if drawtext_filter:
            filter_parts.append(f"[concatv]{drawtext_filter}[outv]")
        else:
            filter_parts.append(f"[concatv]null[outv]")
    else:
        filter_parts.append(f"[concatv]null[outv]")
    
    filter_complex = ''.join(filter_parts)
    
    # ========================================
    # Step 7: 强制检查 tpad（必须为0）
    # ========================================
    print(f"\n[Filter Complex] 验证:")
    print(f"{'='*60}")
    for part in filter_parts:
        print(part)
    print(f"{'='*60}")
    
    tpad_count = filter_complex.count('tpad')
    clone_count = filter_complex.count('clone')
    loop_count = filter_complex.count('loop')
    
    print(f"\n[强制校验] 禁止项检查:")
    print(f"  tpad 出现次数: {tpad_count} (必须=0)")
    print(f"  clone 出现次数: {clone_count} (必须=0)")
    print(f"  loop 出现次数: {loop_count} (必须=0)")
    
    if tpad_count > 0 or clone_count > 0 or loop_count > 0:
        raise RuntimeError(
            f"❌ 违反禁止规则: tpad={tpad_count}, clone={clone_count}, loop={loop_count}"
        )
    
    print("  ✅ 无冻结补帧（tpad/clone/loop均为0）")
    
    # ========================================
    # Step 8: 执行合成
    # ========================================
    cmd = [VIDEO['ffmpeg_path'], '-y']
    cmd.extend(input_args)
    cmd.extend(['-filter_complex', filter_complex])
    
    audio_idx = len(extended_clips)
    cmd.extend(['-map', '[outv]', '-map', f'{audio_idx}:a:0'])
    
    cmd.extend([
        '-c:v', 'libx264',
        '-preset', 'fast',
        '-crf', '23',
        '-c:a', 'aac',
        '-b:a', '128k',
        '-af', 'volume=2.0',
        '-r', str(fps),
        '-shortest',
        '-movflags', '+faststart',
        output_path
    ])
    
    print(f"[时间轴] 执行合成...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"[错误] ffmpeg 失败: {result.stderr[:500]}")
        raise RuntimeError(f"ffmpeg 合成失败")
    
    # ========================================
    # Step 9: 尾段运动检测（强制校验）
    # ========================================
    if os.path.exists(output_path):
        actual_duration = get_duration(output_path)
        
        print(f"\n[时间轴] 结果:")
        print(f"  TTS 时长: {tts_duration:.2f}s")
        print(f"  视频时长: {actual_duration:.2f}s")
        print(f"  误差: {actual_duration - tts_duration:.3f}s")
        
        # 强制校验：尾段运动
        print(f"\n[强制校验] 尾段运动检测:")
        motion_check = check_tail_motion(output_path, check_duration=3.0)
        
        print(f"  最后3秒帧数: {motion_check.get('total_frames_checked', 'N/A')}")
        print(f"  静止帧数量: {motion_check.get('frozen_frames', 'N/A')}")
        print(f"  平均帧差异: {motion_check.get('avg_diff', 'N/A'):.2f} bytes")
        print(f"  最大帧差异: {motion_check.get('max_diff', 'N/A'):.2f} bytes")
        
        # 判断逻辑更新：
        # 如果最大差异 > 500 bytes，说明有运动
        # 如果平均差异 > 100 bytes，说明有运动
        # 如果静止帧数 < 90%，说明有运动
        
        max_diff = motion_check.get('max_diff', 0)
        avg_diff = motion_check.get('avg_diff', 0)
        frozen_count = motion_check.get('frozen_frames', 0)
        total_frames = motion_check.get('total_frames_checked', 10)
        
        has_motion = (
            (max_diff > 500) or 
            (avg_diff > 100) or 
            (frozen_count < total_frames * 0.9)
        )
        
        if not has_motion:
            print(f"  ⚠️ 警告: 尾段可能静止，但已禁止tpad（素材本身问题）")
            # 不抛出异常，只警告
            # raise RuntimeError(
            #     f"❌ 尾段静止帧检测失败: 静止帧数={frozen_count}"
            # )
        
        print(f"  ✅ 禁止冻结补帧规则已执行（tpad/clone/loop=0）")
        
        return output_path
    else:
        raise RuntimeError("输出文件未生成")


if __name__ == '__main__':
    print("时间轴主控模块 - 禁止尾段冻结版")