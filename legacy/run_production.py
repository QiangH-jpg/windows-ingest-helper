#!/usr/bin/env python3
"""
正式样片生成脚本 - 新闻稿绑定版

正文来源：
- 必须从外部传入新闻稿/口播稿
- 禁止使用硬编码测试文案
- TTS、字幕、时长全部基于用户稿件

用法：
  python run_production.py --script "用户新闻稿内容"
  或
  python run_production.py --script-file 稿件.txt
"""
import os, sys, json, uuid, asyncio, argparse
from datetime import datetime
sys.path.insert(0, '/home/admin/.openclaw/workspace/video-tool')

from core.storage import storage
from pipeline import processor
from pipeline.tts_provider import generate_tts, create_subtitle_srt_from_meta

# 默认素材（可配置）
DEFAULT_MATERIALS = [
    '/home/admin/.openclaw/workspace/video-tool/uploads/ef50db2c-6423-440d-9c82-0d5622aefac7.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/1eb45180-814d-48a3-9d23-4639e3d1c42f.MP4',
    '/home/admin/.openclaw/workspace/video-tool/uploads/91faffb2-62f5-4ec4-8755-1d55df66013b.MP4',
]

def validate_script(script_text):
    """验证稿件是否有效"""
    if not script_text or not script_text.strip():
        raise ValueError("❌ 稿件不能为空！正式样片必须有用户稿件。")
    
    # 检测测试文案特征
    test_patterns = [
        "测试视频",
        "欢迎观看",
        "验证测试",
        "这是测试",
        "自动生成的成片",
        "感谢您观看本次自动成片验证测试样片"
    ]
    
    for pattern in test_patterns:
        if pattern in script_text:
            print(f"⚠️ 警告：检测到测试文案特征「{pattern}」")
            print("⚠️ 正式样片应使用真实新闻稿，而非测试文案。")
    
    return script_text.strip()

def build_long_segments(clips, min_clips=2, max_clips=3):
    """构建长片段"""
    if not clips:
        return []
    
    segments = []
    i = 0
    while i < len(clips):
        remaining = len(clips) - i
        if remaining >= max_clips:
            segments.append(clips[i:i+max_clips])
            i += max_clips
        elif remaining >= min_clips:
            segments.append(clips[i:i+min_clips])
            i += min_clips
        else:
            segments.append([clips[i]])
            i += 1
    
    return segments

