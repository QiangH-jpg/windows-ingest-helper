#!/usr/bin/env python3
"""
TOS 存储模块 - 视频项目生产链接入

职责：
1. 原始素材上传 TOS
2. 任务证据包上传 TOS
3. 写回任务记录 TOS 字段
4. 本地清理策略

TOS 目录规范：
/raw/{date}/{task_id}/{filename}       - 原始素材
/tasks/{task_id}/task.json             - 任务元数据
/tasks/{task_id}/script.txt            - 新闻稿
/tasks/{task_id}/tts.mp3               - TTS 音频
/tasks/{task_id}/subtitles.srt         - 字幕文件
/tasks/{task_id}/timeline.json         - 选片清单
/tasks/{task_id}/output.mp4            - 输出视频
/audit/{date}/{task_id}/{filename}     - 审计证据
"""
import os
import json
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List

import tos
from tos import TosClientV2, HttpMethodType

logger = logging.getLogger(__name__)

# TOS 配置
TOS_AK = os.getenv('TOS_AK', 'AKLTZGIxYjcxZWY2NzIyNDIxYjhiOGZjMTYzY2E4OGQxYzE')
TOS_SK = os.getenv('TOS_SK', 'T0RFMVlUSXdZbUl3WVdVMU5HVTBPV0kyTURWak5UYzROemd5TkdWaU9UWQ==')
TOS_BUCKET = os.getenv('TOS_BUCKET', 'e23-video')
TOS_REGION = os.getenv('TOS_REGION', 'cn-beijing')

