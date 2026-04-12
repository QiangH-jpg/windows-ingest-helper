"""
素材预处理缓存模块（生产级防踩坑版）

核心规则：
1. 只缓存"标准化素材"，禁止缓存clip/timeline/concat中间产物
2. cache key必须绑定处理参数签名
3. 必须包含cache_version
4. 兼容判断必须"完全匹配"
5. 提供清理策略
"""
import os
import json
import subprocess
import hashlib
from typing import Dict, Optional, List

# 导入缓存保护机制
from pipeline.processor import enter_cache_context, exit_cache_context

# ============================================
# 缓存配置（修改此处需升级CACHE_VERSION）
# ============================================
CACHE_VERSION = "v1"
TARGET_CODEC = "h264"
TARGET_WIDTH = 1280
TARGET_HEIGHT = 720
TARGET_FPS = 25
TARGET_PIX_FMT = "yuv420p"

# 缓存目录
PROCESSED_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'processed_videos')
INDEX_FILE = os.path.join(PROCESSED_DIR, 'video_index.json')

# FFmpeg路径
FFPROBE_PATH = os.getenv('FFPROBE_PATH', '/usr/local/bin/ffprobe')
FFMPEG_PATH = os.getenv('FFMPEG_PATH', '/usr/local/bin/ffmpeg')


def get_file_hash(file_path: str) -> str:
    """获取文件MD5哈希（前10MB）"""
    hasher = hashlib.md5()
    with open(file_path, 'rb') as f:
        chunk = f.read(10 * 1024 * 1024)
        hasher.update(chunk)
    return hasher.hexdigest()[:12]


def generate_cache_key(file_hash: str, codec: str, width: int, height: int, 
                        fps: int, pix_fmt: str) -> str:
    """
    生成cache key（必须绑定处理参数签名）
    
    格式：<file_hash>__<codec>__<width>x<height>__<fps>fps__<pix_fmt>__<version>
    
    示例：abc123def456__h264__1280x720__25fps__yuv420p__v1
    """
    return f"{file_hash}__{codec}__{width}x{height}__{fps}fps__{pix_fmt}__{CACHE_VERSION}"


def generate_processed_filename(original_name: str, file_hash: str) -> str:
    """
    生成处理后文件名
    
    格式：<original_name>__<hash>__<codec>__<resolution>__<fps>fps__<pix_fmt>__<version>.mp4
    """
    name_without_ext = os.path.splitext(original_name)[0]
    return f"{name_without_ext}__{file_hash[:8]}__{TARGET_CODEC}__{TARGET_WIDTH}x{TARGET_HEIGHT}__{TARGET_FPS}fps__{TARGET_PIX_FMT}__{CACHE_VERSION}.mp4"