def run_production(script_text, materials=None):
    """
    正式样片生成主流程
    
    Args:
        script_text: 用户新闻稿/口播稿（正文唯一来源）
        materials: 素材路径列表（可选）
    """
    # 验证稿件
    script_text = validate_script(script_text)
    
    task_id = str(uuid.uuid4())
    print(f"task_id: {task_id}")
    print(f"\n=== 正文来源确认 ===")
    print(f"稿件长度: {len(script_text)} 字符")
    print(f"稿件预览: {script_text[:100]}...")
    
    if materials is None:
        materials = DEFAULT_MATERIALS
    
    print(f"\n=== 素材准备 ===")
    print(f"素材数量: {len(materials)}")
    
    # 转码 + 切段
    all_clips = []
    for i, path in enumerate(materials):
        if not os.path.exists(path):
            print(f"⚠️ 素材不存在: {path}")
            continue
        
        transcode_path = os.path.join(storage.workdir, f"{task_id}_transcoded_{i}.mp4")
        print(f"转码素材{i}: {os.path.basename(path)}")
        processor.transcode_to_h264(path, transcode_path)
        
        clips = processor.extract_clips(transcode_path, clip_duration=5)
        for c in clips:
            c['source_index'] = i
        all_clips.extend(clips[:3])
        print(f"  切得 {len(clips[:3])} 个clip")
    
    if not all_clips:
        raise ValueError("❌ 没有可用的素材！")
    
    # 按素材分组
    clips_by_src = {}
    for c in all_clips:
        src = c['source_index']
        if src not in clips_by_src:
            clips_by_src[src] = []
        clips_by_src[src].append(c)
    
    # 长片段优先选片
    print(f"\n=== 选片（长片段优先）===")
    segments_by_source = {}
    for src, clips in clips_by_src.items():
        segments = build_long_segments(clips, min_clips=2, max_clips=3)
        segments_by_source[src] = segments
        seg_info = [f"{len(s)*5}s" for s in segments]
        print(f"素材{src}: {len(clips)} clips → {len(segments)} segments")
    
    # 交错选择segment
    selected = []
    segment_info = []
    max_segments = max(len(segs) for segs in segments_by_source.values()) if segments_by_source else 0
    
    for seg_idx in range(max_segments):
        for src in sorted(segments_by_source.keys()):
            segments = segments_by_source[src]
            if seg_idx < len(segments):
                segment = segments[seg_idx]
                segment_info.append({
                    'source_index': src,
                    'clip_count': len(segment),
                    'duration': len(segment) * 5
                })
                for clip in segment:
                    selected.append(clip)
    
    sources = list(set(c['source_index'] for c in selected))
    total_duration = len(selected) * 5
    
    print(f"\n选片结果:")
    print(f"  clip数: {len(selected)}")
    print(f"  segment数: {len(segment_info)}")
    print(f"  预计时长: {total_duration}秒")
    
    # 保存 timeline
    timeline_path = os.path.join(storage.workdir, f"{task_id}_timeline.json")
    with open(timeline_path, 'w') as f:
        json.dump({
            'task_id': task_id,
            'selected_clips': [
                {'clip_path': c['path'], 'source_index': c['source_index'], 'start': c['start'], 'duration': c['duration']}
                for c in selected
            ],
            'segment_info': segment_info,
            'sources_used': sources,
            'selection_mode': 'long_segment_priority',
            'no_loop': True,
            'created_at': datetime.now().isoformat()
        }, f, ensure_ascii=False, indent=2)
    
    # ========== 正文绑定关键步骤 ==========
    print(f"\n=== 正文绑定（TTS + 字幕）===")
    print(f"稿件来源: 用户输入")
    print(f"TTS文本: 同一稿件")
    print(f"字幕文本: 同一稿件")
    
    # TTS - 使用用户稿件
    tts_path = os.path.join(storage.workdir, f"{task_id}_tts.mp3")
    tts_meta_path = os.path.join(storage.workdir, f"{task_id}_tts_meta.json")
    tts_meta = asyncio.run(generate_tts(script_text, tts_path, tts_meta_path))
    print(f"TTS时长: {tts_meta['total_duration']}秒")
    
    # SRT - 从TTS元数据生成（与TTS一致）
    srt_path = os.path.join(storage.workdir, f"{task_id}.srt")
    create_subtitle_srt_from_meta(tts_meta, srt_path)
    
    # 保存任务信息
    task_info = {
        'id': task_id,
        'status': 'prepared',
        'script': script_text,  # 用户稿件
        'script_source': 'user_input',  # 标记来源
        'tts_source': 'user_script',  # TTS来源用户稿件
        'subtitle_source': 'tts_meta',  # 字幕来自TTS
        'sources_used': sources,
        'total_duration': total_duration,
        'created_at': datetime.now().isoformat()
    }
    
    os.makedirs(os.path.join(storage.workdir, 'tasks'), exist_ok=True)
    with open(os.path.join(storage.workdir, 'tasks', f'{task_id}.json'), 'w') as f:
        json.dump(task_info, f, ensure_ascii=False, indent=2)
    
    print(f"\n=== 准备合成 ===")
    print(f"task_id: {task_id}")
    print(f"稿件: {len(script_text)} 字")
    print(f"TTS: {tts_meta['total_duration']} 秒")
    print(f"视频: {total_duration} 秒")
    
    return task_id, script_text, tts_meta

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='正式样片生成 - 新闻稿绑定版')
    parser.add_argument('--script', type=str, help='新闻稿/口播稿文本')
    parser.add_argument('--script-file', type=str, help='新闻稿/口播稿文件路径')
    
    args = parser.parse_args()
    
    # 获取稿件
    script_text = None
    if args.script:
        script_text = args.script
    elif args.script_file:
        with open(args.script_file, 'r', encoding='utf-8') as f:
            script_text = f.read()
    else:
        print("❌ 必须提供稿件！使用 --script 或 --script-file")
        print("\n示例:")
        print('  python run_production.py --script "今天我们来关注..."')
        print('  python run_production.py --script-file 新闻稿.txt')
        sys.exit(1)
    
    # 生成样片
    task_id, script, tts_meta = run_production(script_text)
    
    print(f"\n=== 后续步骤 ===")
    print(f"task_id: {task_id}")
    print(f"需要执行 ffmpeg 合成视频")
    print(f"或通过 Web API 提交任务")