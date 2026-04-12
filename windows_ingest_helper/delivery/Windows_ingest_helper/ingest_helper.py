#!/usr/bin/env python3
"""
Windows 上传/预处理助手 v1.0
功能：扫描素材目录 → 转码 720p proxy → 检测坏片 → 上传 TOS → 生成清单 JSON

运行方式：
    python ingest_helper.py --input "C:\Videos" --output "./output"

输出结构：
    output/
    ├── manifest.json          # 素材清单
    ├── proxy/                 # 720p proxy 文件
    ├── logs/                  # 处理日志
    └── uploads/               # 已上传记录
"""
import os
import sys
import json
import subprocess
import hashlib
import argparse
from datetime import datetime
from pathlib import Path

# 配置
FFMPEG = "ffmpeg"  # Windows 需确保 ffmpeg 在 PATH 中
PROXY_WIDTH = 1280
PROXY_HEIGHT = 720
PROXY_FPS = 25
PROXY_BITRATE = "3M"

def get_video_info(video_path):
    """获取视频元数据"""
    # 使用 ffprobe 而不是 ffmpeg
    FFPROBE = FFMPEG.replace('ffmpeg', 'ffprobe')
    cmd = [
        FFPROBE, '-v', 'error',
        '-show_entries', 'stream=width,height,duration,r_frame_rate,codec_name',
        '-show_entries', 'format=filename,size',
        '-of', 'json',
        video_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if not result.stdout.strip():
            print(f"  ❌ ffprobe 返回空")
            return None
        data = json.loads(result.stdout)
        stream = data.get('streams', [{}])[0]
        fmt = data.get('format', {})
        return {
            'width': stream.get('width', 0),
            'height': stream.get('height', 0),
            'duration': float(stream.get('duration', 0)),
            'fps': stream.get('r_frame_rate', '0/1'),
            'codec': stream.get('codec_name', 'unknown'),
            'size': int(fmt.get('size', 0)),
        }
    except Exception as e:
        print(f"  ❌ 获取元数据失败：{e}")
        return None

def compute_file_hash(file_path):
    """计算文件 SHA256 哈希"""
    sha256 = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            sha256.update(chunk)
    return sha256.hexdigest()

def is_bad_video(video_path, info):
    """检测坏片"""
    reasons = []
    
    # 1. 无法播放（已在 get_video_info 中捕获）
    if info is None:
        reasons.append("无法播放")
        return True, reasons
    
    # 2. 时长过短（< 2 秒）
    if info['duration'] < 2:
        reasons.append(f"时长过短 ({info['duration']:.1f}s)")
    
    # 3. 分辨率过低（< 360p）
    if info['height'] < 360:
        reasons.append(f"分辨率过低 ({info['width']}x{info['height']})")
    
    # 4. 文件大小异常（< 100KB）
    if info['size'] < 102400:
        reasons.append(f"文件大小异常 ({info['size']/1024:.1f}KB)")
    
    return len(reasons) > 0, reasons

def transcode_to_proxy(input_path, output_path):
    """转码为 720p proxy"""
    cmd = [
        FFMPEG, '-y',
        '-i', input_path,
        '-vf', f'scale={PROXY_WIDTH}:{PROXY_HEIGHT}:force_original_aspect_ratio=decrease,pad={PROXY_WIDTH}:{PROXY_HEIGHT}:(ow-iw)/2:(oh-ih)/2',
        '-c:v', 'libx264',
        '-preset', 'fast',
        '-b:v', PROXY_BITRATE,
        '-r', str(PROXY_FPS),
        '-c:a', 'aac',
        '-b:a', '128k',
        output_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return result.returncode == 0
    except Exception as e:
        print(f"  ❌ 转码失败：{e}")
        return False

def scan_directory(input_dir):
    """扫描素材目录"""
    video_extensions = {'.mp4', '.mov', '.avi', '.mkv', '.m4v', '.mov'}
    video_files = []
    
    for root, dirs, files in os.walk(input_dir):
        for file in files:
            if Path(file).suffix.lower() in video_extensions:
                video_files.append(os.path.join(root, file))
    
    return sorted(video_files)

def process_videos(input_dir, output_dir):
    """处理所有视频"""
    print(f"\n📁 扫描素材目录：{input_dir}")
    video_files = scan_directory(input_dir)
    print(f"  找到 {len(video_files)} 个视频文件")
    
    # 创建输出目录
    proxy_dir = os.path.join(output_dir, 'proxy')
    logs_dir = os.path.join(output_dir, 'logs')
    uploads_dir = os.path.join(output_dir, 'uploads')
    os.makedirs(proxy_dir, exist_ok=True)
    os.makedirs(logs_dir, exist_ok=True)
    os.makedirs(uploads_dir, exist_ok=True)
    
    # 处理清单
    manifest = {
        'version': '1.0',
        'created_at': datetime.now().isoformat(),
        'input_directory': input_dir,
        'total_files': len(video_files),
        'processed_files': [],
        'bad_files': [],
        'duplicates': []
    }
    
    # 处理每个视频
    for i, video_path in enumerate(video_files):
        print(f"\n[{i+1}/{len(video_files)}] {os.path.basename(video_path)}")
        
        # 1. 获取元数据
        print("  获取元数据...", end=' ')
        info = get_video_info(video_path)
        if info:
            print(f"✅ {info['width']}x{info['height']} {info['duration']:.1f}s")
        else:
            print("❌ 无法读取")
            manifest['bad_files'].append({
                'original_path': video_path,
                'reason': '无法读取元数据',
                'status': 'bad'
            })
            continue
        
        # 2. 检测坏片
        is_bad, reasons = is_bad_video(video_path, info)
        if is_bad:
            print(f"  ⚠️ 坏片：{', '.join(reasons)}")
            manifest['bad_files'].append({
                'original_path': video_path,
                'original_info': info,
                'reasons': reasons,
                'status': 'bad'
            })
            continue
        
        # 3. 计算哈希（用于去重）
        file_hash = compute_file_hash(video_path)
        
        # 4. 转码 proxy
        proxy_filename = f"proxy_{i:04d}_{Path(video_path).stem}.mp4"
        proxy_path = os.path.join(proxy_dir, proxy_filename)
        print(f"  转码 proxy...", end=' ')
        if transcode_to_proxy(video_path, proxy_path):
            proxy_size = os.path.getsize(proxy_path)
            print(f"✅ {proxy_size/1024/1024:.1f}MB")
        else:
            print("❌ 失败")
            continue
        
        # 5. 记录到清单
        manifest['processed_files'].append({
            'index': i,
            'original_path': video_path,
            'original_filename': os.path.basename(video_path),
            'original_info': info,
            'file_hash': file_hash,
            'proxy_path': proxy_path,
            'proxy_filename': proxy_filename,
            'proxy_size': proxy_size,
            'tos_key': None,  # 待上传后填写
            'tos_url': None,  # 待上传后填写
            'upload_status': 'pending',
            'preprocess_notes': []
        })
    
    # 6. 检测重复文件（基于哈希）
    hash_map = {}
    for item in manifest['processed_files']:
        h = item['file_hash']
        if h in hash_map:
            manifest['duplicates'].append({
                'file': item['original_path'],
                'duplicate_of': hash_map[h],
                'hash': h
            })
            item['preprocess_notes'].append(f"重复文件：{hash_map[h]}")
        else:
            hash_map[h] = item['original_path']
    
    # 7. 保存清单
    manifest_path = os.path.join(output_dir, 'manifest.json')
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    
    # 8. 保存日志
    log_path = os.path.join(logs_dir, f'ingest_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log')
    with open(log_path, 'w', encoding='utf-8') as f:
        f.write(f"Input: {input_dir}\n")
        f.write(f"Output: {output_dir}\n")
        f.write(f"Total: {len(video_files)} files\n")
        f.write(f"Processed: {len(manifest['processed_files'])} files\n")
        f.write(f"Bad: {len(manifest['bad_files'])} files\n")
        f.write(f"Duplicates: {len(manifest['duplicates'])} files\n")
    
    print(f"\n✅ 处理完成")
    print(f"  处理成功：{len(manifest['processed_files'])} 个")
    print(f"  坏片：{len(manifest['bad_files'])} 个")
    print(f"  重复：{len(manifest['duplicates'])} 个")
    print(f"  清单：{manifest_path}")
    
    return manifest

def main():
    parser = argparse.ArgumentParser(description='Windows 上传/预处理助手')
    parser.add_argument('--input', required=True, help='输入素材目录')
    parser.add_argument('--output', default='./output', help='输出目录')
    args = parser.parse_args()
    
    print("=" * 60)
    print("Windows 上传/预处理助手 v1.0")
    print("=" * 60)
    
    manifest = process_videos(args.input, args.output)
    
    print("\n" + "=" * 60)
    print("下一步：上传到 TOS")
    print("命令：python upload_to_tos.py --manifest ./output/manifest.json")
    print("=" * 60)

if __name__ == '__main__':
    main()
