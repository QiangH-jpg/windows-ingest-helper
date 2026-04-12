#!/usr/bin/env python3
"""
本地清理策略执行脚本

清理规则：
1. 任务完成后，临时文件保留 24 小时
2. 失败任务证据保留 7 天
3. clip 切片立即清理（已完成上传后）
4. concat 清单立即清理
5. 热缓存（processed_videos）保留最近 500MB

执行方式：
- 手动：python cleanup_local.py
- 定时：0 2 * * * cd /path && /venv/bin/python cleanup_local.py
"""
import os
import sys
import json
import shutil
import logging
from datetime import datetime, timedelta
from pathlib import Path

# Add project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('cleanup')

WORKDIR = config['storage']['workdir']
OUTPUTS_DIR = config['storage']['outputs_dir']
PROCESSED_DIR = os.path.join(os.path.dirname(WORKDIR), 'processed_videos')

# 配置阈值
MAX_PROCESSED_SIZE_MB = 500  # 热缓存最大 500MB
TEMP_RETENTION_HOURS = 24    # 临时文件保留 24 小时
FAILED_RETENTION_DAYS = 7    # 失败任务保留 7 天

def get_task_status(task_file: Path) -> str:
    """获取任务状态"""
    try:
        with open(task_file, 'r') as f:
            task = json.load(f)
        return task.get('status', 'unknown')
    except:
        return 'unknown'

def is_tos_verified(task_file: Path) -> bool:
    """检查任务是否已验证 TOS 上传"""
    try:
        with open(task_file, 'r') as f:
            task = json.load(f)
        return task.get('tos_verified', False) or task.get('tos', {}).get('success', False)
    except:
        return False

def cleanup_task_temp(task_id: str, dry_run: bool = False) -> dict:
    """
    清理任务临时文件
    
    清理对象：
    - clips/ 目录
    - frames/ 目录
    - concat.txt
    - 临时 tts/srt 文件（保留证据包后）
    """
    task_dir = Path(WORKDIR) / task_id
    cleaned = []
    skipped = []
    
    if not task_dir.exists():
        return {'cleaned': cleaned, 'skipped': skipped}
    
    # 清理 clips
    clips_dir = task_dir / 'clips'
    if clips_dir.exists():
        if dry_run:
            cleaned.append(f"[DRY_RUN] Would remove {clips_dir}")
        else:
            shutil.rmtree(clips_dir)
            cleaned.append(str(clips_dir))
    
    # 清理 frames
    frames_dir = Path(WORKDIR) / 'frames' / task_id
    if frames_dir.exists():
        if dry_run:
            cleaned.append(f"[DRY_RUN] Would remove {frames_dir}")
        else:
            shutil.rmtree(frames_dir)
            cleaned.append(str(frames_dir))
    
    # 清理 concat.txt
    concat_file = task_dir / f"{task_id}.concat.txt"
    if concat_file.exists():
        if dry_run:
            cleaned.append(f"[DRY_RUN] Would remove {concat_file}")
        else:
            concat_file.unlink()
            cleaned.append(str(concat_file))
    
    return {'cleaned': cleaned, 'skipped': skipped}

def cleanup_old_temp_tasks(dry_run: bool = False) -> dict:
    """清理超过保留期的临时任务"""
    cutoff = datetime.now() - timedelta(hours=TEMP_RETENTION_HOURS)
    cleaned = []
    
    # 扫描 workdir 中的任务目录
    for task_dir in Path(WORKDIR).iterdir():
        if not task_dir.is_dir():
            continue
        
        task_id = task_dir.name
        if len(task_id) != 36:  # 不是 UUID 目录
            continue
        
        # 检查目录修改时间
        try:
            mtime = datetime.fromtimestamp(task_dir.stat().st_mtime)
            if mtime < cutoff:
                # 检查是否已完成且 TOS 已验证
                task_file = Path(WORKDIR) / 'tasks' / f"{task_id}.json"
                if task_file.exists() and is_tos_verified(task_file):
                    result = cleanup_task_temp(task_id, dry_run)
                    cleaned.extend(result['cleaned'])
                    logger.info(f"清理过期临时文件：{task_id}")
        except Exception as e:
            logger.error(f"清理任务 {task_id} 失败：{e}")
    
    return {'cleaned': cleaned}

