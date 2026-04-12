import os
import json
import uuid
import asyncio
from datetime import datetime
import sys

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from core.config import config
from pipeline import processor
from pipeline.tts_provider import generate_tts, create_subtitle_srt_from_meta
from pipeline.video_analyzer import create_video_provider, extract_frames_for_task
from core.storage import storage
from core.tos_storage import tos_storage
from pipeline.project_state import load_project_state, validate_script, validate_task, get_state_constraints, clear_cache
from pipeline.video_cache import get_or_create_processed, extract_dynamic_clip
from pipeline.processor import get_video_duration

TASKS_DIR = os.path.join(config['storage']['workdir'], 'tasks')
os.makedirs(TASKS_DIR, exist_ok=True)

def create_task(file_ids, script_text, task_description=None):
    """Create a new task
    
    【项目状态约束】
    任务创建时必须验证稿件合规性和任务方向
    依据：PROJECT_STATE.md 第六条、第十一条
    """
    # 加载项目状态（确保文件存在）
    project_state = load_project_state()
    
    # 验证稿件（禁止测试文案）
    validation = validate_script(script_text)
    if validation['decision'] == 'reject':
        raise ValueError(validation['reason'])
    
    # 验证任务方向（如果提供了描述）
    if task_description:
        task_validation = validate_task(task_description)
        if task_validation['decision'] == 'reject':
            raise ValueError(task_validation['reason'])
    
    task_id = str(uuid.uuid4())
    task = {
        'id': task_id,
        'file_ids': file_ids,
        'script': script_text,
        'task_description': task_description,
        'status': 'pending',
        'created_at': datetime.now().isoformat(),
        'updated_at': datetime.now().isoformat(),
        'progress': 0,
        'output_path': None,
        'error': None,
        'project_state_loaded': True,
        'constraints_applied': True
    }
    save_task(task)
    return task_id

def save_task(task):
    """Save task to disk"""
    task['updated_at'] = datetime.now().isoformat()
    path = os.path.join(TASKS_DIR, f"{task['id']}.json")
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(task, f, ensure_ascii=False, indent=2)

def get_task(task_id):
    """Get task by ID"""
    path = os.path.join(TASKS_DIR, f"{task_id}.json")
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None

def list_tasks():
    """List all tasks"""
    tasks = []
    if os.path.exists(TASKS_DIR):
        for f in os.listdir(TASKS_DIR):
            if f.endswith('.json'):
                path = os.path.join(TASKS_DIR, f)
                with open(path, 'r', encoding='utf-8') as file:
                    tasks.append(json.load(file))
    return sorted(tasks, key=lambda x: x.get('created_at', ''), reverse=True)

def update_task_status(task_id, status, progress=None, error=None, output_path=None):
    """Update task status"""
    task = get_task(task_id)
    if task:
        task['status'] = status
        if progress is not None:
            task['progress'] = progress
        if error is not None:
            task['error'] = error
        if output_path is not None:
            task['output_path'] = output_path
        save_task(task)
    return task