class TOSStorage:
    """TOS 存储服务"""
    
    def __init__(self):
        self.bucket = TOS_BUCKET
        self.region = TOS_REGION
        self.endpoint = f"tos-{TOS_REGION}.volces.com"
        self.client = TosClientV2(
            ak=TOS_AK,
            sk=TOS_SK,
            endpoint=self.endpoint,
            region=TOS_REGION,
        )
        logger.info(f"TOS 客户端初始化成功：bucket={TOS_BUCKET}, region={TOS_REGION}")
    
    def get_file_hash(self, file_path: str) -> str:
        """计算文件 MD5 哈希（前 10MB）"""
        hasher = hashlib.md5()
        with open(file_path, 'rb') as f:
            chunk = f.read(10 * 1024 * 1024)
            hasher.update(chunk)
        return hasher.hexdigest()[:12]
    
    def upload_file(self, local_path: str, tos_key: str, verify: bool = True) -> Dict:
        """
        上传文件到 TOS
        
        Args:
            local_path: 本地文件路径
            tos_key: TOS 对象键
            verify: 是否进行上传后校验
        
        Returns:
            {
                'success': bool,
                'tos_key': str,
                'url': str,
                'size': int,
                'local_size': int,
                'etag': str,
                'verified': bool,
                'error': str (optional)
            }
        """
        if not os.path.exists(local_path):
            return {'success': False, 'error': f'File not found: {local_path}'}
        
        local_size = os.path.getsize(local_path)
        
        try:
            # 上传文件
            result = self.client.put_object_from_file(
                bucket=self.bucket,
                key=tos_key,
                file_path=local_path
            )
            
            # 生成预签名 URL（7 天有效）
            url_result = self.client.pre_signed_url(
                http_method=HttpMethodType.Http_Method_Get,
                bucket=self.bucket,
                key=tos_key,
                expires=604800  # 7 天
            )
            
            logger.info(f"TOS 上传成功：{tos_key} ({local_size} bytes)")
            
            response = {
                'success': True,
                'tos_key': tos_key,
                'url': url_result.signed_url,
                'size': local_size,
                'local_size': local_size,
                'etag': result.etag,
                'verified': False
            }
            
            # 上传后校验（等待 1 秒让 TOS 同步）
            if verify:
                import time
                time.sleep(1)  # 等待 TOS 数据同步
                verified = self.verify_upload(tos_key, local_size)
                response['verified'] = verified
                if not verified:
                    logger.warning(f"TOS 上传校验失败：{tos_key}")
                    response['success'] = False
                    response['error'] = 'Upload verification failed'
            
            return response
            
        except tos.exceptions.TosServerError as e:
            logger.error(f"TOS 上传失败：{tos_key} - {e}")
            return {'success': False, 'error': str(e)}
        except Exception as e:
            logger.error(f"TOS 上传异常：{tos_key} - {e}")
            return {'success': False, 'error': str(e)}
    
    def upload_task_evidence(self, task_id: str, evidence_files: Dict[str, str]) -> Dict:
        """
        上传任务证据包到 TOS
        
        Args:
            task_id: 任务 ID
            evidence_files: {
                'task_json': '/path/to/task.json',
                'script': '/path/to/script.txt',
                'tts': '/path/to/tts.mp3',
                'srt': '/path/to/subtitles.srt',
                'timeline': '/path/to/timeline.json',
                'output': '/path/to/output.mp4'
            }
        
        Returns:
            {
                'success': bool,
                'uploaded': [tos_keys],
                'failed': [(tos_key, error)],
                'urls': {type: url}
            }
        """
        uploaded = []
        failed = []
        urls = {}
        
        # 最小可追溯证据包映射
        evidence_mapping = {
            'task_json': f'tasks/{task_id}/task.json',
            'script': f'tasks/{task_id}/script.txt',
            'tts': f'tasks/{task_id}/tts.mp3',
            'srt': f'tasks/{task_id}/subtitles.srt',
            'timeline': f'tasks/{task_id}/timeline.json',
            'output': f'tasks/{task_id}/output.mp4'
        }
        
        for file_type, local_path in evidence_files.items():
            if not local_path or not os.path.exists(local_path):
                logger.warning(f"证据文件不存在：{file_type} - {local_path}")
                failed.append((file_type, 'File not found'))
                continue
            
            tos_key = evidence_mapping.get(file_type)
            if not tos_key:
                logger.warning(f"未知证据类型：{file_type}")
                continue
            
            result = self.upload_file(local_path, tos_key)
            
            if result['success']:
                uploaded.append(tos_key)
                urls[file_type] = result['url']
                logger.info(f"证据包上传成功：{file_type} -> {tos_key}")
            else:
                failed.append((tos_key, result.get('error', 'Unknown error')))
                logger.error(f"证据包上传失败：{file_type} -> {tos_key}: {result.get('error')}")
        
        return {
            'success': len(failed) == 0,
            'uploaded': uploaded,
            'failed': failed,
            'urls': urls
        }
    
    def upload_raw_material(self, file_id: str, local_path: str, original_filename: str) -> Dict:
        """
        上传原始素材到 TOS
        
        Args:
            file_id: 文件 ID（UUID）
            local_path: 本地文件路径
            original_filename: 原始文件名
        
        Returns:
            {
                'success': bool,
                'tos_key': str,
                'url': str,
                'size': int
            }
        """
        date_str = datetime.now().strftime('%Y%m%d')
        ext = Path(original_filename).suffix or '.mp4'
        tos_key = f'raw/{date_str}/{file_id}{ext}'
        
        return self.upload_file(local_path, tos_key)
    
    def verify_upload(self, tos_key: str, expected_size: int = None) -> bool:
        """
        验证 TOS 对象上传成功
        
        校验规则：
        1. 对象存在（head_object 不抛异常）
        2. 文件大小一致（如果提供 expected_size）
        
        Args:
            tos_key: TOS 对象键
            expected_size: 预期文件大小（可选）
        
        Returns:
            True if verified, False otherwise
        """
        try:
            response = self.client.head_object(bucket=self.bucket, key=tos_key)
            
            # 校验 1: 对象存在（不抛异常即存在）
            # TOS SDK v2 返回 HeadObjectOutput 对象
            
            # 校验 2: 文件大小一致
            if expected_size is not None:
                tos_size = response.content_length
                if tos_size != expected_size:
                    logger.error(f"TOS 校验失败：{tos_key} - 大小不一致 (TOS: {tos_size}, 本地：{expected_size})")
                    return False
            
            logger.info(f"TOS 校验成功：{tos_key} (size={response.content_length})")
            return True
            
        except tos.exceptions.TosServerError as e:
            if e.status_code == 404:
                logger.error(f"TOS 校验失败：{tos_key} - 对象不存在")
            else:
                logger.error(f"TOS 校验失败：{tos_key} - {e}")
            return False
        except Exception as e:
            logger.error(f"TOS 校验异常：{tos_key} - {e}")
            return False
    
    def upload_raw_materials(self, file_ids: List[str], file_paths: Dict[str, str]) -> Dict:
        """
        批量上传原始素材到 TOS
        
        Args:
            file_ids: 文件 ID 列表
            file_paths: {file_id: local_path}
        
        Returns:
            {
                'success': bool,
                'uploaded': [{'file_id': str, 'tos_key': str, 'size': int}],
                'failed': [{'file_id': str, 'error': str}]
            }
        """
        uploaded = []
        failed = []
        
        for file_id, local_path in file_paths.items():
            if not os.path.exists(local_path):
                failed.append({'file_id': file_id, 'error': 'File not found'})
                continue
            
            # 生成 TOS key: raw/{date}/{file_id}.{ext}
            date_str = datetime.now().strftime('%Y%m%d')
            ext = Path(local_path).suffix or '.mp4'
            tos_key = f'raw/{date_str}/{file_id}{ext}'
            
            result = self.upload_file(local_path, tos_key, verify=True)
            
            if result['success']:
                uploaded.append({
                    'file_id': file_id,
                    'tos_key': tos_key,
                    'size': result['size'],
                    'url': result['url']
                })
                logger.info(f"原始素材上传成功：{file_id} -> {tos_key}")
            else:
                failed.append({
                    'file_id': file_id,
                    'tos_key': tos_key,
                    'error': result.get('error', 'Unknown error')
                })
                logger.error(f"原始素材上传失败：{file_id} -> {tos_key}: {result.get('error')}")
        
        return {
            'success': len(failed) == 0,
            'uploaded': uploaded,
            'failed': failed
        }
    
    def generate_url(self, tos_key: str, expires: int = 604800) -> str:
        """
        生成预签名 URL
        
        Args:
            tos_key: TOS 对象键
            expires: 有效期（秒），默认 7 天
        
        Returns:
            signed URL string
        """
        try:
            result = self.client.pre_signed_url(
                http_method=HttpMethodType.Http_Method_Get,
                bucket=self.bucket,
                key=tos_key,
                expires=expires
            )
            return result.signed_url
        except Exception as e:
            logger.error(f"生成 URL 失败：{tos_key} - {e}")
            return ''


# 全局单例
tos_storage = TOSStorage()