def cleanup_processed_cache(dry_run: bool = False) -> dict:
    """
    清理转码缓存（LRU 策略）
    
    保留策略：
    - 总大小不超过 MAX_PROCESSED_SIZE_MB
    - 超出时删除最旧的文件
    """
    if not os.path.exists(PROCESSED_DIR):
        return {'cleaned': [], 'freed_mb': 0}
    
    # 获取所有缓存文件及其时间
    files = []
    total_size = 0
    
    for f in Path(PROCESSED_DIR).glob('*.mp4'):
        if f.is_file():
            stat = f.stat()
            files.append({
                'path': str(f),
                'size': stat.st_size,
                'mtime': datetime.fromtimestamp(stat.st_mtime)
            })
            total_size += stat.st_size
    
    total_mb = total_size / 1024 / 1024
    cleaned = []
    freed = 0
    
    # 如果超出阈值，按 LRU 删除
    if total_mb > MAX_PROCESSED_SIZE_MB:
        # 按修改时间排序（最旧在前）
        files.sort(key=lambda x: x['mtime'])
        
        for f in files:
            if total_mb <= MAX_PROCESSED_SIZE_MB:
                break
            
            if dry_run:
                cleaned.append(f"[DRY_RUN] Would remove {f['path']}")
            else:
                os.remove(f['path'])
                cleaned.append(f['path'])
            
            freed_mb = f['size'] / 1024 / 1024
            total_mb -= freed_mb
            freed += freed_mb
            logger.info(f"LRU 清理缓存：{f['path']} ({freed_mb:.1f} MB)")
    
    return {'cleaned': cleaned, 'freed_mb': freed, 'total_mb': total_mb}

def cleanup_failed_tasks(dry_run: bool = False) -> dict:
    """
    清理失败任务（保留 FAILED_RETENTION_DAYS 天）
    
    失败任务保留用于调试，超过保留期后清理
    """
    cutoff = datetime.now() - timedelta(days=FAILED_RETENTION_DAYS)
    cleaned = []
    
    tasks_dir = Path(WORKDIR) / 'tasks'
    if not tasks_dir.exists():
        return {'cleaned': cleaned}
    
    for task_file in tasks_dir.glob('*.json'):
        try:
            with open(task_file, 'r') as f:
                task = json.load(f)
            
            if task.get('status') == 'failed':
                # 检查创建时间
                created_at = task.get('created_at', '')
                if created_at:
                    try:
                        created_time = datetime.fromisoformat(created_at)
                        if created_time < cutoff:
                            # 可以清理
                            task_id = task.get('id', task_file.stem)
                            
                            # 清理任务目录
                            task_dir = Path(WORKDIR) / task_id
                            if task_dir.exists():
                                if dry_run:
                                    cleaned.append(f"[DRY_RUN] Would remove {task_dir}")
                                else:
                                    shutil.rmtree(task_dir)
                                    cleaned.append(str(task_dir))
                            
                            # 清理任务记录
                            if not dry_run:
                                task_file.unlink()
                                cleaned.append(str(task_file))
                            
                            logger.info(f"清理过期失败任务：{task_id}")
                    except:
                        pass
        except Exception as e:
            logger.error(f"检查失败任务 {task_file} 出错：{e}")
    
    return {'cleaned': cleaned}

def main():
    """主清理入口"""
    import argparse
    
    parser = argparse.ArgumentParser(description='本地清理策略执行')
    parser.add_argument('--dry-run', action='store_true', help='仅显示将清理的内容')
    parser.add_argument('--temp', action='store_true', help='仅清理临时文件')
    parser.add_argument('--cache', action='store_true', help='仅清理转码缓存')
    parser.add_argument('--failed', action='store_true', help='仅清理失败任务')
    args = parser.parse_args()
    
    logger.info("="*60)
    logger.info("本地清理策略执行")
    logger.info("="*60)
    
    if args.dry_run:
        logger.info("⚠️  模式：DRY RUN（仅显示，不实际删除）")
    
    results = {}
    
    # 清理过期临时任务
    if not args.cache and not args.failed:
        logger.info("\n[1/3] 清理过期临时文件...")
        results['temp'] = cleanup_old_temp_tasks(args.dry_run)
        logger.info(f"  清理：{len(results['temp']['cleaned'])} 项")
    
    # 清理转码缓存
    if not args.temp and not args.failed:
        logger.info("\n[2/3] 清理转码缓存（LRU）...")
        results['cache'] = cleanup_processed_cache(args.dry_run)
        logger.info(f"  清理：{len(results['cache']['cleaned'])} 项，释放 {results['cache'].get('freed_mb', 0):.1f} MB")
    
    # 清理失败任务
    if not args.temp and not args.cache:
        logger.info("\n[3/3] 清理过期失败任务...")
        results['failed'] = cleanup_failed_tasks(args.dry_run)
        logger.info(f"  清理：{len(results['failed']['cleaned'])} 项")
    
    logger.info("\n" + "="*60)
    logger.info("清理完成")
    logger.info("="*60)
    
    return results

if __name__ == '__main__':
    main()
