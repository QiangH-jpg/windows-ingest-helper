"""
视频理解层 (L5) Provider 抽象
支持多种视频分析服务接入，当前默认使用本地保底分析
"""
import os
import subprocess
import json
import math
from typing import List, Dict, Any, Optional
from datetime import datetime

from core.config import config

VIDEO = config['video']


def get_video_info(path: str) -> Dict[str, Any]:
    """获取视频基础信息（ffprobe）"""
    if not os.path.exists(path):
        return {'error': f'File not found: {path}'}
    
    ffprobe_path = VIDEO['ffmpeg_path'].replace('ffmpeg', 'ffprobe')
    cmd = [
        ffprobe_path, '-v', 'error',
        '-show_entries', 'stream=codec_type,codec_name,width,height,r_frame_rate,duration',
        '-show_entries', 'format=format_name,duration,size',
        '-of', 'json',
        path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        info = json.loads(result.stdout)
    except:
        return {'error': f'ffprobe parse failed: {result.stderr}'}
    
    video_stream = None
    audio_stream = None
    for stream in info.get('streams', []):
        if stream.get('codec_type') == 'video':
            video_stream = stream
        elif stream.get('codec_type') == 'audio':
            audio_stream = stream
    
    format_info = info.get('format', {})
    
    # Parse fps
    fps = 0.0
    if video_stream and 'r_frame_rate' in video_stream:
        try:
            num, den = map(int, video_stream['r_frame_rate'].split('/'))
            fps = num / den if den > 0 else 0.0
        except:
            pass
    
    return {
        'path': path,
        'duration': float(video_stream.get('duration', 0) or format_info.get('duration', 0)),
        'width': video_stream.get('width', 0),
        'height': video_stream.get('height', 0),
        'fps': fps,
        'has_audio': audio_stream is not None,
        'file_size': int(format_info.get('size', 0)),
        'codec': video_stream.get('codec_name', 'unknown') if video_stream else 'unknown'
    }


def extract_frame(video_path: str, output_path: str, position: str = '00:00:01') -> bool:
    """
    抽取单帧
    
    Args:
        video_path: 输入视频路径
        output_path: 输出帧路径
        position: 抽取位置 (HH:MM:SS 或秒数)
    
    Returns:
        是否成功
    """
    if not os.path.exists(video_path):
        return False
    
    cmd = [
        VIDEO['ffmpeg_path'], '-y',
        '-ss', str(position),
        '-i', video_path,
        '-vframes', '1',
        '-q:v', '2',
        output_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return os.path.exists(output_path) and os.path.getsize(output_path) > 0


def calculate_brightness(image_path: str) -> float:
    """
    计算图像亮度分数 (0-100)
    简化版本：返回中间值
    """
    if not os.path.exists(image_path):
        return 50.0
    
    # 简化：返回中间值（实际应该用 ffmpeg 计算）
    return 50.0


def calculate_sharpness(image_path: str) -> float:
    """
    计算图像清晰度分数 (0-100)
    使用拉普拉斯方差法估算
    简化版本：基于视频编码参数估算
    """
    if not os.path.exists(image_path):
        return 50.0
    
    # 获取视频信息来估算
    info = get_video_info(image_path)
    if 'error' in info:
        return 50.0
    
    # 简化算法：基于分辨率和码率估算
    # 实际应该用图像处理库计算拉普拉斯方差
    width = info.get('width', 0)
    height = info.get('height', 0)
    
    if width == 0 or height == 0:
        return 50.0
    
    # 分辨率分数 (基于 640x360 基准)
    resolution_score = min(100, ((width * height) / (640 * 360)) * 50)
    
    # 保底返回
    return min(100, 50 + resolution_score / 2)


def calculate_motion_score(video_path: str) -> float:
    """
    计算运动强度分数 (0-100)
    简化版本：返回中间值
    """
    if not os.path.exists(video_path):
        return 50.0
    
    # 简化：返回中间值
    return 50.0


class VideoAnalysisProvider:
    """视频分析服务提供者基类"""
    
    def __init__(self, provider_name: str, enabled_model_analysis: bool = False):
        self.provider_name = provider_name
        self.enabled_model_analysis = enabled_model_analysis
    
    def analyze_source(self, source_path: str) -> Dict[str, Any]:
        """分析源视频"""
        raise NotImplementedError
    
    def analyze_clip(self, clip_path: str, frame_path: str = None) -> Dict[str, Any]:
        """分析视频片段"""
        raise NotImplementedError


class LocalBasicProvider(VideoAnalysisProvider):
    """本地基础分析实现（保底方案）"""
    
    def __init__(self):
        super().__init__(provider_name='local_basic', enabled_model_analysis=False)
    
    def analyze_source(self, source_path: str) -> Dict[str, Any]:
        """分析源视频（本地保底）"""
        info = get_video_info(source_path)
        
        if 'error' in info:
            return {
                'source_path': source_path,
                'error': info['error'],
                'analyzed': False
            }
        
        return {
            'source_path': source_path,
            'duration': info.get('duration', 0),
            'width': info.get('width', 0),
            'height': info.get('height', 0),
            'fps': info.get('fps', 0),
            'has_audio': info.get('has_audio', False),
            'file_size': info.get('file_size', 0),
            'codec': info.get('codec', 'unknown'),
            'analyzed': True,
            'provider': self.provider_name
        }
    
    def analyze_clip(self, clip_path: str, frame_path: str = None) -> Dict[str, Any]:
        """分析视频片段（本地保底）"""
        info = get_video_info(clip_path)
        
        if 'error' in info:
            return {
                'clip_path': clip_path,
                'error': info['error'],
                'analyzed': False
            }
        
        # 计算基础质量分数
        brightness = 50.0  # 简化：返回中间值
        sharpness = 50.0   # 简化：返回中间值
        
        # 如果有帧文件，可以尝试计算
        if frame_path and os.path.exists(frame_path):
            brightness = calculate_brightness(frame_path)
            sharpness = calculate_sharpness(frame_path)
        
        return {
            'clip_path': clip_path,
            'frame_path': frame_path,
            'duration': info.get('duration', 0),
            'width': info.get('width', 0),
            'height': info.get('height', 0),
            'fps': info.get('fps', 0),
            'has_audio': info.get('has_audio', False),
            'file_size': info.get('file_size', 0),
            'metrics': {
                'brightness_score': round(brightness, 2),
                'sharpness_score': round(sharpness, 2),
                'motion_like_score': 50.0  # 保底值
            },
            'score': round((brightness + sharpness + 50) / 3, 2),
            'analyzed': True,
            'provider': self.provider_name
        }


def create_video_provider() -> VideoAnalysisProvider:
    """根据配置创建视频分析提供者实例"""
    # 当前默认使用本地保底分析
    return LocalBasicProvider()


def extract_frames_for_task(task_id: str, clips: List[Dict[str, Any]], output_dir: str = None) -> Dict[str, str]:
    """
    为任务的每个 clip 抽取代表帧
    
    Args:
        task_id: 任务 ID
        clips: clip 列表（包含 path 字段）
        output_dir: 输出目录（可选）
    
    Returns:
        clip_path -> frame_path 映射
    """
    if output_dir is None:
        output_dir = os.path.join(config['storage']['workdir'], 'frames', task_id)
    
    os.makedirs(output_dir, exist_ok=True)
    
    frame_mapping = {}
    
    for clip in clips:
        clip_path = clip.get('path', '')
        if not clip_path or not os.path.exists(clip_path):
            continue
        
        # 从 clip 路径提取索引信息
        clip_name = os.path.basename(clip_path)
        frame_name = clip_name.replace('.mp4', '.jpg')
        frame_path = os.path.join(output_dir, frame_name)
        
        # 抽取中间帧
        duration = clip.get('duration', 5.0)
        position = duration / 2  # 抽取中间位置
        
        success = extract_frame(clip_path, frame_path, position=str(position))
        
        if success:
            frame_mapping[clip_path] = frame_path
        else:
            # 失败时记录空路径
            frame_mapping[clip_path] = None
    
    return frame_mapping