def load_index() -> Dict:
    """加载索引文件"""
    if os.path.exists(INDEX_FILE):
        with open(INDEX_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_index(index: Dict):
    """保存索引文件"""
    with open(INDEX_FILE, 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


def probe_video(file_path: str) -> Optional[Dict]:
    """
    检测视频格式
    
    Returns:
        {
            'codec': str,
            'width': int,
            'height': int,
            'fps': float,
            'pix_fmt': str,
            'duration': float
        }
    """
    cmd = [
        FFPROBE_PATH, '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=codec_name,width,height,r_frame_rate,pix_fmt',
        '-show_entries', 'format=duration',
        '-of', 'json',
        file_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    try:
        data = json.loads(result.stdout)
        stream = data.get('streams', [{}])[0]
        format_info = data.get('format', {})
        
        # 解析帧率
        fps_str = stream.get('r_frame_rate', '25/1')
        if '/' in fps_str:
            num, den = fps_str.split('/')
            fps = float(num) / float(den) if float(den) != 0 else 25.0
        else:
            fps = float(fps_str)
        
        return {
            'codec': stream.get('codec_name', ''),
            'width': int(stream.get('width', 0)),
            'height': int(stream.get('height', 0)),
            'fps': fps,
            'pix_fmt': stream.get('pix_fmt', ''),
            'duration': float(format_info.get('duration', stream.get('duration', 0)))
        }
    except Exception as e:
        print(f"[CACHE] 格式检测失败: {e}")
        return None


def check_format_compatible(video_info: Dict) -> bool:
    """
    检查视频格式是否完全匹配目标（严格匹配）
    
    规则：
    - codec 必须完全等于 TARGET_CODEC
    - fps 必须完全等于 TARGET_FPS（不允许接近）
    - pix_fmt 必须完全等于 TARGET_PIX_FMT
    - resolution 必须完全等于 TARGET_WIDTH x TARGET_HEIGHT
    
    ❌ 禁止用"接近"、"大于等于"等宽松判断
    """
    if video_info is None:
        return False
    
    # 检查编码（必须完全匹配）
    if video_info['codec'] != TARGET_CODEC:
        return False
    
    # 检查帧率（必须完全匹配，误差<0.1）
    if abs(video_info['fps'] - TARGET_FPS) >= 0.1:
        return False
    
    # 检查像素格式（必须完全匹配）
    if video_info['pix_fmt'] != TARGET_PIX_FMT:
        return False
    
    # 检查分辨率（必须完全匹配）
    if video_info['width'] != TARGET_WIDTH or video_info['height'] != TARGET_HEIGHT:
        return False
    
    return True


def get_or_create_processed(source_path: str) -> str:
    """
    获取或创建处理后的素材（缓存入口）
    
    流程：
    1. 计算源文件哈希
    2. 生成cache key（包含参数签名）
    3. 检查缓存索引（必须版本一致）
    4. 命中缓存 → 直接返回
    5. 未命中 → 检测格式
    6. 格式完全匹配 → 标记为无需转码
    7. 格式不匹配 → 转码 → 写入缓存
    
    Returns:
        处理后的文件路径
    """
    # 进入缓存上下文（允许转码操作）
    enter_cache_context()
    
    try:
        index = load_index()
    
        # 计算源文件哈希
        file_hash = get_file_hash(source_path)
        source_name = os.path.basename(source_path)
        
        # 生成cache key（绑定参数签名）
        cache_key = generate_cache_key(
            file_hash, TARGET_CODEC, TARGET_WIDTH, TARGET_HEIGHT, 
            TARGET_FPS, TARGET_PIX_FMT
        )
        
        print(f"\n[CACHE] {source_name}")
        print(f"  cache_key: {cache_key}")
        
        # 检查缓存（必须版本一致）
        if cache_key in index:
            cached_info = index[cache_key]
            cached_path = cached_info.get('processed_path')
            
            # 验证版本
            if cached_info.get('cache_version') != CACHE_VERSION:
                print(f"  → cache version mismatch (旧: {cached_info.get('cache_version')}, 新: {CACHE_VERSION})")
                print(f"  → 强制重新转码")
            elif cached_path and os.path.exists(cached_path):
                print(f"  → cache hit ✅ (version: {CACHE_VERSION})")
                return cached_path
        
        # 未命中缓存
        print(f"  → cache miss")
        
        # 检测视频格式
        video_info = probe_video(source_path)
        
        if video_info:
            print(f"  → 原始格式: {video_info['codec']} {video_info['width']}x{video_info['height']} {video_info['fps']:.2f}fps {video_info['pix_fmt']}")
        
        # 生成处理后的文件名
        processed_name = generate_processed_filename(source_name, file_hash)
        processed_path = os.path.join(PROCESSED_DIR, processed_name)
        
        # 检查是否需要转码
        if video_info and check_format_compatible(video_info):
            # 格式完全匹配，直接复制
            print(f"  → format compatible (完全匹配)，直接复制")
            
            import shutil
            shutil.copy2(source_path, processed_path)
            is_transcoded = False
        else:
            # 格式不匹配，执行转码
            print(f"  → transcode required 🔄")
            
            cmd = [
                FFMPEG_PATH, '-y',
                '-i', source_path,
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                '-vf', f'scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=decrease,pad={TARGET_WIDTH}:{TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2',
                '-r', str(TARGET_FPS),
                '-pix_fmt', TARGET_PIX_FMT,
                '-c:a', 'aac', '-b:a', '128k',
                '-movflags', '+faststart',
                processed_path
            ]
            
            subprocess.run(cmd, capture_output=True, check=True)
            is_transcoded = True
        
        # 更新索引
        index[cache_key] = {
            'cache_key': cache_key,
            'original_path': source_path,
            'original_name': source_name,
            'processed_path': processed_path,
            'processed_name': processed_name,
            'file_hash': file_hash,
            'codec': TARGET_CODEC,
            'resolution': f"{TARGET_WIDTH}x{TARGET_HEIGHT}",
            'fps': TARGET_FPS,
            'pix_fmt': TARGET_PIX_FMT,
            'cache_version': CACHE_VERSION,
            'is_transcoded': is_transcoded,
            'ready': True
        }
        
        save_index(index)
        print(f"  → cached: {processed_name}")
        
        return processed_path
    finally:
        # 退出缓存上下文
        exit_cache_context()


def get_processed_clips(source_path: str, clip_duration: int = 5, 
                        workdir: str = None, task_id: str = None) -> List[Dict]:
    """
    获取处理后的素材片段（缓存入口）
    
    ⚠️ 已废弃：此函数使用固定5秒预切片，不符合动态裁剪规则
    ⚠️ 请使用 extract_dynamic_clip() 替代
    
    保留此函数仅为兼容旧代码，新代码禁止调用
    
    Returns:
        clips列表（已废弃）
    """
    print(f"\n⚠️ WARNING: get_processed_clips() 使用固定5秒预切片，已废弃")
    print(f"   请使用 extract_dynamic_clip() 进行动态裁剪")
    
    # 进入缓存上下文（允许切片操作）
    enter_cache_context()
    
    try:
        # 获取处理后的素材
        processed_path = get_or_create_processed(source_path)
        
        # 获取时长
        probe_cmd = [
            FFPROBE_PATH, '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            processed_path
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True)
        duration = float(result.stdout.strip())
        
        # 切片目录：必须在workdir中，禁止进入processed_videos
        if workdir and task_id:
            clip_dir = os.path.join(workdir, task_id, 'clips')
        else:
            # 兼容旧调用
            clip_dir = os.path.dirname(processed_path)
        
        os.makedirs(clip_dir, exist_ok=True)
        
        clips = []
        i = 0
        
        while i * clip_duration < duration:
            start = i * clip_duration
            
            # clip文件名：必须清晰标识
            source_name = os.path.splitext(os.path.basename(source_path))[0]
            clip_path = os.path.join(clip_dir, f"{source_name}_clip_{i}.mp4")
            
            # 切片（重新编码，确保视频流完整）
            cmd = [
                FFMPEG_PATH, '-y',
                '-i', processed_path,
                '-ss', str(start),
                '-t', str(clip_duration),
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                '-c:a', 'aac', '-b:a', '128k',
                clip_path
            ]
            
            result = subprocess.run(cmd, capture_output=True)
            
            if os.path.exists(clip_path) and os.path.getsize(clip_path) > 1000:
                clips.append({
                    'path': clip_path,
                    'start': start,
                    'duration': clip_duration,
                    'source_index': i
                })
            
            i += 1
        
        return clips
    finally:
        # 退出缓存上下文
        exit_cache_context()


def extract_dynamic_clip(source_path: str, start: float, duration: float,
                         workdir: str = None, task_id: str = None,
                         clip_id: int = 0) -> Dict:
    """
    动态裁剪素材片段（取代固定5秒预切片）
    
    ✅ 符合规则：
    1. 起点任意（不对齐5秒边界）
    2. 时长浮动（3-6秒）
    3. 同一素材多次使用，起点必须不同
    
    Args:
        source_path: 原始素材路径
        start: 起始时间（任意值，如 2.3, 7.1, 12.8）
        duration: 片段时长（3-6秒浮动）
        workdir: 工作目录
        task_id: 任务ID
        clip_id: 片段编号
    
    Returns:
        {
            'path': clip文件路径,
            'start': 实际起始时间,
            'duration': 实际时长,
            'source_name': 素材名称
        }
    """
    # 进入缓存上下文
    enter_cache_context()
    
    try:
        # 获取处理后的素材（标准化）
        processed_path = get_or_create_processed(source_path)
        
        # 获取素材总时长
        probe_cmd = [
            FFPROBE_PATH, '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            processed_path
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True)
        total_duration = float(result.stdout.strip())
        
        # 安全检查：起点不能超出素材时长
        if start >= total_duration:
            print(f"  ⚠️ 警告: 起点 {start}s 超出素材时长 {total_duration}s，调整为末尾")
            start = max(0, total_duration - duration)
        
        # 安全检查：终点不能超出素材时长
        if start + duration > total_duration:
            actual_duration = total_duration - start
            print(f"  ⚠️ 警告: 片段超出素材边界，调整为 {actual_duration:.1f}s")
        else:
            actual_duration = duration
        
        # clip目录：必须在workdir中
        if workdir and task_id:
            clip_dir = os.path.join(workdir, task_id, 'clips')
        else:
            clip_dir = '/tmp/openclaw/clips'
        
        os.makedirs(clip_dir, exist_ok=True)
        
        # 生成clip文件名：包含精确起始时间标识
        source_name = os.path.splitext(os.path.basename(source_path))[0]
        clip_filename = f"{source_name}_dynamic_{clip_id}_start{start:.1f}s_dur{actual_duration:.1f}s.mp4"
        clip_path = os.path.join(clip_dir, clip_filename)
        
        # 动态裁剪（重新编码）
        cmd = [
            FFMPEG_PATH, '-y',
            '-i', processed_path,
            '-ss', str(start),
            '-t', str(actual_duration),
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
            '-c:a', 'aac', '-b:a', '128k',
            clip_path
        ]
        
        result = subprocess.run(cmd, capture_output=True)
        
        if os.path.exists(clip_path) and os.path.getsize(clip_path) > 1000:
            return {
                'path': clip_path,
                'start': start,
                'duration': actual_duration,
                'source_name': source_name,
                'processed_path': processed_path
            }
        else:
            print(f"  ❌ 裁剪失败: {clip_path}")
            return None
    finally:
        exit_cache_context()


def audit_cache() -> Dict:
    """
    审计缓存状态
    
    Returns:
        {
            'total_files': int,
            'total_size_mb': float,
            'stale_files': int,
            'clip_pollution': int,
            'version_mismatch': int,
            'details': List
        }
    """
    index = load_index()
    
    total_files = 0
    total_size = 0
    clip_pollution = 0
    version_mismatch = 0
    stale_files = 0
    details = []
    
    if os.path.exists(PROCESSED_DIR):
        for f in os.listdir(PROCESSED_DIR):
            if f == 'video_index.json':
                continue
            
            file_path = os.path.join(PROCESSED_DIR, f)
            
            if os.path.isfile(file_path):
                total_files += 1
                total_size += os.path.getsize(file_path)
                
                # 检查clip污染
                if '.clip_' in f:
                    clip_pollution += 1
                    details.append({
                        'file': f,
                        'issue': 'clip_pollution',
                        'recommendation': '删除'
                    })
    
    # 检查版本不匹配
    for key, info in index.items():
        if info.get('cache_version') != CACHE_VERSION:
            version_mismatch += 1
            details.append({
                'key': key,
                'issue': 'version_mismatch',
                'old_version': info.get('cache_version'),
                'current_version': CACHE_VERSION,
                'recommendation': '重新转码'
            })
    
    return {
        'total_files': total_files,
        'total_size_mb': total_size / 1024 / 1024,
        'clip_pollution': clip_pollution,
        'version_mismatch': version_mismatch,
        'stale_files': stale_files,
        'details': details,
        'cache_version': CACHE_VERSION
    }


def clear_stale_cache(dry_run: bool = True) -> Dict:
    """
    清理过期/污染缓存
    
    Args:
        dry_run: True=仅统计，False=实际删除
    
    Returns:
        {
            'deleted_files': int,
            'deleted_size_mb': float,
            'details': List
        }
    """
    audit_result = audit_cache()
    
    deleted_files = 0
    deleted_size = 0
    details = []
    
    # 清理clip污染
    if os.path.exists(PROCESSED_DIR):
        for f in os.listdir(PROCESSED_DIR):
            if '.clip_' in f:
                file_path = os.path.join(PROCESSED_DIR, f)
                file_size = os.path.getsize(file_path)
                
                details.append({
                    'file': f,
                    'action': 'delete_clip_pollution',
                    'size_mb': file_size / 1024 / 1024
                })
                
                if not dry_run:
                    os.remove(file_path)
                
                deleted_files += 1
                deleted_size += file_size
    
    return {
        'dry_run': dry_run,
        'deleted_files': deleted_files,
        'deleted_size_mb': deleted_size / 1024 / 1024,
        'details': details
    }


if __name__ == '__main__':
    import sys
    
    if len(sys.argv) > 1:
        action = sys.argv[1]
        
        if action == '--audit-cache':
            result = audit_cache()
            print(f"\n{'='*60}")
            print(f"缓存审计报告")
            print(f"{'='*60}")
            print(f"缓存版本: {result['cache_version']}")
            print(f"文件总数: {result['total_files']}")
            print(f"总大小: {result['total_size_mb']:.2f} MB")
            print(f"clip污染: {result['clip_pollution']} 个文件")
            print(f"版本不匹配: {result['version_mismatch']} 条记录")
            print(f"{'='*60}")
            
            if result['details']:
                print(f"\n问题详情:")
                for detail in result['details'][:10]:
                    print(f"  - {detail}")
        
        elif action == '--clear-stale-cache':
            dry_run = '--execute' not in sys.argv
            result = clear_stale_cache(dry_run=dry_run)
            
            print(f"\n{'='*60}")
            print(f"缓存清理{'(dry-run)' if dry_run else '(已执行)'}")
            print(f"{'='*60}")
            print(f"删除文件: {result['deleted_files']}")
            print(f"释放空间: {result['deleted_size_mb']:.2f} MB")
            print(f"{'='*60}")
            
            if result['details']:
                print(f"\n删除详情:")
                for detail in result['details'][:10]:
                    print(f"  - {detail}")
        
        else:
            print(f"用法:")
            print(f"  python video_cache.py --audit-cache")
            print(f"  python video_cache.py --clear-stale-cache [--execute]")
    else:
        print(f"缓存配置:")
        print(f"  版本: {CACHE_VERSION}")
        print(f"  目标编码: {TARGET_CODEC}")
        print(f"  目标分辨率: {TARGET_WIDTH}x{TARGET_HEIGHT}")
        print(f"  目标帧率: {TARGET_FPS}fps")
        print(f"  目标像素格式: {TARGET_PIX_FMT}")
        print(f"  缓存目录: {PROCESSED_DIR}")