async def process_task(task_id):
    """Process a task - main pipeline
    
    【项目状态约束】
    任务执行前必须加载 PROJECT_STATE.md 进行决策判断
    依据：PROJECT_STATE.md 第十一条「执行要求」
    
    【决策机制】
    - 加载状态约束（不是打印）
    - 验证任务方向
    - 命中禁止事项 → 拒绝执行
    """
    # 加载项目状态约束（用于决策）
    constraints = get_state_constraints()
    
    task = get_task(task_id)
    if not task:
        return
    
    # 验证任务方向（从任务内容构建描述）
    task_description = task.get('task_description') or f"处理{len(task['file_ids'])}个素材，稿件:{task['script'][:50]}..."
    validation = validate_task(task_description)
    
    # 决策：拒绝执行
    if validation['decision'] == 'reject':
        update_task_status(task_id, 'rejected', error=validation['reason'])
        print(f"[项目状态] 任务 {task_id} 被拒绝：{validation['reason']}")
        return
    
    # 决策：警告但继续
    if validation['decision'] == 'warn':
        print(f"[项目状态] 警告：{validation['warning']}")
    
    # 决策：允许执行
    print(f"[项目状态] 任务 {task_id} 开始执行")
    print(f"  目标: {constraints['goal']}")
    print(f"  优先级: {constraints['priority']}")
    
    try:
        update_task_status(task_id, 'processing', progress=10)
        
        # Step 0: Upload raw materials to TOS
        print(f"\n[TOS 上传] 开始上传原始素材...")
        file_paths = {}
        for file_id in task['file_ids']:
            upload_path = storage.get_upload_path(file_id)
            if upload_path and os.path.exists(upload_path):
                file_paths[file_id] = upload_path
        
        if file_paths:
            tos_raw_result = tos_storage.upload_raw_materials(task['file_ids'], file_paths)
            
            # 更新任务记录 with raw materials TOS info
            if task:
                task['input_files'] = []
                for uploaded in tos_raw_result.get('uploaded', []):
                    file_id = uploaded['file_id']
                    local_path = file_paths.get(file_id, '')
                    task['input_files'].append({
                        'file_id': file_id,
                        'local_path': local_path,
                        'tos_key': uploaded['tos_key'],
                        'tos_url': uploaded['url'],
                        'size': uploaded['size'],
                        'uploaded': True
                    })
                
                for failed in tos_raw_result.get('failed', []):
                    task['input_files'].append({
                        'file_id': failed['file_id'],
                        'tos_key': None,
                        'error': failed['error'],
                        'uploaded': False
                    })
                
                save_task(task)
            
            if tos_raw_result['success']:
                print(f"[TOS 上传] ✅ 原始素材上传成功：{len(tos_raw_result['uploaded'])} files")
            else:
                print(f"[TOS 上传] ⚠️ 部分原始素材上传失败：{len(tos_raw_result['failed'])} files")
        else:
            print(f"[TOS 上传] ⚠️ 未找到原始素材文件")
        
        # Step 1: Collect and transcode clips
        all_clips = []
        for i, file_id in enumerate(task['file_ids']):
            upload_path = storage.get_upload_path(file_id)
            if not upload_path:
                raise Exception(f"File not found: {file_id}")
            
            # ✅ 使用 video_cache 进行转码
            processed_path = get_or_create_processed(upload_path)
            
            # ✅ 使用 extract_dynamic_clip 进行切片
            duration = get_video_duration(processed_path)
            clip_duration = 5
            j = 0
            while j * clip_duration < duration:
                start = j * clip_duration
                clip = extract_dynamic_clip(processed_path, start, clip_duration, workdir=storage.workdir, task_id=task_id, clip_id=j)
                if clip:
                    all_clips.append(clip)
                j += 1
            update_task_status(task_id, 'processing', progress=20 + i * 10)
        
        update_task_status(task_id, 'processing', progress=50)
        
        # Step 2: Video analysis (L5)
        video_provider = create_video_provider()
        
        # Analyze sources
        sources_analysis = []
        for i, file_id in enumerate(task['file_ids']):
            upload_path = storage.get_upload_path(file_id)
            if upload_path:
                source_info = video_provider.analyze_source(upload_path)
                sources_analysis.append(source_info)
        
        # Extract frames for all clips
        frame_mapping = extract_frames_for_task(task_id, all_clips)
        
        # Analyze clips
        clips_analysis = []
        for clip in all_clips:
            clip_path = clip.get('path', '')
            frame_path = frame_mapping.get(clip_path)
            clip_info = video_provider.analyze_clip(clip_path, frame_path)
            clips_analysis.append(clip_info)
        
        # Save analysis.json
        analysis_path = os.path.join(storage.workdir, f"{task_id}_analysis.json")
        analysis_result = {
            'task_id': task_id,
            'provider': {
                'name': video_provider.provider_name,
                'enabled_model_analysis': video_provider.enabled_model_analysis
            },
            'sources': sources_analysis,
            'clips': clips_analysis,
            'analyzed_at': datetime.now().isoformat()
        }
        with open(analysis_path, 'w', encoding='utf-8') as f:
            json.dump(analysis_result, f, ensure_ascii=False, indent=2)
        
        update_task_status(task_id, 'processing', progress=60)
        
        # Step 3: Select clips for target duration
        target_duration = config['video']['target_duration_sec']
        
        # ✅ 修复：正确分组 clip 按源素材
        # 根据 clip 路径中的素材哈希来分组
        source_clips = {}
        for clip in all_clips:
            # 从 clip 路径中提取素材标识（哈希部分）
            clip_path = clip.get('path', '')
            # 例如：394A0108__8f355bf4__h264__1280x720__25fps__yuv420p__v1_dynamic_0_start0.0s_dur5.0s.mp4
            source_hash = clip_path.split('__')[1] if '__' in clip_path else 'unknown'
            if source_hash not in source_clips:
                source_clips[source_hash] = []
            source_clips[source_hash].append(clip)
        
        print(f"\n[Clip 选择] 素材分组：{len(source_clips)} 个素材")
        for src_hash, clips in source_clips.items():
            print(f"  {src_hash}: {len(clips)} 个 clips")
        
        # 计算需要的总 clip 数（基于目标时长，TTS 尚未生成）
        # 使用 target_duration 作为基准（配置中定义的期望时长）
        total_clips_needed = max(3, int(target_duration / 5))
        
        # 平均分配每个素材的 clip 数
        clips_per_source = max(1, total_clips_needed // max(1, len(source_clips)))
        
        # 从每个素材选择 clip
        selected_clips = []
        for source_hash, clips in source_clips.items():
            selected_clips.extend(clips[:clips_per_source])
        
        # 按 start 时间排序，确保连续性
        selected_clips.sort(key=lambda c: c.get('start', 0))
        
        print(f"\n[Clip 选择] 已选择 {len(selected_clips)} 个 clips，总时长约 {sum(c.get('duration',0) for c in selected_clips):.1f}s")
        for i, clip in enumerate(selected_clips):
            print(f"  Clip{i+1}: {os.path.basename(clip['path'])[:50]}... ({clip['duration']}s)")
        
        # Save timeline.json for audit
        timeline_path = os.path.join(storage.workdir, f"{task_id}_timeline.json")
        timeline_data = {
            'task_id': task_id,
            'target_duration': target_duration,
            'selected_clips': [
                {
                    'clip_path': clip['path'],
                    'source_file_id': task['file_ids'][i] if f'_transcoded_{i}.' in clip['path'] else 'unknown',
                    'source_index': next((i for i, fid in enumerate(task['file_ids']) if f'_transcoded_{i}.' in clip['path']), -1),
                    'start_time': clip['start'],
                    'duration': clip['duration'],
                    'end_time': clip['start'] + clip['duration'],
                    'selected_reason': 'distributed_selection'
                }
                for clip in selected_clips
            ],
            'total_clips_selected': len(selected_clips),
            'sources_used': list(source_clips.keys()),
            'created_at': datetime.now().isoformat()
        }
        with open(timeline_path, 'w', encoding='utf-8') as f:
            json.dump(timeline_data, f, ensure_ascii=False, indent=2)
        
        # Step 3: Generate TTS from script (with metadata)
        tts_path = os.path.join(storage.workdir, f"{task_id}_tts.mp3")
        tts_meta_path = os.path.join(storage.workdir, f"{task_id}_tts_meta.json")
        tts_meta = generate_tts(task['script'], tts_path, tts_meta_path)
        update_task_status(task_id, 'processing', progress=70)
        
        # Step 4: Create subtitles from TTS metadata
        srt_path = os.path.join(storage.workdir, f"{task_id}.srt")
        create_subtitle_srt_from_meta(tts_meta, srt_path)
        update_task_status(task_id, 'processing', progress=80)
        
        # Step 5: Assemble final video (keep concat for audit)
        output_path = storage.get_output_path(task_id)
        processor.assemble_video(selected_clips, tts_path, srt_path, output_path, target_duration, keep_concat=True)
        
        # Step 6: Upload evidence package to TOS
        print(f"\n[TOS 上传] 开始上传任务证据包...")
        evidence_files = {
            'task_json': os.path.join(TASKS_DIR, f"{task_id}.json"),
            'script': os.path.join(storage.workdir, f"{task_id}_script.txt"),
            'tts': tts_path,
            'srt': srt_path,
            'timeline': os.path.join(storage.workdir, f"{task_id}_timeline.json"),
            'output': output_path
        }
        
        # 保存 script 到单独文件
        script_path = evidence_files['script']
        with open(script_path, 'w', encoding='utf-8') as f:
            f.write(task['script'])
        
        tos_result = tos_storage.upload_task_evidence(task_id, evidence_files)
        
        # 更新任务记录 with TOS info
        task = get_task(task_id)
        if task:
            task['tos'] = {
                'uploaded': tos_result['uploaded'],
                'failed': tos_result['failed'],
                'urls': tos_result['urls'],
                'upload_time': datetime.now().isoformat(),
                'success': tos_result['success']
            }
            
            # 添加 output_tos_key 和 output_url
            if tos_result['success'] and 'output' in tos_result['urls']:
                task['output_tos_key'] = f'tasks/{task_id}/output.mp4'
                task['output_url'] = tos_result['urls']['output']
                task['tos_verified'] = True
            
            save_task(task)
        
        if tos_result['success']:
            print(f"[TOS 上传] ✅ 证据包上传成功：{len(tos_result['uploaded'])} files")
            for key in tos_result['uploaded']:
                print(f"   - {key}")
        else:
            print(f"[TOS 上传] ⚠️ 部分上传失败：{len(tos_result['failed'])} files")
            for key, error in tos_result['failed']:
                print(f"   - {key}: {error}")
        
        update_task_status(task_id, 'completed', progress=100, output_path=output_path)
        
    except Exception as e:
        update_task_status(task_id, 'failed', error=str(e))
        print(f"[TOS 上传] ❌ 任务执行失败：{e}")
