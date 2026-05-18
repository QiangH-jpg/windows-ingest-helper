import os
import sys
import json
import glob
import re
import asyncio
import threading
from datetime import datetime
from flask import Flask, request, jsonify, render_template, send_file, send_from_directory, redirect
from flask_cors import CORS
from dotenv import load_dotenv

# Load .env before any other imports
_dotenv_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', '.env')
load_dotenv(_dotenv_path)

# ============================================================
# v12.7: 访问口令 + 并发锁 + 原子写入 + task_token
# ============================================================
import secrets
import time as _time_mod
import hashlib as _hl

VIDEO_TOOL_ACCESS_CODE = os.environ.get('VIDEO_TOOL_ACCESS_CODE', '')
if not VIDEO_TOOL_ACCESS_CODE:
    print("[v12.7] VIDEO_TOOL_ACCESS_CODE not set, auth disabled")
else:
    print(f"[v12.7] Access code configured (len={len(VIDEO_TOOL_ACCESS_CODE)})")

def _make_access_cookie():
    ts = str(_time_mod.time())
    h = _hl.sha256(f'{VIDEO_TOOL_ACCESS_CODE}:{ts}'.encode()).hexdigest()[:16]
    return f'{ts}:{h}'

def _check_access_cookie():
    if not VIDEO_TOOL_ACCESS_CODE:
        return True
    cv = request.cookies.get('vt_access', '')
    if not cv or ':' not in cv:
        return False
    ts, hv = cv.split(':', 1)
    try:
        t = float(ts)
    except ValueError:
        return False
    if _time_mod.time() - t > 8 * 3600:
        return False
    exp = _hl.sha256(f'{VIDEO_TOOL_ACCESS_CODE}:{ts}'.encode()).hexdigest()[:16]
    return hv == exp

def _verify_task_token(task_id, token):
    if not token:
        return True
    tp = os.path.join(TASKS_DIR, f"{task_id}.json")
    if not os.path.exists(tp):
        return True
    try:
        with open(tp, 'r', encoding='utf-8') as f:
            td = json.load(f)
        st = td.get('task_token', '')
        return not (st and st != token)
    except Exception:
        return True

def _token_guard(task_id, token):
    if not _verify_task_token(task_id, token):
        return jsonify({'error': 'Forbidden: invalid token', 'code': 403}), 403
    return None

# 并发锁
VIDEO_TOOL_MAX_CONCURRENT = int(os.environ.get('VIDEO_TOOL_MAX_CONCURRENT_GENERATIONS', '4'))
_gen_count = 0
_gen_lock = threading.Lock()
_task_locks = {}
_task_locks_mu = threading.Lock()

def _try_global_slot():
    global _gen_count
    with _gen_lock:
        if _gen_count >= VIDEO_TOOL_MAX_CONCURRENT:
            return False
        _gen_count += 1
        return True

def _free_global_slot():
    global _gen_count
    with _gen_lock:
        _gen_count = max(0, _gen_count - 1)

def _get_task_lock(tid):
    with _task_locks_mu:
        if tid not in _task_locks:
            _task_locks[tid] = threading.Lock()
        return _task_locks[tid]

def _atomic_json(path, data):
    tmp = path + '.tmp.' + str(os.getpid())
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

# v12.9 P0: 用户级错误提示映射（技术错误 → 用户可读消息）
_ERROR_USER_MESSAGES = {
    # L3 / model 相关
    'runtime_error': 'AI 模型暂时繁忙，系统正在自动重试。',
    # L3 retry 全部失败
    'l3_all_failed': 'AI 生成暂时失败，任务已保留，请稍后重试。',
    # 渲染/时长问题
    'render_timeline_duration_mismatch': '视频渲染参数异常，请检查任务配置后重试。',
    # 通用异常
    'exception': 'AI 生成暂时失败，任务已保留，请稍后重试。',
    # 未知
    'unknown': '生成出现异常，请稍后重试。',
}

def _get_user_message(error_type: str, error_msg: str) -> str:
    """根据错误类型返回用户友好的提示"""
    msg = _ERROR_USER_MESSAGES.get(error_type, _ERROR_USER_MESSAGES['unknown'])
    # L3 相关错误特殊处理
    if 'L3' in (error_msg or '') or 'model' in (error_msg or '').lower():
        msg = _ERROR_USER_MESSAGES['runtime_error']
    return msg

def _write_failed_status(task_path: str, error_msg: str, error_type: str = 'unknown', tb_tail: str = ''):
    """v12.9 P0: 统一写入任务失败状态（含用户提示 + 可恢复标记）"""
    from datetime import datetime as _dt_fail
    try:
        if not os.path.exists(task_path):
            return
        with open(task_path, 'r', encoding='utf-8') as f:
            task_data = json.load(f)
        task_data['status'] = 'failed'
        task_data['generate_stage'] = 'failed'
        task_data['error'] = error_msg[:500]
        task_data['error_type'] = error_type
        task_data['error_traceback'] = tb_tail[-2000:]
        task_data['failed_at'] = _dt_fail.now().isoformat()
        task_data['last_step'] = task_data.get('generate_stage', 'unknown')
        task_data['loaded_version'] = APP_LOADED_VERSION
        # v12.9 P0-3: 用户级提示
        task_data['user_message'] = _get_user_message(error_type, error_msg)
        task_data['recoverable'] = True
        # v12.9 P0-4: 技术摘要（供"复制错误信息"使用）
        task_data['technical_error'] = f'{error_type}: {error_msg[:200]}'
        _atomic_json(task_path, task_data)
        print(f"[一键生成] ❌ 失败状态已持久化: {task_path} [{error_type}]")
    except Exception as write_err:
        print(f"[一键生成] ❌ 写入失败状态也失败: {write_err}")

def generate_task_token():
    return secrets.token_urlsafe(16)



# v10.4: 启动时版本标记（防旧代码运行误判）
APP_START_TIME = datetime.now().isoformat()
try:
    import subprocess as _sp
    _git_hash = _sp.check_output(['git', 'rev-parse', '--short', 'HEAD'],
                                  cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  stderr=_sp.DEVNULL).decode().strip()
except Exception:
    _git_hash = 'unknown'
# 用关键文件 mtime 作为版本指纹
_gen_mtime = os.path.getmtime(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                            'pipeline', 'generate_video.py'))
APP_LOADED_VERSION = f"git:{_git_hash}_mtime:{int(_gen_mtime)}_start:{APP_START_TIME}"
print(f"[启动] APP_LOADED_VERSION = {APP_LOADED_VERSION}")

# Add project root to path for imports
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def _resolve_env_vars(val):
    """Resolve shell-style ${VAR:-default} in config strings"""
    if not isinstance(val, str):
        return val
    def replacer(m):
        expr = m.group(1)
        if ':-' in expr:
            var, default = expr.split(':-', 1)
            return os.environ.get(var, default)
        return os.environ.get(expr, '')
    return re.sub(r'\$\{([^}]+)\}', replacer, val)


def _load_resolved_config():
    """Load config.json with environment variable resolution"""
    config_path = os.path.join(PROJECT_ROOT, 'config', 'config.json')
    with open(config_path, 'r') as f:
        raw = json.load(f)
    resolved = {}
    for k, v in raw.items():
        if isinstance(v, dict):
            resolved[k] = {kk: _resolve_env_vars(vv) for kk, vv in v.items()}
        else:
            resolved[k] = _resolve_env_vars(v)
    return resolved

resolved_config = _load_resolved_config()

# Resolve storage paths
# ✅ 2026-05-18 fix: absolute paths must NOT use lstrip — lstrip('./') treats '/' as
#    a character set and strips the leading '/' from absolute paths.
#    Fix: check os.path.isabs() first; use absolute paths directly.
_storage = resolved_config['storage']

workdir_val = _storage['workdir']
if os.path.isabs(workdir_val):
    WORK_DIR = workdir_val
else:
    WORK_DIR = os.path.join(PROJECT_ROOT, workdir_val.replace('./', '', 1))

TASKS_DIR = os.path.join(WORK_DIR, 'tasks')
os.makedirs(TASKS_DIR, exist_ok=True)

# Permanent protection: WORK_DIR must be absolute
assert os.path.isabs(WORK_DIR), f'[FATAL] WORK_DIR is not absolute: {WORK_DIR}'
assert os.path.isabs(TASKS_DIR), f'[FATAL] TASKS_DIR is not absolute: {TASKS_DIR}'

print(f'[storage] WORK_DIR={WORK_DIR}')
print(f'[storage] TASKS_DIR={TASKS_DIR}')
print(f'[storage] OUTPUT_DIR={_storage.get("outputs_dir", "N/A")}')

from core.config import config
from core.storage import storage
from pipeline.tasks import create_task, get_task, list_tasks, process_task, save_task

app = Flask(__name__, 
            template_folder=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'templates'),
            static_folder=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'outputs'))
CORS(app)

# ============================================================
# WebUI 静态文件托管（React SPA）
# ============================================================
WEBUI_DIST = os.path.join(PROJECT_ROOT, 'webui', 'dist')

@app.route('/ui')
@app.route('/ui/task-workbench.html')
def serve_workbench():
    """Serve workbench HTML page"""
    resp = send_from_directory(WEBUI_DIST, 'task-workbench.html')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
    return resp

@app.route('/ui/')
def serve_webui_index():
    """Serve WebUI — 统一返回新版 task-workbench.html（修复 Safari 首次访问旧版问题 2026-05-08）"""
    resp = send_from_directory(WEBUI_DIST, 'task-workbench.html')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
    return resp

@app.route('/ui/<path:filename>')
def serve_webui_static(filename):
    """Serve WebUI static assets; SPA fallback 统一到 task-workbench.html"""
    file_path = os.path.join(WEBUI_DIST, filename)
    if os.path.exists(file_path) and os.path.isfile(file_path):
        resp = send_from_directory(WEBUI_DIST, filename)
        # HTML 文件禁止缓存（v7.3：确保修改立即生效）
        if filename.endswith('.html'):
            resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return resp
    # SPA fallback — 统一到新版工作台（不再 fallback 到旧版 index.html）
    resp = send_from_directory(WEBUI_DIST, 'task-workbench.html')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp


# ============================================================
# task_id 生成器（V15 产品链专用）
# 格式：task_YYYYMMDD_NNN（如 task_20260416_001）
# ============================================================
TASK_ID_COUNTER_FILE = os.path.join(PROJECT_ROOT, 'tasks', '_task_counter.json')
TASK_ID_LOCK = threading.Lock()

def generate_task_id():
    """
    生成 task_YYYYMMDD_NNN 格式的任务 ID
    
    规则：
    - 日期前缀：当天日期 YYYYMMDD
    - 序列号：当日从 001 递增
    - 跨天自动重置
    
    返回值：task_id 字符串
    """
    today = datetime.now().strftime('%Y%m%d')
    
    with TASK_ID_LOCK:
        # 读取计数器
        counter = {'date': today, 'seq': 0}
        if os.path.exists(TASK_ID_COUNTER_FILE):
            try:
                with open(TASK_ID_COUNTER_FILE, 'r') as f:
                    counter = json.load(f)
            except:
                pass
        
        # 跨天重置
        if counter.get('date') != today:
            counter = {'date': today, 'seq': 0}
        
        # 递增
        counter['seq'] += 1
        
        # 写回
        os.makedirs(os.path.dirname(TASK_ID_COUNTER_FILE), exist_ok=True)
        with open(TASK_ID_COUNTER_FILE, 'w') as f:
            json.dump(counter, f)
    
    return f"task_{today}_{counter['seq']:03d}"


# ============================================================
# 原有路由（保持向后兼容）
# ============================================================

# Add static route for outputs directory
@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'outputs'), filename)

@app.route('/ai_v1_results')
def ai_v1_results():
    """AI 编辑主线 V1 结果页面"""
    return render_template('ai_v1_results.html')

@app.route('/ai_v1_results_v2')
def ai_v1_results_v2():
    """AI 编辑主线 V1 结果页面（v2 浏览器兼容版）"""
    return render_template('ai_v1_results_v2.html')

# Background task processing
task_queue = []
task_lock = threading.Lock()

def run_task_async(task_id):
    """Run task in background thread. process_v15_task is SYNC, call directly."""
    import traceback as _tb
    try:
        from pipeline.tasks import process_v15_task
        task_path = os.path.join(TASKS_DIR, f"{task_id}.json")
        if not os.path.exists(task_path):
            print(f"[worker] 任务文件不存在: {task_path}")
            return
        with open(task_path) as f:
            task = json.load(f)
        # process_v15_task 是同步函数（非 async），直接调用
        process_v15_task(task_id, task)
    except Exception as e:
        err_tb = _tb.format_exc()
        print(f"[worker] 任务 {task_id} 处理失败: {e}")
        print(err_tb)
        try:
            task_path = os.path.join(TASKS_DIR, f"{task_id}.json")
            if os.path.exists(task_path):
                with open(task_path, 'r', encoding='utf-8') as f:
                    t = json.load(f)
                t['status'] = 'failed'
                t['progress'] = 0
                t['error'] = f'处理异常: {str(e)[:200]}'
                t['technical_error'] = err_tb[-1000:]
                t['updated_at'] = datetime.now().isoformat()
                with open(task_path, 'w', encoding='utf-8') as f:
                    json.dump(t, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

def process_queue_thread():
    """Background thread to process task queue"""
    global task_queue
    while True:
        try:
            task_id = None
            with task_lock:
                if task_queue:
                    task_id = task_queue.pop(0)
            if task_id:
                run_task_async(task_id)
            else:
                threading.Event().wait(1)
        except Exception as e:
            import traceback as _tb2
            print(f"[worker] process_queue_thread 异常: {e}")
            print(_tb2.format_exc())
            threading.Event().wait(3)

def start_background_worker():
    t = threading.Thread(target=process_queue_thread, daemon=True)
    t.start()
    return t

@app.route('/')
def index():
    return render_template('upload.html')

@app.route('/results')
def results():
    return render_template('results.html')

@app.route('/api/upload', methods=['POST'])
def upload():
    if 'files' not in request.files:
        return jsonify({'error': 'No files provided'}), 400
    
    files = request.files.getlist('files')
    file_ids = []
    
    for file in files:
        if file.filename:
            content = file.read()
            file_id, path = storage.save_upload(content, file.filename)
            file_ids.append(file_id)
    
    return jsonify({'file_ids': file_ids, 'count': len(file_ids)})

@app.route('/api/ui/thumbnail', methods=['GET'])
def api_ui_thumbnail():
    """素材缩略图 — 从本地 frames/ 或 TOS 获取"""
    task_id = request.args.get('task_id', '')
    filename = request.args.get('filename', '')
    if not task_id or not filename:
        return jsonify({'error': 'Missing task_id or filename'}), 400
    
    # 查找缩略图
    thumb_name = os.path.splitext(filename)[0] + '.jpg'
    
    # 1. 优先查找本地 frames/ 目录
    frames_dir = os.path.join(PROJECT_ROOT, 'frames', task_id)
    local_thumb = os.path.join(frames_dir, thumb_name)
    if os.path.exists(local_thumb):
        return send_file(local_thumb, mimetype='image/jpeg')
    
    # 2. 查找 tmp_v15 目录（处理中）
    tmp_dir = os.path.join(PROJECT_ROOT, 'tmp_v15', task_id)
    tmp_thumb = os.path.join(tmp_dir, thumb_name)
    if os.path.exists(tmp_thumb):
        return send_file(tmp_thumb, mimetype='image/jpeg')
    
    # 3. 返回占位图
    return jsonify({'error': 'Thumbnail not available', 'status': 'pending'}), 404

@app.route('/api/task', methods=['POST'])
def create_task_api():
    data = request.json
    file_ids = data.get('file_ids', [])
    script = data.get('script', '')
    
    if not file_ids or not script:
        return jsonify({'error': 'Missing file_ids or script'}), 400
    
    task_id = create_task(file_ids, script)
    
    # Queue for processing
    with task_lock:
        task_queue.append(task_id)
    
    return jsonify({'task_id': task_id, 'status': 'queued'})

@app.route('/api/task/<task_id>', methods=['GET'])
def get_task_api(task_id):
    task = get_task(task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    return jsonify(task)

@app.route('/api/tasks', methods=['GET'])
def list_tasks_api():
    tasks = list_tasks()
    return jsonify(tasks)

@app.route('/api/download/<task_id>')
@app.route('/download/<task_id>')
def download(task_id):
    # v12.7: token 校验
    guard = _token_guard(task_id, request.args.get('token', ''))
    if guard:
        return guard
    task = get_task(task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    
    # Priority 1: TOS URL (if local file cleaned)
    if task.get('tos_verified') and task.get('output_url'):
        return redirect(task['output_url'])
    
    # Priority 2: Local file
    output_path = task.get('output_path')
    if output_path and os.path.exists(output_path):
        return send_file(output_path, as_attachment=True, download_name=f"{task_id}.mp4")
    
    return jsonify({'error': 'File not found (local cleaned, TOS not available)'}), 404

@app.route('/api/health')
def health():
    return jsonify({'status': 'ok', 'version': '0.1.0-test'})


@app.route('/api/version')
def api_version():
    """客户端版本检查接口 — 供 uploader 启动时自动检测版本"""
    versions_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'runtime_versions.json')
    try:
        with open(versions_file, 'r', encoding='utf-8') as f:
            versions = json.load(f)
        return jsonify({
            'status': 'ok',
            'server_version': versions.get('server_version', 'unknown'),
            'min_client_version': versions.get('min_client_version', '0.0.0'),
            'recommended_client_version': versions.get('recommended_client_version', '0.0.0'),
            'api_runtime': versions.get('api_runtime', 'flask'),
            'upload_entry': versions.get('upload_entry', ''),
        })
    except Exception:
        return jsonify({'status': 'ok', 'server_version': 'unknown', 'min_client_version': '0.0.0', 'recommended_client_version': '0.0.0'})


# ============================================================
# 返修交接包 V0.1 下载 API
# ============================================================


@app.route('/api/ui/auth/login', methods=['POST'])
def api_ui_auth_login():
    d = request.get_json(silent=True) or {}
    if not VIDEO_TOOL_ACCESS_CODE:
        return jsonify({'ok': True, 'auth_disabled': True})
    if d.get('code') == VIDEO_TOOL_ACCESS_CODE:
        r = jsonify({'ok': True})
        r.set_cookie('vt_access', _make_access_cookie(), max_age=8*3600, httponly=False)
        return r
    return jsonify({'ok': False, 'error': 'Invalid access code'}), 403

@app.route('/api/ui/auth/check')
def api_ui_auth_check():
    if not VIDEO_TOOL_ACCESS_CODE:
        return jsonify({'required': False, 'auth_disabled': True})
    return jsonify({'required': True, 'authenticated': _check_access_cookie()})


@app.route('/api/ui/task/<task_id>/handoff-package/status')
def handoff_package_status(task_id):
    # v12.7: token 校验
    guard = _token_guard(task_id, request.args.get('token', ''))
    if guard:
        return guard
    """查询返修交接包状态"""
    import re as _re
    task_dir = os.path.join(PROJECT_ROOT, 'outputs', task_id)
    if not os.path.isdir(task_dir):
        return jsonify({'exists': False, 'error': '任务目录不存在'}), 404

    # 查找 handoff zip（优先 v0_1_user_test，其次 handoff_package_*）
    zips = sorted(glob.glob(os.path.join(task_dir, 'handoff_package*.zip')))
    if not zips:
        return jsonify({'exists': False, 'message': '当前任务尚未生成返修交接包，请先生成交接包。'})

    latest_zip = zips[-1]
    zip_name = os.path.basename(latest_zip)
    zip_size = os.path.getsize(latest_zip)
    zip_mtime = datetime.fromtimestamp(os.path.getmtime(latest_zip)).isoformat()

    return jsonify({
        'exists': True,
        'zip_name': zip_name,
        'zip_size': zip_size,
        'zip_size_mb': round(zip_size / 1024 / 1024, 1),
        'generated_at': zip_mtime,
        'version': 'V0.2',
    })


@app.route('/api/ui/task/<task_id>/handoff-package/download')
def handoff_package_download(task_id):
    # v12.7: token 校验
    guard = _token_guard(task_id, request.args.get('token', ''))
    if guard:
        return guard
    """下载返修交接包 zip"""
    import re as _re
    # 安全检查：task_id 只允许字母数字下划线
    if not _re.match(r'^[a-zA-Z0-9_]+$', task_id):
        return jsonify({'error': '非法 task_id'}), 400

    task_dir = os.path.join(PROJECT_ROOT, 'outputs', task_id)
    if not os.path.isdir(task_dir):
        return jsonify({'error': '任务目录不存在'}), 404

    # 查找 handoff zip（优先 v0_1_user_test，其次 handoff_package_*）
    zips = sorted(glob.glob(os.path.join(task_dir, 'handoff_package*.zip')))
    if not zips:
        return jsonify({'error': '当前任务尚未生成返修交接包，请先生成交接包。'}), 404

    latest_zip = zips[-1]
    zip_name = os.path.basename(latest_zip)

    # 安全检查：确保路径在 outputs 目录内
    real_path = os.path.realpath(latest_zip)
    outputs_dir = os.path.realpath(os.path.join(PROJECT_ROOT, 'outputs'))
    if not real_path.startswith(outputs_dir):
        return jsonify({'error': '路径安全检查失败'}), 403

    return send_file(real_path, as_attachment=True, download_name=zip_name)


# ============================================================
# V15 产品链 API — 任务初始化 & 自动进入 Web 任务页
# ============================================================

@app.route('/api/ui/task/init', methods=['POST'])
def api_ui_task_init():
    """
    V15 上传前调用：创建任务、生成 task_id
    
    请求体（可选）:
    {
        "file_count": 3,
        "filenames": ["video1.mp4", "video2.mp4"],
        "script": "新闻稿内容...",
        "description": "任务描述"
    }
    
    返回:
    {
        "task_id": "task_20260416_001",
        "task_url": "http://47.93.194.154:8088/ui/task-workbench.html#/task/task_20260416_001",
        "tos_prefix": "windows_ingest/20260416/task_20260416_001/"
    }
    """
    data = request.get_json(silent=True) or {}
    
    task_id = generate_task_id()
    today_dashed = datetime.now().strftime('%Y-%m-%d')
    
    # 创建任务记录 — 双进度字段
    task = {
        'id': task_id,
        'status': 'uploading',
        'created_at': datetime.now().isoformat(),
        'updated_at': datetime.now().isoformat(),
        'progress': 0,  # 任务总进度（0-100）
        'flash_progress': 0,  # Flash 分析进度（0-100），独立于总进度
        'materials_count': data.get('file_count', 0),
        'filenames': data.get('filenames', []),
        'script': data.get('script', ''),
        'description': data.get('description', ''),
        'output_path': None,
        'error': None,
        # 素材级状态追踪
        'material_status': {},  # {filename: {upload: 'uploaded', analysis: 'pending', conclusion: null}}
        # 任务语境（第二层 Pro 候选池分层用）
        'task_context': data.get('task_context', {}),
    }
    
    task_path = os.path.join(TASKS_DIR, f"{task_id}.json")
    os.makedirs(os.path.dirname(task_path), exist_ok=True)
    _atomic_json(task_path, task)
    
    # 构建公网 URL（v12.7.1: 适配 nginx 反代，不再暴露内网端口）
    request_host = request.host.split(':')[0] if request.host else ''
    if request_host in ('', '0.0.0.0', '127.0.0.1', 'localhost'):
        request_host = '47.93.194.154'
    
    scheme = request.headers.get('X-Forwarded-Proto', request.scheme) or 'http'
    # 通过 nginx 反代时不拼接端口（标准端口 80/443 无需显式指定）
    task_url = f"{scheme}://{request_host}/ui/task-workbench.html#/task/{task_id}"
    tos_prefix = f"windows_ingest/{today_dashed}/{task_id}/"
    
    # v12.7: task_token
    task_token = generate_task_token()
    try:
        with open(task_path, 'r', encoding='utf-8') as f:
            _it = json.load(f)
        _it['task_token'] = task_token
        _atomic_json(task_path, _it)
    except Exception:
        pass
    task_url_with_token = f"{task_url}?token={task_token}"

    return jsonify({
        'task_id': task_id,
        'task_token': task_token,
        'task_url': task_url_with_token,
        'tos_prefix': tos_prefix,
        'status': 'created',
    })


@app.route('/api/ui/task/<task_id>/notify', methods=['POST'])
def api_ui_task_notify(task_id):
    # v12.7: token 校验
    _pg_notify = request.get_json(silent=True) or {}
    guard = _token_guard(task_id, _pg_notify.get('token', ''))
    if guard:
        return guard
    """
    V15 上传完成后调用：通知服务器素材已就绪，开始处理
    
    请求体:
    {
        "tos_keys": ["windows_ingest/20260416/task_xxx/video1.mp4"],
        "file_count": 3
    }
    
    返回:
    {
        "task_id": "task_xxx",
        "status": "processing"
    }
    """
    task_path = os.path.join(TASKS_DIR, f"{task_id}.json")
    if not os.path.exists(task_path):
        return jsonify({'error': 'Task not found'}), 404
    
    with open(task_path, 'r', encoding='utf-8') as f:
        task = json.load(f)
    
    data = request.get_json(silent=True) or {}
    
    # 更新任务状态
    task['status'] = 'processing'
    task['progress'] = 5
    task['materials_count'] = data.get('file_count', task.get('materials_count', 0))
    task['tos_keys'] = data.get('tos_keys', [])
    task['updated_at'] = datetime.now().isoformat()
    
    os.makedirs(os.path.dirname(task_path), exist_ok=True)
    with open(task_path, 'w', encoding='utf-8') as f:
        json.dump(task, f, ensure_ascii=False, indent=2)
    
    # 加入处理队列（复用现有处理链）
    with task_lock:
        task_queue.append(task_id)
    
    return jsonify({
        'task_id': task_id,
        'status': 'processing',
        'message': 'Task queued for processing'
    })


# ============================================================
# Phase 1.5: UI API endpoints（Windows 产品化界面）
# ============================================================

@app.route('/api/ui/tasks', methods=['GET'])
def api_ui_tasks():
    """任务总览 — 返回所有任务的精简状态列表"""
    tasks = []
    task_files = sorted(glob.glob(os.path.join(TASKS_DIR, '*.json')))
    for tf in task_files:
        basename = os.path.basename(tf)
        if basename.startswith('_'):
            continue  # 跳过计数器文件
        try:
            with open(tf) as f:
                d = json.load(f)
            tasks.append({
                'task_id': d.get('id', basename.replace('.json', '')),
                'status': d.get('status', 'unknown'),
                'materials_count': d.get('materials_count', 0),
                'clips_count': d.get('timeline_clips_count', d.get('extracted_clips_count', 0)),
                'created_at': d.get('created_at', d.get('timestamp', '')),
                'script': (d.get('script', '') or '')[:100],
                'output_path': d.get('output_path', ''),
                'progress': d.get('progress', 0),
            })
        except:
            pass
    tasks.reverse()  # 最新在前
    return jsonify({'tasks': tasks, 'total': len(tasks)})


@app.route('/api/ui/tasks/<task_id>', methods=['GET'])
def api_ui_task_detail(task_id):
    # v12.7: token 校验
    guard = _token_guard(task_id, request.args.get('token', ''))
    if guard:
        return guard
    """
    单个任务详情 — Web 任务页核心 API
    
    返回任务的完整信息，包括状态、审片结果摘要、候选池摘要、输出信息
    """
    # 直接从 TASKS_DIR 读取（不依赖 pipeline.tasks 的 get_task）
    task_path = os.path.join(TASKS_DIR, f"{task_id}.json")
    if not os.path.exists(task_path):
        return jsonify({'error': 'Task not found'}), 404
    
    with open(task_path, 'r', encoding='utf-8') as f:
        task = json.load(f)
    
    result = {
        'task_id': task.get('id', task_id),
        'status': task.get('status', 'unknown'),
        'progress': task.get('progress', 0),
        'flash_progress': task.get('flash_progress', 0),  # Flash 分析进度（独立字段）
        'created_at': task.get('created_at', ''),
        'updated_at': task.get('updated_at', ''),
        'materials_count': task.get('materials_count', 0),
        'filenames': task.get('filenames', []),  # 文件名列表（用于构建素材骨架）
        'tos_keys': task.get('tos_keys', []),  # TOS 路径（用于构建视频 URL）
        'script': (task.get('script', '') or '')[:200],
        'output_path': task.get('output_path', ''),
        'output_url': task.get('output_url', ''),
        'output_duration': task.get('output_duration', 0),
        'output_filename': task.get('output_filename', ''),
        'generate_stage': task.get('generate_stage', ''),
        'generate_heartbeat': task.get('generate_heartbeat', ''),
        'generate_started_at': task.get('generate_started_at', ''),
        'failed_at': task.get('failed_at', ''),
        'last_step': task.get('last_step', ''),
        'error_traceback': task.get('error_traceback', ''),
        'loaded_version': task.get('loaded_version', ''),
        'versions': task.get('versions', []),
        'config': task.get('config', {}),
        'error': task.get('error', ''),
        'material_status': task.get('material_status', {}),  # 素材级状态
        'task_context': task.get('task_context', {}),  # 视频主题/新闻事件
        'analysis_progress': {
            'total': task.get('materials_count', 0),
            'analyzed': sum(1 for v in task.get('material_status', {}).values()
                          if isinstance(v, dict) and v.get('analysis_status') == 'analyzed'),
            'analyzing': sum(1 for v in task.get('material_status', {}).values()
                           if isinstance(v, dict) and v.get('analysis_status') == 'analyzing'),
            'pending': sum(1 for v in task.get('material_status', {}).values()
                         if isinstance(v, dict) and v.get('analysis_status') in ('pending', None)),
        },
    }
    
    # 附加审片摘要（如果存在）
    review_files = sorted(glob.glob(os.path.join(PROJECT_ROOT, 'analysis_results', f'{task_id}*editing_advice*.json')))
    if review_files:
        try:
            with open(review_files[-1]) as f:
                rd = json.load(f)
            shots = (rd.get('opening_shots') or []) + (rd.get('body_shots') or []) + (rd.get('closing_shots') or [])
            result['review'] = {
                'available': True,
                'summary': rd.get('summary', ''),
                'editing_advice': rd.get('overall_editing_advice', ''),
                'shot_count': len(shots),
            }
        except:
            result['review'] = {'available': False}
    else:
        result['review'] = {'available': False}
    
    # 附加候选池摘要（如果存在）
    pool_files = sorted(glob.glob(os.path.join(PROJECT_ROOT, 'analysis_results', f'{task_id}*candidate*.json')))
    if pool_files:
        try:
            with open(pool_files[-1]) as f:
                pd_ = json.load(f)
            shots = pd_ if isinstance(pd_, list) else (pd_.get('shots') or pd_.get('candidate_shots') or [])
            result['pool'] = {
                'available': True,
                'shot_count': len(shots),
            }
        except:
            result['pool'] = {'available': False}
    else:
        result['pool'] = {'available': False}
    
    return jsonify(result)


@app.route('/api/ui/review', methods=['GET'])
def api_ui_review():
    """素材审片结果 — 返回最新的审片数据，支持按 task_id 过滤"""
    task_id = request.args.get('task_id', '')
    
    review_files = sorted(glob.glob(os.path.join(PROJECT_ROOT, 'analysis_results', '*_editing_advice*.json')))
    
    # 如果指定了 task_id，找对应文件
    if task_id:
        review_files = [f for f in review_files if task_id in f]
    
    # 取最新的非-full版本（full版太大）
    target = None
    for f in reversed(review_files):
        if '_full' not in f:
            target = f
            break
    if not target and review_files:
        target = review_files[-1]
    
    if not target or not os.path.exists(target):
        return jsonify({'error': 'No review data found'}), 404
    
    with open(target) as f:
        d = json.load(f)
    
    # 获取 task_id 和 tos_keys 用于构建视频 URL
    review_task_id = d.get('task_id', '')
    task_path = os.path.join(TASKS_DIR, f"{review_task_id}.json")
    tos_keys = []
    if os.path.exists(task_path):
        with open(task_path, 'r') as tf:
            task_data = json.load(tf)
            tos_keys = task_data.get('tos_keys', [])
    
    # 合并所有 shots
    shots = []
    for idx, shot in enumerate(d.get('opening_shots') or []):
        s = {**shot, 'role': 'opening'}
        if idx < len(tos_keys):
            s['tos_key'] = tos_keys[idx]
        shots.append(s)
    for idx, shot in enumerate(d.get('body_shots') or []):
        s = {**shot, 'role': 'body'}
        offset = len(d.get('opening_shots') or [])
        if (idx + offset) < len(tos_keys):
            s['tos_key'] = tos_keys[idx + offset]
        shots.append(s)
    for idx, shot in enumerate(d.get('closing_shots') or []):
        s = {**shot, 'role': 'closing'}
        offset = len(d.get('opening_shots') or []) + len(d.get('body_shots') or [])
        if (idx + offset) < len(tos_keys):
            s['tos_key'] = tos_keys[idx + offset]
        shots.append(s)
    
    # 不推荐的内容
    avoided = []
    for seg in (d.get('cut_or_shorten_segments') or []):
        if seg.get('suggestion') == '删除':
            avoided.append(seg)
    
    return jsonify({
        'task_id': d.get('task_id', ''),
        'video_id': d.get('video_id', ''),
        'summary': d.get('summary', ''),
        'editing_advice': d.get('overall_editing_advice', ''),
        'shots': shots,
        'avoided': avoided,
        'keep_segments': d.get('keep_segments', []),
        'timestamp': d.get('timestamp', ''),
    })


@app.route('/api/ui/pool', methods=['GET'])
def api_ui_pool():
    """候选池 — 三种视图：windows(默认/可用窗口) / highlights(高光切片) / materials(旧版素材)"""
    task_id = request.args.get('task_id', '')
    view_mode = request.args.get('view', 'windows')  # windows(默认) / highlights / materials
    
    # ========== 新主链：片段级数据 ==========
    full_run_dir = os.path.join(PROJECT_ROOT, 'outputs', 'full_run')
    l2_path = os.path.join(full_run_dir, 'l2_clean_windows_full.json')
    clip_path = os.path.join(full_run_dir, 'clip_candidate_pool_full.json')
    thumb_dir = os.path.join(full_run_dir, 'thumbnails')
    
    has_new_data = os.path.exists(l2_path) and os.path.exists(clip_path)
    
    # ========== task_id 隔离：优先读取本次任务的产物 ==========
    task_l2_path = None
    task_clip_path = None
    task_thumb_dir = None
    if task_id:
        task_output_dir = os.path.join(PROJECT_ROOT, 'outputs', task_id)
        t_l2 = os.path.join(task_output_dir, 'l2_clean_windows_full.json')
        t_clip = os.path.join(task_output_dir, 'clip_candidate_pool_full.json')
        t_thumb = os.path.join(task_output_dir, 'thumbnails')
        if os.path.exists(t_l2):
            task_l2_path = t_l2
            task_clip_path = t_clip if os.path.exists(t_clip) else clip_path
            task_thumb_dir = t_thumb if os.path.exists(t_thumb) else thumb_dir
    
    # 如果有 task 级产物，用 task 级；否则 fallback 到 full_run（兼容旧数据）
    # === 2026-04-22 修复：有 task_id 时，禁止 fallback 到 full_run ===
    # 只有当 task 级产物真实存在时才使用 task 数据
    # 如果 task 级产物不存在且 task 仍在处理中，返回空态而不是 fallback
    if task_id and task_l2_path:
        actual_l2_path = task_l2_path
        actual_clip_path = task_clip_path or clip_path
        actual_thumb_dir = task_thumb_dir or thumb_dir
    elif task_id and not task_l2_path:
        # task 存在但 L2 产物未生成 → 不 fallback，后续由状态机判定返回空态
        actual_l2_path = None
        actual_clip_path = None
        actual_thumb_dir = thumb_dir
    else:
        # 无 task_id → 兼容旧模式，用 full_run
        actual_l2_path = l2_path
        actual_clip_path = clip_path
        actual_thumb_dir = thumb_dir
    has_new_data = actual_l2_path and os.path.exists(actual_l2_path) and actual_clip_path and os.path.exists(actual_clip_path)
    
    # ========== 判断候选池真实状态 ==========
    pool_state = 'pending'
    pool_stage = '等待素材分析完成'
    pool_processed = 0
    pool_total = 0
    pool_proc_count = 0
    pool_sub_stage = ''
    pool_est_total = 0
    pool_start_time = 0
    pool_proc_total = 0
    
    if task_id:
        task_path_check = os.path.join(TASKS_DIR, f"{task_id}.json")
        if os.path.exists(task_path_check):
            with open(task_path_check, 'r', encoding='utf-8') as tf:
                task_data_check = json.load(tf)
            task_status = task_data_check.get('status', 'unknown')
            task_progress = task_data_check.get('progress', 0)
            pool_total = task_data_check.get('materials_count', 0)
            
            # 素材级分析进度
            ms = task_data_check.get('material_status', {})
            pool_processed = sum(1 for v in ms.values() if isinstance(v, dict) and v.get('analysis_status') == 'analyzed')
            
            # 读取 pool_phase（逐条提炼阶段）
            pool_phase = task_data_check.get('pool_phase', '')
            pool_proc_count = task_data_check.get('pool_processed', 0)
            pool_proc_total = task_data_check.get('pool_total', pool_total)
            pool_sub_stage = task_data_check.get('pool_sub_stage', '')
            pool_est_total = task_data_check.get('pool_est_total_sec', 0)
            pool_start_time = task_data_check.get('pool_start_time', 0)
            
            import time as _time
            
            if task_status in ('uploading', 'created'):
                pool_state = 'pending'
                pool_stage = '等待素材上传'
            elif task_status == 'failed':
                # v10.8: 如果 pool_phase 已经 completed，不要因为 task.status=failed（生成阶段失败）
                # 而把 pool_state 覆盖为 failed。pool 和 generate 是独立阶段。
                if pool_phase == 'completed' and has_new_data:
                    pool_state = 'completed'
                    pool_stage = task_data_check.get('pool_stage_text', '候选池提炼完成（生成阶段失败不影响候选池）')
                else:
                    pool_state = 'failed'
                    pool_stage = f'任务处理失败：{task_data_check.get("error", "未知错误")[:60]}'
            elif pool_phase == 'failed':
                pool_state = 'failed'
                pool_stage = task_data_check.get('pool_stage_text', '候选池处理失败')
            elif pool_phase == 'pro_degraded':
                # v7.4: Pro 分层降级（规则兜底），L2 可能还在跑
                pool_state = 'processing'
                pool_stage = task_data_check.get('pool_stage_text', 'Pro 分层已降级，继续 L2 审查')
            elif pool_phase == 'l2_partial':
                # v7.4: L2 部分失败但仍可产出候选池
                pool_state = 'processing'
                pool_stage = task_data_check.get('pool_stage_text', 'L2 部分完成，正在整理结果')
            elif pool_phase == 'health_check':
                pool_state = 'processing'
                pool_stage = task_data_check.get('pool_stage_text', '正在检测 API 可用性...')
            elif task_progress < 50:
                pool_state = 'pending'
                pool_stage = f'AI 分析中（{pool_processed}/{pool_total}）'
            elif pool_phase == 'completed' and has_new_data:
                # 只有 pool_phase=completed 且 task 级 L2 产物真实存在才算完成
                pool_state = 'completed'
                pool_stage = task_data_check.get('pool_stage_text', '候选池提炼完成')
            elif pool_phase in ('processing', '') or (task_progress >= 50 and pool_phase != 'completed'):
                pool_state = 'processing'
                pool_stage = task_data_check.get('pool_stage_text', f'候选池处理中（{pool_proc_count}/{pool_proc_total}）')
                pool_proc_count = task_data_check.get('pool_processed', 0)
                
                # === 停滞检测（2026-04-22 修复：接入正式主链） ===
                updated_at = task_data_check.get('updated_at', '')
                stall_threshold = 600  # 10 分钟无更新视为停滞（v7.3.1：L2 并发下单条可能需 3 分钟）
                if updated_at:
                    try:
                        from datetime import datetime as _dt
                        last_update = _dt.fromisoformat(updated_at.replace('Z', '+00:00'))
                        now = _dt.now(last_update.tzinfo) if last_update.tzinfo else _dt.now()
                        idle_sec = (now - last_update).total_seconds()
                        if idle_sec > stall_threshold:
                            pool_state = 'stalled'
                            pool_stage = f'候选池处理停滞（已 {int(idle_sec//60)} 分钟无更新，后端可能中断）'
                    except:
                        pass
                
                # 预估超时检测
                if pool_start_time > 0:
                    elapsed_total = _time.time() - pool_start_time
                    if pool_est_total > 0 and elapsed_total > pool_est_total * 3:
                        pool_state = 'stalled'
                        pool_stage = f'候选池处理超时（已超过预估时间 3 倍，可能需要重试）'
                
            else:
                pool_state = 'pending'
                pool_stage = f'分析进行中（{pool_processed}/{pool_total}）'
    elif has_new_data:
        # 无 task_id 但有 full_run 数据（兼容旧模式）
        pool_state = 'completed'
        pool_stage = '候选池提炼完成'
    
    # ========== 候选池未完成前返回空态（防止初始化污染） ==========
    if pool_state != 'completed':
        import time as _time
        elapsed = round(_time.time() - pool_start_time, 1) if pool_start_time > 0 else 0
        remaining = max(0, pool_est_total - elapsed) if pool_est_total > 0 else 0
        return jsonify({
            'shots': [],
            'total': 0,
            'view_mode': view_mode,
            'data_source': 'none',
            'overrides': {},
            'pool_status': {
                'state': pool_state,
                'stage': pool_stage,
                'sub_stage': pool_sub_stage if task_id else '',
                'processed': pool_proc_count if task_id else 0,
                'total_sources': pool_proc_total if task_id else pool_total,
                'material_analyzed': pool_processed,
                'material_total': pool_total,
                'est_total_sec': pool_est_total if task_id else 0,
                'elapsed_sec': elapsed,
                'remaining_sec': max(0, round(remaining)),
                'l2_est_total_sec': 0,
                'l2_material_count': 0,
            },
        })
    
    if has_new_data and view_mode == 'windows':
        # ========== 可用窗口池视图（默认） ==========
        with open(actual_l2_path) as f:
            l2_data = json.load(f)
        
        # 加载 candidate.json 获取 Pro 分层的 pool_level（v7.3.1）
        _candidate_levels = {}
        if task_id:
            _cand_path = os.path.join(PROJECT_ROOT, 'analysis_results', f'{task_id}_candidate.json')
            if os.path.exists(_cand_path):
                try:
                    with open(_cand_path) as _cf:
                        _cands = json.load(_cf)
                    for _c in _cands:
                        _cfn = _c.get('source_file', '')
                        _clevel = _c.get('pool_level', 'primary')
                        if _cfn:
                            _candidate_levels[_cfn] = _clevel
                except:
                    pass
        
        # 边界精修参数
        BOUNDARY_SAFETY_MARGIN = 0.5  # 紧邻 boundary 时额外收缩
        ZERO_START_MARGIN = 0.3       # 起点=0 时额外收缩（避免起镜晃动）
        
        # 加载 task 的 material_status 获取真实 tos_key（2026-04-22 修复）
        _task_tos_map = {}
        if task_id:
            _task_json_path = os.path.join(TASKS_DIR, f"{task_id}.json")
            if os.path.exists(_task_json_path):
                with open(_task_json_path, 'r', encoding='utf-8') as _tf:
                    _task_data = json.load(_tf)
                for _fn, _ms in _task_data.get('material_status', {}).items():
                    if isinstance(_ms, dict) and _ms.get('tos_key'):
                        _task_tos_map[_fn] = _ms['tos_key']
        
        shots = []
        for fn, r in l2_data.items():
            if not r.get('usable', False):
                continue
            formality = r.get('formality', 'formal')
            info_type = r.get('information_type', '')
            newsworthiness = r.get('newsworthiness', 'medium')
            issues = r.get('issues', [])
            bm_list = r.get('best_moment_candidates', [])
            boundary_segs = r.get('boundary_segments', [])
            total_dur = r.get('total_duration', 0)
            
            for i, w in enumerate(r.get('clean_windows', [])):
                ws_raw = w.get('start_sec', 0)
                we_raw = w.get('end_sec', 0)
                reason = w.get('reason', '')
                
                # === 编辑层 vs 渲染层分离（2026-04-22 修复） ===
                # raw_start/raw_end = L2 原始值（不可修改）
                # start_sec/end_sec = 编辑层（用户看到的、可拖拽的，等于 raw 值）
                # render_start/render_end = 渲染层（自动精修，仅用于最终成片裁切）
                ws = ws_raw  # 编辑层 = L2 原始值
                we = we_raw
                
                # 渲染层精修（不影响编辑层）
                rs = ws_raw  # render_start
                re = we_raw  # render_end
                refined_notes = []
                
                if rs == 0:
                    rs = round(rs + ZERO_START_MARGIN, 1)
                    refined_notes.append(f"渲染起点从0→{rs}s（避免起镜晃动）")
                
                for b in boundary_segs:
                    be = b.get('end_sec', 0)
                    if abs(ws_raw - be) < 1.0 and ws_raw <= be:
                        new_rs = round(be + BOUNDARY_SAFETY_MARGIN, 1)
                        if new_rs > rs:
                            refined_notes.append(f"渲染起点从{rs}→{new_rs}s（远离boundary）")
                            rs = new_rs
                
                for b in boundary_segs:
                    bs = b.get('start_sec', 0)
                    if abs(we_raw - bs) < 1.0 and we_raw >= bs:
                        new_re = round(bs - BOUNDARY_SAFETY_MARGIN, 1)
                        if new_re < re:
                            refined_notes.append(f"渲染终点从{re}→{new_re}s（远离boundary）")
                            re = new_re
                
                if total_dur > 0 and abs(re - total_dur) < 0.5:
                    re = round(re - 0.3, 1)
                    refined_notes.append(f"渲染终点收缩0.3s（避免收镜拖尾）")
                
                render_dur = round(re - rs, 1)
                dur = round(we - ws, 1)
                if dur < 0.5:
                    continue  # 原始区间太短，跳过
                
                safe_fn = fn.replace('.', '_').replace(' ', '_')
                thumb_name = f"w_{safe_fn}_{i}.jpg"
                thumb_file = os.path.join(actual_thumb_dir, thumb_name)
                _tid_param = f"?task_id={task_id}" if task_id else ""
                thumb_url = f"/api/ui/clip_thumb/{thumb_name.replace('.jpg','')}{_tid_param}" if os.path.exists(thumb_file) else ''
                
                # 统计该窗口内的 best moments
                bm_in_window = [bm for bm in bm_list if bm.get('start_sec', 0) >= ws and bm.get('end_sec', 0) <= we]
                best_score = max((bm.get('highlight_score', 0) for bm in bm_in_window), default=0)
                
                # pool_level: 优先从 candidate.json（Pro 分层）读取，fallback 到 formality 判定
                # v7.3.1: 统一候选池层级来源
                pool_level = _candidate_levels.get(fn, 'primary' if formality == 'formal' else 'backup')
                
                # 构建 tos_key — 优先从 task material_status 读取真实路径（2026-04-22 修复）
                stored_tos_key = _task_tos_map.get(fn, '') or r.get('tos_key', '')
                if not stored_tos_key and task_id:
                    stored_tos_key = f'windows_ingest/{datetime.now().strftime("%Y-%m-%d")}/{task_id}/{fn}'
                elif not stored_tos_key:
                    stored_tos_key = f'windows_ingest/2026-04-17/task_20260417_013/{fn}'
                tos_key = stored_tos_key
                
                # 三级推荐理由策略
                # L1: best_moment 真实推荐理由
                best_reason = ''
                reason_level = 'none'
                if bm_in_window:
                    best_reason = bm_in_window[0].get('candidate_reason', '')
                    if best_reason:
                        reason_level = 'best_moment'
                
                # L2: 窗口级推荐理由（编辑口吻，基于窗口属性）
                if not best_reason:
                    # 根据内容类型生成接近编辑口吻的窗口说明
                    info_lower = (info_type or '').lower()
                    if '合影' in info_lower or '横幅' in info_lower or '主题' in info_lower:
                        best_reason = f'活动主题展示清晰，适合作为开场或收束镜头'
                    elif '发放' in info_lower or '递' in info_lower or '宣传' in info_lower or '讲解' in info_lower:
                        best_reason = f'工作人员与受众互动自然，核心服务场景完整'
                    elif '采访' in info_lower or '反馈' in info_lower:
                        best_reason = f'受访者状态自然，适合作为反馈类镜头'
                    elif '互动' in info_lower or '游戏' in info_lower or '投' in info_lower:
                        best_reason = f'互动环节画面生动，可增加活动氛围感'
                    elif '骑手' in info_lower or '外卖' in info_lower:
                        best_reason = f'骑手群体画面清晰，体现活动受众覆盖'
                    elif info_type:
                        best_reason = f'{info_type[:40]}，画面稳定可用'
                    else:
                        best_reason = f'画面稳定、构图完整，可用于新闻剪辑'
                    
                    if dur >= 8:
                        best_reason += f'，可用时长充足（{dur:.0f}秒）'
                    if best_score >= 8:
                        best_reason += f'，含高光瞬间'
                    reason_level = 'window'
                
                # L3: 素材级兜底
                selection_summary = r.get('selection_summary', '')
                if not best_reason and selection_summary:
                    reason_level = 'material'
                
                # 补全缩略图：如果窗口缩略图不存在，尝试用素材级缩略图
                if not thumb_url:
                    mat_thumb = os.path.join(actual_thumb_dir, f"{safe_fn}.jpg")
                    if os.path.exists(mat_thumb):
                        thumb_url = f"/api/ui/clip_thumb/{safe_fn}{_tid_param}"
                
                # 窗口视图中所有卡片都是窗口标注（原片 seek 播放）
                # 物理片段只在精选瞬间视图中存在
                has_clip = False
                
                shot = {
                    'clip_id': f"w_{safe_fn}_{i}",
                    'source_id': f"w_{safe_fn}_{i}",
                    'window_id': f"w_{safe_fn}_{i}",
                    'original_filename': fn,
                    'source_file': fn,
                    'name': fn,
                    'start_sec': ws,           # 编辑层（= raw，用户可拖拽修改）
                    'end_sec': we,             # 编辑层
                    'base_start_sec': ws,      # 编辑层基准（override delta 的基准）
                    'base_end_sec': we,        # 编辑层基准
                    'render_start_sec': rs,    # 渲染层（自动精修，仅用于成片裁切）
                    'render_end_sec': re,      # 渲染层
                    'raw_start_sec': ws_raw,   # L2 原始值（不可修改）
                    'raw_end_sec': we_raw,     # L2 原始值
                    'boundary_refined': len(refined_notes) > 0,
                    'refined_notes': '; '.join(refined_notes) if refined_notes else '',
                    'duration': f"{ws:.1f}-{we:.1f}s ({dur:.1f}s)",
                    'window_duration': dur,
                    'pool_level': pool_level,
                    'pool_source': 'clean_window',
                    'is_materialized': has_clip,
                    'preview_mode': 'seek',
                    'is_manual': False,
                    'thumbnail': thumb_url,
                    'highlight_score': best_score,
                    'best_moments_count': len(bm_in_window),
                    'candidate_reason': best_reason,
                    'reason_level': reason_level,
                    'selection_summary': selection_summary[:80] if selection_summary else '',
                    'ai_summary': info_type if info_type else '',
                    'formality': formality,
                    'information_type': info_type,
                    'newsworthiness': newsworthiness,
                    'issues_summary': '; '.join(issues[:2]) if issues else '',
                    'metadata': {'duration': dur, 'total_duration': r.get('total_duration', 0)},
                    'tos_key': tos_key,
                }
                shots.append(shot)
        
        # 应用人工窗口覆盖
        window_overrides_path = os.path.join(PROJECT_ROOT, 'config', 'pool_window_overrides.json')
        window_overrides = {}
        if os.path.exists(window_overrides_path):
            try:
                with open(window_overrides_path) as wof:
                    window_overrides = json.load(wof)
            except:
                pass
        
        for shot in shots:
            wid = shot.get('window_id', '')
            if wid in window_overrides:
                wo = window_overrides[wid]
                shot['override_status'] = wo.get('status', 'auto')
                sd = wo.get('start_delta', 0)
                ed = wo.get('end_delta', 0)
                if sd != 0 or ed != 0:
                    new_start = round(shot['base_start_sec'] + sd, 1)
                    new_end = round(shot['base_end_sec'] + ed, 1)
                    # clamp：不允许负数，不允许 start >= end
                    new_start = max(0, new_start)
                    new_end = max(new_start + 0.5, new_end)
                    shot['start_sec'] = new_start  # 编辑层更新
                    shot['end_sec'] = new_end       # 编辑层更新
                    # render 层也同步更新（基于编辑层 + 精修偏移量）
                    render_offset_start = shot.get('render_start_sec', shot['base_start_sec']) - shot['base_start_sec']
                    render_offset_end = shot.get('render_end_sec', shot['base_end_sec']) - shot['base_end_sec']
                    shot['render_start_sec'] = round(max(0, new_start + render_offset_start), 1)
                    shot['render_end_sec'] = round(new_end + render_offset_end, 1)
                    shot['window_duration'] = round(new_end - new_start, 1)
                    shot['duration'] = f"{new_start:.1f}-{new_end:.1f}s ({shot['window_duration']:.1f}s)"
                    shot['is_manual'] = True
                    shot['locked'] = True  # 人工锁定，后续 AI 刷新不覆盖
        
        # v7.3.1: 把 weak_safe / fallback_windows 也加进来作为 backup 展示
        for fn, r in l2_data.items():
            if fn == '_metadata' or not isinstance(r, dict):
                continue
            fw = r.get('weak_safe_segments', r.get('fallback_windows', []))
            for fi, w in enumerate(fw):
                fws = w.get('start_sec', 0)
                fwe = w.get('end_sec', 0)
                fdur = fwe - fws
                if fdur < 2.0:
                    continue  # 太短不展示
                stab = w.get('ffmpeg_stability', '')
                safe_fn = fn.replace('.', '_').replace(' ', '_')
                stored_tos_key = _task_tos_map.get(fn, '')
                
                # v7.3.2: backup 窗口也尝试提供缩略图
                # 优先用素材级缩略图（frames/{task_id}/{filename}.jpg）
                _tid_param = f"?task_id={task_id}" if task_id else ""
                _backup_thumb = ''
                _mat_thumb_path = os.path.join(PROJECT_ROOT, 'frames', task_id or '', os.path.splitext(fn)[0] + '.jpg')
                if os.path.exists(_mat_thumb_path):
                    _backup_thumb = f'/api/ui/thumbnail{_tid_param}&filename={fn}'
                
                # 推荐理由：如果有 ffmpeg 标注就用，否则用通用文案
                _reason = ''
                if stab == 'slight_handheld':
                    _reason = '轻微手持（可补充使用）'
                elif stab == 'normal_camera_move':
                    _reason = '正常运镜（可补充使用）'
                elif stab:
                    _reason = f'{stab}（可补充）'
                else:
                    _reason = '弱安全窗口（可补充）'
                
                shots.append({
                    'source_file': fn,
                    'source_id': f'ws_{fn}_{fi}',
                    'window_id': f'ws_{safe_fn}_{fi}',
                    'start_sec': round(fws, 1),
                    'end_sec': round(fwe, 1),
                    'raw_start_sec': round(fws, 1),
                    'raw_end_sec': round(fwe, 1),
                    'window_duration': round(fdur, 1),
                    'duration': f'{fws:.1f}-{fwe:.1f}s ({fdur:.1f}s)',
                    'pool_level': 'backup',
                    'candidate_reason': _reason,
                    'reason_level': 'window',
                    'thumbnail': _backup_thumb,
                    'tos_key': stored_tos_key,
                    'original_filename': fn,
                    'preview_mode': 'seek',
                    'sort_index': 0,
                })
        
        # 标记被禁用的窗口（不再过滤，让前端按 tab 显示）
        for s in shots:
            if s.get('override_status', 'auto') == 'force_exclude':
                s['pool_level'] = 'disabled'
        
        # 排序: primary > backup > disabled，同级按素材名+窗口序号固定排序（不受区间修改影响）
        level_order = {'primary': 0, 'backup': 1, 'disabled': 2}
        shots.sort(key=lambda s: (level_order.get(s['pool_level'], 9), s.get('source_file', ''), s.get('raw_start_sec', 0)))
        # 给每个 shot 加固定排序序号（前端用此排序，保证一致）
        for si, shot in enumerate(shots):
            shot['sort_index'] = si
        
        # 统计真实处理信息（从源数据统计，不从精修后的窗口统计）
        total_sources = len(l2_data)
        usable_sources = sum(1 for r in l2_data.values() if r.get('usable'))
        primary_count = sum(1 for s in shots if s['pool_level'] == 'primary')
        backup_count = sum(1 for s in shots if s['pool_level'] == 'backup')
        disabled_count = sum(1 for s in shots if s['pool_level'] == 'disabled')
        total_windows = primary_count + backup_count  # 不含 disabled
        # best moments 从 l2_data 源数据统计（不受窗口精修影响）
        total_bm = sum(len(r.get('best_moment_candidates', [])) for r in l2_data.values())
        
        return jsonify({
            'shots': shots,
            'total': len(shots),
            'view_mode': 'windows',
            'data_source': 'full_run_clean_windows',
            'overrides': {},
            'pool_status': {
                'state': pool_state,
                'total_sources': total_sources,
                'usable_sources': usable_sources,
                'total_windows': total_windows,
                'primary_count': primary_count,
                'backup_count': backup_count,
                'disabled_count': disabled_count,
                'total_best_moments': total_bm,
                'total_clips': len(json.load(open(actual_clip_path))) if os.path.exists(actual_clip_path) else 0,
                'stage': pool_stage,
                'processed': pool_proc_count if task_id else total_sources,
                'total_sources_pool': pool_proc_total if task_id else total_sources,
            },
        })
    
    if has_new_data and view_mode == 'highlights':
        # 精选瞬间：从 L2 的 best_moment_candidates 构建（独立数据源）
        with open(actual_l2_path) as f:
            l2_data = json.load(f)
        
        # 加载 task 的 material_status 获取真实 tos_key（2026-04-22 修复）
        _task_tos_map_h = {}
        if task_id:
            _task_json_path_h = os.path.join(TASKS_DIR, f"{task_id}.json")
            if os.path.exists(_task_json_path_h):
                with open(_task_json_path_h, 'r', encoding='utf-8') as _tf:
                    _task_data_h = json.load(_tf)
                for _fn, _ms in _task_data_h.get('material_status', {}).items():
                    if isinstance(_ms, dict) and _ms.get('tos_key'):
                        _task_tos_map_h[_fn] = _ms['tos_key']
        
        shots = []
        for fn, r in l2_data.items():
            if fn == '_metadata' or not isinstance(r, dict):
                continue
            if not r.get('usable', False) and not r.get('clean_windows'):
                continue
            
            bm_list = r.get('best_moment_candidates', [])
            info_type = r.get('information_type', '')
            formality = r.get('formality', '')
            
            for bi, bm in enumerate(bm_list):
                bs = bm.get('start_sec', 0)
                be = bm.get('end_sec', 0)
                dur = round(be - bs, 1)
                if dur < 0.5:
                    continue
                
                safe_fn = fn.replace('.', '_').replace(' ', '_')
                clip_id = f"bm_{safe_fn}_{bi}"
                thumb_name = f"w_{safe_fn}_0"  # 用第一个窗口缩略图
                thumb_file = os.path.join(actual_thumb_dir, f"{thumb_name}.jpg")
                _tid_param = f"?task_id={task_id}" if task_id else ""
                thumb_url = f"/api/ui/clip_thumb/{thumb_name}{_tid_param}" if os.path.exists(thumb_file) else f"/api/ui/clip_thumb/{safe_fn}{_tid_param}"
                
                tos_key = _task_tos_map_h.get(fn, '') or r.get('tos_key', '')
                if not tos_key and task_id:
                    tos_key = f'windows_ingest/{datetime.now().strftime("%Y-%m-%d")}/{task_id}/{fn}'
                elif not tos_key:
                    tos_key = f'windows_ingest/2026-04-17/task_20260417_013/{fn}'
                
                shot = {
                    'clip_id': clip_id,
                    'source_id': clip_id,
                    'window_id': clip_id,
                    'original_filename': fn,
                    'source_file': fn,
                    'name': fn,
                    'start_sec': bs,
                    'end_sec': be,
                    'raw_start_sec': bs,  # 2026-04-22 修复：highlights 也需要 raw_* 字段
                    'raw_end_sec': be,
                    'duration': f"{bs:.1f}-{be:.1f}s ({dur:.1f}s)",
                    'window_duration': dur,
                    'pool_level': 'primary',
                    'pool_source': 'best_moment',
                    'is_materialized': False,
                    'preview_mode': 'seek',
                    'is_manual': False,
                    'thumbnail': thumb_url,
                    'highlight_score': bm.get('highlight_score', 0),
                    'best_moments_count': 1,
                    'candidate_reason': bm.get('candidate_reason', ''),
                    'reason_level': 'best_moment',
                    'expression_quality': bm.get('expression_quality', ''),
                    'action_completion': bm.get('action_completion', ''),
                    'ai_summary': bm.get('candidate_reason', ''),
                    'formality': formality,
                    'information_type': info_type,
                    'metadata': {'duration': dur},
                    'tos_key': tos_key,
                }
                shots.append(shot)
        
        # 按 highlight_score 降序
        shots.sort(key=lambda s: -s.get('highlight_score', 0))
        
        # 如果 L2 没有 best_moments，fallback 到 clip pool（兼容旧数据）
        if not shots and os.path.exists(actual_clip_path):
            with open(actual_clip_path) as f:
                clips = json.load(f)
            for c in clips:
                fn = c.get('source_file', '')
                l2 = l2_data.get(fn, {})
                clip_id = c.get('clip_id', '')
            
                # 缩略图路径
                thumb_file = os.path.join(thumb_dir, f"{clip_id}.jpg")
                thumb_url = f"/api/ui/clip_thumb/{clip_id}" if os.path.exists(thumb_file) else ''
            
                # 从 L2 结果中找到对应的 best_moment
                bm_match = None
                for bm in l2.get('best_moment_candidates', []):
                    if abs(bm.get('start_sec', -1) - c.get('start_sec', 0)) < 1.0:
                        bm_match = bm
                        break
            
                # 构建 tos_key
                tos_key = f'windows_ingest/2026-04-17/task_20260417_013/{fn}'
            
                # 检查 clip 文件是否真实存在
                clip_filename = c.get('clip_filename', f"{clip_id}_{fn}")
                clip_file_path = os.path.join(PROJECT_ROOT, 'outputs', 'full_run', 'clips', clip_filename)
                clip_exists = os.path.exists(clip_file_path)
            
                shot = {
                'clip_id': clip_id,
                'source_id': clip_id,
                'window_id': clip_id,
                'original_filename': fn,
                'source_file': fn,
                'name': fn,
                'start_sec': c.get('start_sec', 0),
                'end_sec': c.get('end_sec', 0),
                'duration': f"{c.get('start_sec',0):.1f}-{c.get('end_sec',0):.1f}s ({c.get('duration',0):.1f}s)",
                'pool_level': c.get('pool_level', 'primary'),
                'pool_source': 'best_moment',
                'is_manual': False,
                'is_materialized': clip_exists,
                'preview_mode': 'clip' if clip_exists else 'seek',
                'is_fallback_clip': c.get('is_fallback_clip', False),
                'boundary_refined': False,
                'refined_notes': '',
                'thumbnail': thumb_url,
                'highlight_score': bm_match.get('highlight_score', 0) if bm_match else 0,
                'candidate_reason': bm_match.get('candidate_reason', c.get('segment_reason', '')) if bm_match else c.get('segment_reason', ''),
                'expression_quality': bm_match.get('expression_quality', '') if bm_match else '',
                'action_completion': bm_match.get('action_completion', '') if bm_match else '',
                'ai_summary': bm_match.get('candidate_reason', '') if bm_match else c.get('segment_reason', ''),
                'exclusion_reason': bm_match.get('candidate_reason', '') if bm_match else '',
                'formality': l2.get('formality', ''),
                'information_type': l2.get('information_type', ''),
                'newsworthiness': l2.get('newsworthiness', ''),
                'metadata': {'duration': c.get('duration', 0)},
                'tos_key': tos_key,
                }
                shots.append(shot)
        
        # 按 pool_level + highlight_score 排序
        level_order = {'primary': 0, 'backup': 1, 'fallback': 2}
        shots.sort(key=lambda s: (level_order.get(s['pool_level'], 9), -s.get('highlight_score', 0)))
        
        # 获取 pool_status（2026-04-22 修复：highlights 视图也需要返回 pool_status）
        _pool_status = {'state': 'completed', 'stage': '精选瞬间已生成'}
        if task_id:
            _task_json_path = os.path.join(TASKS_DIR, f"{task_id}.json")
            if os.path.exists(_task_json_path):
                with open(_task_json_path, 'r', encoding='utf-8') as _tf:
                    _task_data = json.load(_tf)
                _pool_status['state'] = _task_data.get('pool_phase', 'completed')
                _pool_status['stage'] = _task_data.get('pool_stage_text', '精选瞬间已生成')
        
        return jsonify({
            'shots': shots,
            'total': len(shots),
            'view_mode': 'clips',
            'data_source': 'full_run_best_moments',
            'overrides': {},
            'pool_status': _pool_status,
        })
    
    # ========== 旧版降级：素材级数据 ==========
    pool_files = sorted(glob.glob(os.path.join(PROJECT_ROOT, 'analysis_results', '*_candidate*.json')))
    if task_id:
        pool_files = [f for f in pool_files if task_id in f]
    if not pool_files:
        return jsonify({'error': 'No candidate pool data found'}), 404
    
    with open(pool_files[-1]) as f:
        data = json.load(f)
    shots = data if isinstance(data, list) else data.get('shots', data.get('candidate_shots', []))
    
    # 读用户池分层干预结果
    overrides_path = os.path.join(PROJECT_ROOT, 'config', 'user_pool_levels.json')
    overrides = {}
    if os.path.exists(overrides_path):
        try:
            with open(overrides_path) as f:
                overrides = json.load(f)
        except:
            pass
    
    for shot in shots:
        sid = shot.get('source_id', shot.get('clip_id', ''))
        if sid in overrides:
            shot['pool_level'] = overrides[sid]
            shot['is_manual'] = True
        else:
            shot['is_manual'] = False
            if 'pool_level' not in shot:
                shot['pool_level'] = 'auto'
    
    level_order = {'primary': 0, 'backup': 1, 'discard': 2, 'auto': 3}
    shots.sort(key=lambda s: level_order.get(s.get('pool_level', 'auto'), 9))
    
    return jsonify({
        'shots': shots,
        'total': len(shots),
        'view_mode': 'materials',
        'data_source': 'legacy_candidate',
        'overrides': overrides,
        'pool_status': {
            'state': 'pending',
            'stage': '等待素材分析完成',
            'total_sources': len(shots),
        },
    })


@app.route('/api/ui/pool_level', methods=['POST'])
def api_ui_pool_level():
    # v12.7: 无需 token 校验（公共资源）
    """用户干预：设置素材候选池层级 (Primary / Backup / Discard)
    
    请求体:
    {
        "action": "set_primary" | "set_backup" | "set_discard" | "reset",
        "shot_id": "shot_xxx"
    }
    
    结果写入 config/user_pool_levels.json
    """
    data = request.get_json()
    action = data.get('action', '')
    shot_id = data.get('shot_id', '')
    
    if not action or not shot_id:
        return jsonify({'error': 'action and shot_id required'}), 400
        
    level_map = {
        'set_primary': 'primary',
        'set_backup': 'backup',
        'set_discard': 'discard',
        'reset': None
    }
    
    if action not in level_map:
        return jsonify({'error': 'Invalid action'}), 400
    
    overrides_path = os.path.join(PROJECT_ROOT, 'config', 'user_pool_levels.json')
    overrides = {}
    if os.path.exists(overrides_path):
        try:
            with open(overrides_path) as f:
                overrides = json.load(f)
        except:
            pass
    
    new_level = level_map[action]
    if new_level:
        overrides[shot_id] = new_level
    elif shot_id in overrides:
        del overrides[shot_id]
    
    with open(overrides_path, 'w') as f:
        json.dump(overrides, f)
        
    return jsonify({
        'success': True,
        'action': action,
        'shot_id': shot_id,
        'overrides': overrides,
    })


# ============================================================
# 高光候选池 + 最终 Timeline 可视化 API
# ============================================================

@app.route('/api/ui/generate', methods=['POST'])
def api_ui_generate():
    """一键生成成片 — 正式主链（2026-04-22）
    
    请求体:
    {
        "task_id": "task_20260422_002"
    }
    
    完整链路：
    build_l2_segments_text(task_id) → L3 动态调用 → timeline → TTS → 字幕 → 渲染
    消费人工 overrides，产出 task 级成片。
    """
    data = request.get_json(silent=True) or {}
    task_id = data.get('task_id', '')
    if not task_id:
        return jsonify({'error': 'task_id required'}), 400

    # v12.7: token 校验
    token = data.get('token', '') or request.args.get('token', '')
    guard = _token_guard(task_id, token)
    if guard:
        return guard

    # v12.7: 任务级锁
    _task_gen_lock = _get_task_lock(task_id)
    if not _task_gen_lock.acquire(blocking=False):
        return jsonify({'error': '该任务正在生成中，请稍后', 'user_message': '该任务正在生成中，请稍后', 'code': 409}), 409

    # v12.7: 全局并发上限
    if not _try_global_slot():
        _task_gen_lock.release()
        return jsonify({'error': f'当前生成任务较多，请稍后重试', 'user_message': '当前生成任务较多，请稍后重试', 'code': 503}), 503
    
    # 异步执行（后台线程）— v10.4 增强：完整异常持久化 + 阶段心跳
    import threading
    def _run():
        _task_path = os.path.join(TASKS_DIR, f"{task_id}.json")
        _start_time = datetime.now().isoformat()
        
        def _write_stage(stage, extra=None):
            """写入阶段心跳到 task JSON（v12.9 P0 增强：retry 状态映射）"""
            try:
                if os.path.exists(_task_path):
                    with open(_task_path, 'r', encoding='utf-8') as f:
                        _t = json.load(f)
                    _t['generate_stage'] = stage
                    _t['generate_heartbeat'] = datetime.now().isoformat()
                    _t['generate_started_at'] = _start_time
                    _t['loaded_version'] = APP_LOADED_VERSION
                    # v12.9 P0-4: retry 状态映射
                    if stage == 'l3_retrying' and extra:
                        _t['status'] = 'retrying'
                        _t['retry'] = {
                            'stage': 'l3',
                            'count': extra.get('retry_count', 0),
                            'max': extra.get('retry_max', 3),
                            'next_retry_at': extra.get('next_retry_at', ''),
                            'last_error': extra.get('last_error', ''),
                        }
                        _t['user_message'] = 'AI 正在自动重试，请稍候'
                        _t['recoverable'] = True
                    elif extra:
                        _t.update(extra)
                    _atomic_json(_task_path, _t)
            except Exception:
                pass  # 心跳写入失败不影响主流程
        
        try:
            _write_stage('generating')
            from pipeline.generate_video import generate_video
            result = generate_video(task_id, stage_callback=_write_stage)
            _write_stage('done', {'status': 'completed'})
            print(f"[一键生成] 完成: {result}")
            # v12.7: 释放锁
            try:
                _task_gen_lock.release()
            except RuntimeError:
                pass
            _free_global_slot()
        except RuntimeError as cancel_err:
            if 'task_cancelled' in str(cancel_err):
                # v10.5: 用户主动取消
                print(f"[一键生成] ⛔ 用户取消: {task_id}")
                try:
                    if os.path.exists(_task_path):
                        with open(_task_path, 'r', encoding='utf-8') as f:
                            task_data = json.load(f)
                        task_data['status'] = 'cancelled'
                        task_data['generate_stage'] = 'cancelled'
                        task_data['cancelled_at'] = datetime.now().isoformat()
                        task_data['last_step'] = task_data.get('generate_stage', 'unknown')
                        task_data['loaded_version'] = APP_LOADED_VERSION
                        task_data.pop('error', None)
                        task_data.pop('error_traceback', None)
                        _atomic_json(_task_path, task_data)
                except Exception:
                    pass
                return  # 正常退出，不走 fallback
            # v10.8: 其他 RuntimeError（tpad/timeline/replan/L3）也必须写 failed
            import traceback as _tb_rt
            _tb_str_rt = _tb_rt.format_exc()
            _tb_rt.print_exc()
            _err_type = 'runtime_error'
            _err_msg = str(cancel_err)
            if any(kw in _err_msg for kw in ['tpad', 'pad', 'duration', 'shortfall', 'timeline']):
                _err_type = 'render_timeline_duration_mismatch'
            _write_failed_status(_task_path, f'生成失败: {_err_msg[:500]}', _err_type, _tb_str_rt[-2000:])
        except Exception as e:
            import traceback
            tb_str = traceback.format_exc()
            traceback.print_exc()
            # v10.4: 完整异常持久化（前端必须能看到失败原因）
            _write_failed_status(_task_path, f'一键生成失败: {str(e)[:500]}', 'exception', tb_str[-2000:])
        finally:
            # v12.9 P0: 无论成功/失败/取消，都必须释放锁（修复之前只在部分路径释放的 Bug）
            try:
                _task_gen_lock.release()
            except RuntimeError:
                pass
            _free_global_slot()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    
    return jsonify({
        'status': 'generating',
        'task_id': task_id,
        'message': '成片生成已启动，请等待完成',
    })


@app.route('/api/ui/tasks/<task_id>/cancel', methods=['POST'])
def api_ui_cancel_generate(task_id):
    # v12.7: token 校验
    _pg = request.get_json(silent=True) or {}
    guard = _token_guard(task_id, _pg.get('token', '') if 'task_id' in locals() else request.args.get('token', ''))
    if guard:
        return guard
    """v10.5: 终止生成任务"""
    task_path = os.path.join(TASKS_DIR, f"{task_id}.json")
    if not os.path.exists(task_path):
        return jsonify({'error': 'Task not found'}), 404
    
    with open(task_path, 'r', encoding='utf-8') as f:
        task = json.load(f)
    
    # v10.8: 如果任务已经假死（heartbeat 超 5 分钟），直接标记 failed
    _hb = task.get('generate_heartbeat', '')
    _is_stale = False
    if _hb and task.get('status') == 'generating':
        try:
            _hb_time = datetime.fromisoformat(_hb)
            _stale_sec = (datetime.now() - _hb_time).total_seconds()
            if _stale_sec > 300:  # 5 分钟无心跳 = 假死
                _is_stale = True
        except Exception:
            pass
    
    if _is_stale:
        # 假死状态 → 直接标记 failed_cancelled
        task['status'] = 'failed'
        task['generate_stage'] = 'failed'
        task['error'] = f'任务假死后被用户终止（心跳已停 {int(_stale_sec)}s）'
        task['error_type'] = 'stale_cancelled'
        task['failed_at'] = datetime.now().isoformat()
        task['last_step'] = task.get('generate_stage', 'unknown')
    else:
        task['status'] = 'cancelled' if task.get('status') == 'generating' else task.get('status')
        task['generate_stage'] = 'cancelled'
    
    task['cancel_requested'] = True
    task['cancel_reason'] = 'user_cancel'
    task['cancelled_at'] = datetime.now().isoformat()
    
    with open(task_path, 'w', encoding='utf-8') as f:
        json.dump(task, f, ensure_ascii=False, indent=2)
    
    # 尝试杀死关联的 ffmpeg 子进程
    _killed = []
    try:
        import subprocess as _sp
        result = _sp.run(['pgrep', '-f', f'ffmpeg.*{task_id}'], capture_output=True, text=True)
        for pid_str in result.stdout.strip().split('\n'):
            if pid_str.strip():
                try:
                    os.kill(int(pid_str.strip()), 9)
                    _killed.append(int(pid_str.strip()))
                except Exception:
                    pass
    except Exception:
        pass
    
    print(f"[取消生成] task_id={task_id}, killed_pids={_killed}")
    return jsonify({'success': True, 'task_id': task_id, 'killed_pids': _killed})


@app.route('/api/ui/video/<task_id>/<filename>')
def api_ui_video(task_id, filename):
    # v12.7: token 校验
    guard = _token_guard(task_id, request.args.get('token', ''))
    if guard:
        return guard
    """提供 task 级成片文件下载/播放"""
    video_path = os.path.join(PROJECT_ROOT, 'outputs', task_id, filename)
    if os.path.exists(video_path):
        return send_file(video_path, mimetype='video/mp4')
    return jsonify({'error': 'Video not found'}), 404


# ============================================================
# v13.3-c2a: Slot Replace API（人工返修 — 时间线镜头替换）
# 不调用 L2/L3/TTS，不重建 candidate_reel
# ============================================================

@app.route('/api/ui/tasks/<task_id>/timeline', methods=['GET'])
def api_ui_task_timeline(task_id):
    # v12.7: token 校验
    guard = _token_guard(task_id, request.args.get('token', ''))
    if guard:
        return guard
    """返回 task 的 final_render_timeline + slot 详情 + protected 状态"""
    import re as _re_tl
    od = os.path.join(PROJECT_ROOT, 'outputs', task_id)
    frt_path = os.path.join(od, 'final_render_timeline.json')
    sp_path = os.path.join(od, 'slot_plan.json')
    mf_path = os.path.join(od, 'candidate_reel_manifest.json')
    l2_path = os.path.join(od, 'l2_clean_windows_full.json')

    if not os.path.exists(frt_path):
        return jsonify({'error': 'Timeline not found', 'available': False}), 404

    with open(frt_path) as f:
        frt = json.load(f)
    sp_map = {}
    if os.path.exists(sp_path):
        with open(sp_path) as f:
            for s in json.load(f):
                sp_map[s.get('slot_id', '')] = s

    # L2 data for scene_description
    l2_data = {}
    if os.path.exists(l2_path):
        with open(l2_path) as f:
            l2_data = json.load(f)

    # Protected sources
    PROTECTED_SOURCES = {'DJI_20001115144241_0146_D.MP4', 'DJI_20001115144232_0145_D.MP4', '394A0109.MP4'}

    slots = []
    for s in frt:
        sid = s.get('slot_id', '')
        cf = s.get('clip_file', '')
        m = _re_tl.match(r'clip_\d+(?:_extra)?_(.*?)\.mp4', cf)
        src = (m.group(1) + '.MP4') if m else cf
        sp = sp_map.get(sid, {})

        is_anchor = s.get('is_anchor_slot', False)
        is_opener = sid == 'slot_01'
        is_prot_src = src in PROTECTED_SOURCES
        protected = is_opener or is_prot_src
        replaceable = not protected

        l2_entry = l2_data.get(src, {}) if isinstance(l2_data.get(src), dict) else {}
        scene_desc = l2_entry.get('scene_description', '')
        info_type = l2_entry.get('information_type', '')

        # Thumbnail: find existing thumbnail for this source
        thumb_name = 'w_' + src.replace('.', '_') + '_0.jpg'
        thumb_dir_check = os.path.join(od, 'thumbnails')
        has_thumb = os.path.exists(os.path.join(thumb_dir_check, thumb_name)) if os.path.exists(thumb_dir_check) else False
        thumb_url = f'/api/ui/tasks/{task_id}/thumbnail/{thumb_name}' if has_thumb else ''

        # Source video URL for segmented preview
        sv_check = os.path.join(od, 'source_videos', src)
        source_video_url = f'/api/ui/tasks/{task_id}/source-video/{src}' if os.path.exists(sv_check) else ''

        # Source start/end from L2 clean window
        source_start = 0.0
        source_end = s.get('duration', 2.6)
        strong_segs = l2_entry.get('strong_safe_segments', [])
        if strong_segs:
            best = max(strong_segs, key=lambda x: x.get('end_sec',0) - x.get('start_sec',0))
            source_start = best.get('start_sec', 0)
            source_end = min(source_start + s.get('duration', 2.6), best.get('end_sec', source_end))

        slots.append({
            'slot_id': sid,
            'final_start': round(s.get('render_start', 0), 2),
            'final_end': round(s.get('render_end', 0), 2),
            'duration': round(s.get('duration', 0), 2),
            'clip_file': cf,
            'source_file': src,
            'source_video_url': source_video_url,
            'source_start': round(source_start, 3),
            'source_end': round(source_end, 3),
            'scene_description': scene_desc[:80],
            'subtitle_text': sp.get('subtitle_text', ''),
            'protected': protected,
            'replaceable': replaceable,
            'replace_block_reason': 'opener_slot' if is_opener else ('protected_source' if is_prot_src else ''),
            'low_info': info_type in ('LOW_INFO', 'low_info', 'noise'),
            'is_anchor': is_anchor,
            'info_type': info_type,
            'thumbnail_url': thumb_url,
        })

    # Find latest video
    vids = sorted([f for f in os.listdir(od) if f.endswith('.mp4') and task_id in f and 'video_only' not in f and 'candidate_reel' not in f], reverse=True)
    latest_video = vids[0] if vids else ''

    # Manual edits
    edits_dir = os.path.join(od, 'manual_edits')
    manual_edits = []
    if os.path.exists(edits_dir):
        for ts_dir in sorted(os.listdir(edits_dir), reverse=True):
            summary_path = os.path.join(edits_dir, ts_dir, 'replace_slot_summary.json')
            if os.path.exists(summary_path):
                with open(summary_path) as f:
                    manual_edits.append(json.load(f))

    return jsonify({
        'available': True,
        'task_id': task_id,
        'current_video': latest_video,
        'current_video_url': f'/api/ui/video/{task_id}/{latest_video}' if latest_video else '',
        'slot_count': len(slots),
        'slots': slots,
        'manual_edits': manual_edits[:10],
    })


@app.route('/api/ui/tasks/<task_id>/replace-candidates', methods=['GET'])
def api_ui_replace_candidates(task_id):
    # v12.7: token 校验
    guard = _token_guard(task_id, request.args.get('token', ''))
    if guard:
        return guard
    """返回指定 slot 的可替换候选 clips"""
    import re as _re_rc
    slot_id = request.args.get('slot_id', '')
    if not slot_id:
        return jsonify({'error': 'slot_id required'}), 400

    od = os.path.join(PROJECT_ROOT, 'outputs', task_id)
    frt_path = os.path.join(od, 'final_render_timeline.json')
    mf_path = os.path.join(od, 'candidate_reel_manifest.json')
    l2_path = os.path.join(od, 'l2_clean_windows_full.json')

    if not os.path.exists(frt_path) or not os.path.exists(mf_path):
        return jsonify({'error': 'Timeline or manifest not found'}), 404

    with open(frt_path) as f:
        frt = json.load(f)
    with open(mf_path) as f:
        mf_raw = json.load(f)
    clips = mf_raw if isinstance(mf_raw, list) else mf_raw.get('clips', [])
    l2_data = {}
    if os.path.exists(l2_path):
        with open(l2_path) as f:
            l2_data = json.load(f)

    # Current used sources
    used_sources = set()
    target_slot = None
    for s in frt:
        cf = s.get('clip_file', '')
        m = _re_rc.match(r'clip_\d+(?:_extra)?_(.*?)\.mp4', cf)
        src = (m.group(1) + '.MP4') if m else cf
        if s.get('slot_id') != slot_id:
            used_sources.add(src)
        else:
            target_slot = s

    if not target_slot:
        return jsonify({'error': f'slot_id {slot_id} not found'}), 404

    slot_duration = target_slot.get('duration', 0)

    candidates = []
    for c in clips:
        rid = c.get('reel_clip_id', '')
        src = c.get('source_file', '')
        dur = c.get('duration', 0)
        is_p = c.get('is_primary', False)

        l2_entry = l2_data.get(src, {}) if isinstance(l2_data.get(src), dict) else {}
        usable = l2_entry.get('usable_for_mainline', '')
        info_type = l2_entry.get('information_type', '')
        scene_desc = l2_entry.get('scene_description', '')

        # Clean window check
        strong = l2_entry.get('strong_safe_segments', [])
        weak = l2_entry.get('weak_safe_segments', [])
        cw = l2_entry.get('clean_windows', [])
        best_window = None
        w_source = 'none'
        for segs, label in [(strong, 'clean_window'), (weak, 'weak_safe'), (cw, 'clean_window')]:
            if segs:
                best = max(segs, key=lambda x: x.get('end_sec', 0) - x.get('start_sec', 0))
                best_window = {'start': best.get('start_sec', 0), 'end': best.get('end_sec', 0)}
                w_source = label
                break

        # Determine if allowed
        allowed = True
        block_reasons = []

        if usable in ('unsafe', 'rejected', 'no'):
            allowed = False
            block_reasons.append(f'L2: {usable}')
        if src in used_sources:
            allowed = False
            block_reasons.append('duplicate_source')
        if info_type in ('LOW_INFO', 'low_info', 'noise'):
            allowed = False
            block_reasons.append('LOW_INFO')
        if not best_window:
            allowed = False
            block_reasons.append('no_clean_window')
        elif best_window['end'] - best_window['start'] < slot_duration * 0.5:
            allowed = False
            block_reasons.append('window_too_short')

        # Thumbnail
        c_thumb_name = 'w_' + src.replace('.', '_') + '_0.jpg'
        c_thumb_dir = os.path.join(od, 'thumbnails')
        c_has_thumb = os.path.exists(os.path.join(c_thumb_dir, c_thumb_name)) if os.path.exists(c_thumb_dir) else False
        c_thumb_url = f'/api/ui/tasks/{task_id}/thumbnail/{c_thumb_name}' if c_has_thumb else ''

        # Source video URL for preview
        c_sv_check = os.path.join(od, 'source_videos', src)
        c_sv_url = f'/api/ui/tasks/{task_id}/source-video/{src}' if os.path.exists(c_sv_check) else ''

        # Full L2 windows for manual range selection
        all_strong = [{'start': s.get('start_sec',0), 'end': s.get('end_sec',0), 'type': 'clean'} for s in strong]
        all_weak = [{'start': s.get('start_sec',0), 'end': s.get('end_sec',0), 'type': 'weak_safe'} for s in weak]
        all_unsafe = [{'start': s.get('start_sec',0), 'end': s.get('end_sec',0), 'type': 'unsafe'} for s in l2_entry.get('unsafe_segments', [])]
        total_dur_src = l2_entry.get('total_duration', dur)

        candidates.append({
            'clip_id': rid,
            'source_file': src,
            'source_video_url': c_sv_url,
            'scene_description': scene_desc[:60],
            'clean_window': best_window,
            'window_source': w_source,
            'windows': all_strong + all_weak,
            'unsafe_segments': all_unsafe,
            'total_source_duration': round(total_dur_src, 2) if isinstance(total_dur_src, (int, float)) else 0,
            'duration': round(dur, 2),
            'is_primary': is_p,
            'info_type': info_type,
            'usable': usable,
            'allowed': allowed,
            'block_reason': '; '.join(block_reasons) if block_reasons else '',
            'thumbnail_url': c_thumb_url,
        })

    # Sort: allowed first, then by duration desc
    candidates.sort(key=lambda x: (not x['allowed'], -x['duration']))

    return jsonify({
        'task_id': task_id,
        'slot_id': slot_id,
        'slot_duration': slot_duration,
        'candidate_count': len(candidates),
        'allowed_count': sum(1 for c in candidates if c['allowed']),
        'candidates': candidates,
    })


@app.route('/api/ui/tasks/<task_id>/replace-slot', methods=['POST'])
def api_ui_replace_slot(task_id):
    # v12.7: token 校验
    _pg = request.get_json(silent=True) or {}
    guard = _token_guard(task_id, _pg.get('token', '') if 'task_id' in locals() else request.args.get('token', ''))
    if guard:
        return guard
    """执行 slot 替换 — 不调用 L2/L3/TTS"""
    data = request.get_json(silent=True) or {}
    slot_id = data.get('slot_id', '')
    new_clip_id = data.get('new_clip_id', '')
    force = data.get('force', False)
    # v13.3-c2h: 人工选段支持
    new_start = data.get('new_start')
    new_end = data.get('new_end')
    if new_start is not None: new_start = float(new_start)
    if new_end is not None: new_end = float(new_end)

    if not slot_id or not new_clip_id:
        return jsonify({'error': 'slot_id and new_clip_id required'}), 400

    try:
        import sys
        sys.path.insert(0, str(PROJECT_ROOT))
        from tools.replace_slot import replace_slot as _do_replace
        result = _do_replace(task_id, slot_id, new_clip_id, force=force, new_start=new_start, new_end=new_end)

        if result.get('success'):
            # Build video URL for the new video
            video_path = result.get('video_path', '')
            video_filename = os.path.basename(video_path) if video_path else ''
            # manual_edits videos served via a new route
            edit_ts = result.get('timestamp', '')
            video_url = f'/api/ui/video-edit/{task_id}/{edit_ts}/{video_filename}' if video_filename else ''

            return jsonify({
                'success': True,
                'new_video_url': video_url,
                'new_video_path': video_path,
                'summary': result,
            })
        else:
            return jsonify({
                'success': False,
                'errors': result.get('errors', ['Unknown error']),
            }), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'errors': [str(e)]}), 500


@app.route('/api/ui/tasks/<task_id>/source-video/<filename>')
def api_ui_source_video(task_id, filename):
    # v12.7: token 校验
    guard = _token_guard(task_id, request.args.get('token', ''))
    if guard:
        return guard
    """提供 task 原始素材视频（用于分段预览播放）"""
    sv_dir = os.path.join(PROJECT_ROOT, 'outputs', task_id, 'source_videos')
    sv_path = os.path.join(sv_dir, filename)
    if os.path.exists(sv_path):
        return send_file(sv_path, mimetype='video/mp4')
    return jsonify({'error': 'Source video not found'}), 404


@app.route('/api/ui/tasks/<task_id>/thumbnail/<filename>')
def api_ui_slot_thumbnail(task_id, filename):
    # v12.7: token 校验
    guard = _token_guard(task_id, request.args.get('token', ''))
    if guard:
        return guard
    """提供 slot/candidate 缩略图"""
    thumb_dir = os.path.join(PROJECT_ROOT, 'outputs', task_id, 'thumbnails')
    thumb_path = os.path.join(thumb_dir, filename)
    if os.path.exists(thumb_path):
        return send_file(thumb_path, mimetype='image/jpeg')
    return '', 404


@app.route('/api/ui/video-edit/<task_id>/<edit_ts>/<filename>')
def api_ui_video_edit(task_id, edit_ts, filename):
    # v12.7: token 校验
    guard = _token_guard(task_id, request.args.get('token', ''))
    if guard:
        return guard
    """提供 manual_edit 返修视频文件"""
    video_path = os.path.join(PROJECT_ROOT, 'outputs', task_id, 'manual_edits', edit_ts, filename)
    if os.path.exists(video_path):
        return send_file(video_path, mimetype='video/mp4')
    return jsonify({'error': 'Video not found'}), 404


@app.route('/api/ui/pool_adjust', methods=['POST'])
def api_ui_pool_adjust():
    # v12.7: 无需 token 校验（公共资源）
    """人工干预：调整候选池窗口边界 / 保留 / 禁用
    
    请求体:
    {
        "task_id": "task_xxx",
        "window_id": "w_xxx",
        "action": "adjust_start" | "adjust_end" | "keep" | "disable" | "reset",
        "delta": 0.5  // adjust 时的偏移量（秒）
    }
    """
    data = request.get_json()
    task_id = data.get('task_id', '')
    window_id = data.get('window_id', '')
    action = data.get('action', '')
    delta = float(data.get('delta', 0.5))
    
    if not window_id or not action:
        return jsonify({'error': 'window_id and action required'}), 400
    
    # 读取/创建人工调整文件
    overrides_path = os.path.join(PROJECT_ROOT, 'config', 'pool_window_overrides.json')
    overrides = {}
    if os.path.exists(overrides_path):
        try:
            with open(overrides_path) as f:
                overrides = json.load(f)
        except:
            pass
    
    entry = overrides.get(window_id, {'status': 'auto', 'start_delta': 0, 'end_delta': 0})
    
    if action == 'adjust_start':
        entry['start_delta'] = round(delta, 1)  # 绝对偏移，不累加
    elif action == 'adjust_end':
        entry['end_delta'] = round(delta, 1)  # 绝对偏移，不累加
    elif action == 'keep':
        entry['status'] = 'force_keep'
    elif action == 'disable':
        entry['status'] = 'force_exclude'
    elif action == 'reset':
        entry = {'status': 'auto', 'start_delta': 0, 'end_delta': 0}
    
    overrides[window_id] = entry
    
    with open(overrides_path, 'w') as f:
        json.dump(overrides, f, indent=2)
    
    return jsonify({
        'success': True,
        'window_id': window_id,
        'action': action,
        'current': entry,
    })


@app.route('/api/ui/clip_thumb/<clip_id>')
def api_ui_clip_thumb(clip_id):
    """返回片段/窗口起始帧截图（按需从素材帧目录或 TOS 视频提取）"""
    task_id = request.args.get('task_id', '')
    
    # 搜索顺序（2026-04-22 修复）：
    # 1. outputs/{task_id}/thumbnails/ （本 task 窗口级缩略图，优先）
    # 2. outputs/full_run/thumbnails/ （旧 full_run 兼容）
    # 3. frames/{task_dir}/ （素材级首帧 fallback）
    
    search_dirs = []
    if task_id:
        search_dirs.append(os.path.join(PROJECT_ROOT, 'outputs', task_id, 'thumbnails'))
    search_dirs.append(os.path.join(PROJECT_ROOT, 'outputs', 'full_run', 'thumbnails'))
    
    # 直接匹配
    for d in search_dirs:
        thumb_path = os.path.join(d, f'{clip_id}.jpg')
        if os.path.exists(thumb_path):
            return send_file(thumb_path, mimetype='image/jpeg')
    
    # 尝试素材级缩略图（去掉窗口索引后缀）
    base_id = '_'.join(clip_id.rsplit('_', 1)[:-1]) if clip_id.count('_') > 1 else clip_id
    for d in search_dirs:
        for suffix in ['_0', '']:
            alt_path = os.path.join(d, f'{base_id}{suffix}.jpg')
            if os.path.exists(alt_path):
                return send_file(alt_path, mimetype='image/jpeg')
    
    # 尝试 frames 目录（素材级首帧）
    frames_dir = os.path.join(PROJECT_ROOT, 'frames')
    for task_dir in sorted(os.listdir(frames_dir), reverse=True) if os.path.isdir(frames_dir) else []:
        parts = clip_id.split('_')
        if parts[0] == 'w' and len(parts) >= 3:
            stem = '_'.join(parts[1:-1]).replace('_MP4', '').replace('_mp4', '')
            frame_path = os.path.join(frames_dir, task_dir, f'{stem}.jpg')
            if os.path.exists(frame_path):
                return send_file(frame_path, mimetype='image/jpeg')
    
    return '', 404


# ============================================================
# 高光候选池 + 最终 Timeline 可视化 API
# ============================================================

@app.route('/api/ui/highlight_pool', methods=['GET'])
def api_ui_highlight_pool():
    """返回 L2+L2.8 合并结果 + clip pool + timeline，供工作台页面可视化"""
    task_id = request.args.get('task_id', 'task_20260417_013')
    
    # task_id 隔离：优先读 task 级目录
    task_output_dir = os.path.join(PROJECT_ROOT, 'outputs', task_id)
    if os.path.exists(os.path.join(task_output_dir, 'l2_clean_windows_full.json')):
        output_dir = task_output_dir
    else:
        output_dir = os.path.join(PROJECT_ROOT, 'outputs', 'full_run')
    
    result = {
        'task_id': task_id,
        'materials': [],
        'clip_pool': [],
        'timeline': [],
        'stats': {},
    }
    
    # 加载 L2+L2.8 合并结果
    l2_path = os.path.join(output_dir, 'l2_clean_windows_full.json')
    if os.path.exists(l2_path):
        with open(l2_path) as f:
            l2_data = json.load(f)
        for fn, r in l2_data.items():
            material = {
                'source_file': fn,
                'usable': r.get('usable', False),
                'formality': r.get('formality', ''),
                'information_type': r.get('information_type', ''),
                'newsworthiness': r.get('newsworthiness', ''),
                'has_inappropriate': r.get('has_inappropriate', False),
                'issues': r.get('issues', []),
                'clean_windows': r.get('clean_windows', []),
                'unsafe_segments': r.get('unsafe_segments', []),
                'boundary_segments': r.get('boundary_segments', []),
                'best_moment_candidates': r.get('best_moment_candidates', []),
                'pool_level': r.get('pool_level', 'primary'),
            }
            result['materials'].append(material)
    
    # 加载 clip pool
    clip_path = os.path.join(output_dir, 'clip_candidate_pool_full.json')
    if os.path.exists(clip_path):
        with open(clip_path) as f:
            result['clip_pool'] = json.load(f)
    
    # 加载 timeline
    tl_path = os.path.join(output_dir, 'final_timeline_full.json')
    if os.path.exists(tl_path):
        with open(tl_path) as f:
            tl_data = json.load(f)
        result['timeline'] = tl_data.get('timeline', [])
        result['shot_plan'] = tl_data.get('shot_plan', [])
        result['summary'] = tl_data.get('summary', {})
    
    # 加载 manifest
    manifest_path = os.path.join(output_dir, 'render_manifest_full.json')
    if os.path.exists(manifest_path):
        with open(manifest_path) as f:
            result['stats'] = json.load(f).get('stats', {})
    
    # 标记哪些 best_moment 最终入选了 timeline
    timeline_segments = set()
    for t in result.get('timeline', []):
        key = f"{t.get('source_file', '')}_{t.get('start_sec', 0):.1f}_{t.get('end_sec', 0):.1f}"
        timeline_segments.add(key)
    
    for mat in result['materials']:
        for bm in mat.get('best_moment_candidates', []):
            key = f"{mat['source_file']}_{bm.get('start_sec', 0):.1f}_{bm.get('end_sec', 0):.1f}"
            bm['in_timeline'] = key in timeline_segments
    
    return jsonify(result)


# ============================================================
# V15 TOS 预签名上传 — 客户端直传 TOS（2026-05-08 新增）
# 客户端不持有 AK/SK，服务端只负责签名
# ============================================================

@app.route('/api/ui/upload/presign-put', methods=['POST'])
def api_ui_upload_presign_put():
    """
    生成 TOS PUT 预签名 URL，客户端直传文件到 TOS。
    
    请求体:
    {
        "task_id": "task_20260508_001",
        "filename": "video1.mp4",
        "content_type": "video/mp4"  (可选，默认 application/octet-stream)
    }
    
    返回:
    {
        "success": true,
        "put_url": "https://e23-video.tos-cn-beijing.volces.com/...",
        "object_key": "windows_ingest/2026-05-08/task_20260508_001/video1.mp4",
        "expire_seconds": 3600,
        "method": "PUT"
    }
    """
    try:
        from tos import TosClientV2, HttpMethodType
        
        data = request.get_json(silent=True) or {}
        task_id = data.get('task_id', '')
        filename = data.get('filename', '')
        content_type = data.get('content_type', 'application/octet-stream')
        
        if not task_id or not filename:
            return jsonify({'error': 'task_id and filename required'}), 400
        
        # 安全检查：filename 不能包含路径穿越
        safe_filename = os.path.basename(filename)
        if not safe_filename:
            return jsonify({'error': 'Invalid filename'}), 400
        
        # 构造 object_key（与现有路径兼容）
        today = datetime.now().strftime('%Y-%m-%d')
        object_key = f"windows_ingest/{today}/{task_id}/{safe_filename}"
        
        # TOS 凭据
        ak = os.environ.get('TOS_AK', os.environ.get('TOS_AK', ''))
        sk = os.environ.get('TOS_SK', os.environ.get('TOS_SK', ''))
        bucket = os.environ.get('TOS_BUCKET', 'e23-video')
        region = os.environ.get('TOS_REGION', 'cn-beijing')
        endpoint = f'tos-{region}.volces.com'
        
        if not ak or not sk:
            return jsonify({'error': 'TOS credentials not configured on server'}), 500
        
        client = TosClientV2(ak=ak, sk=sk, endpoint=endpoint, region=region)
        
        expire_seconds = 3600
        resp = client.pre_signed_url(
            http_method=HttpMethodType.Http_Method_Put,
            bucket=bucket,
            key=object_key,
            expires=expire_seconds,
        )
        
        print(f"[presign-put] task={task_id} key={object_key} expire={expire_seconds}s")
        
        return jsonify({
            'success': True,
            'put_url': resp.signed_url,
            'object_key': object_key,
            'expire_seconds': expire_seconds,
            'method': 'PUT',
            'content_type': content_type,
        })
    except Exception as e:
        print(f"[presign-put] ERROR: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================
# V15 GUI 上传 — 服务器代理上传端点（presign 替代方案，保留兼容）
# Windows 端通过此端点将文件发给服务器，服务器用 TOS SDK 直传
# ============================================================

@app.route('/api/ui/upload/presign', methods=['POST'])
def api_ui_upload_presign():
    """
    V15 GUI 上传代理：接收文件并上传到 TOS
    
    请求体:
    {
        "tos_key": "windows_ingest/2026-04-16/task_xxx/video.mp4",
        "file_base64": "<base64 encoded file data>"
    }
    
    返回:
    {
        "success": true,
        "tos_key": "windows_ingest/2026-04-16/task_xxx/video.mp4"
    }
    """
    try:
        from tos import TosClientV2
        
        data = request.get_json(silent=True) or {}
        tos_key = data.get('tos_key', '')
        file_base64 = data.get('file_base64', '')
        
        if not tos_key or not file_base64:
            return jsonify({'error': 'tos_key and file_base64 required'}), 400
        
        # 解码文件
        import base64
        file_data = base64.b64decode(file_base64)
        
        # TOS 凭据
        ak = os.environ.get('TOS_INGEST_AK', os.environ.get('TOS_PUBLISH_AK', ''))
        sk = os.environ.get('TOS_INGEST_SK', os.environ.get('TOS_PUBLISH_SK', ''))
        
        if not ak or not sk:
            return jsonify({'error': 'TOS credentials not configured'}), 500
        
        bucket = os.environ.get('TOS_BUCKET', 'e23-video')
        region = os.environ.get('TOS_REGION', 'cn-beijing')
        endpoint = f'tos-{region}.volces.com'
        
        client = TosClientV2(ak=ak, sk=sk, endpoint=endpoint, region=region)
        client.put_object(
            bucket=bucket,
            key=tos_key,
            content=file_data,
        )
        
        return jsonify({
            'success': True,
            'tos_key': tos_key,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============================================================
# V15 任务配置 API — 新闻稿 / 音色 / 字幕（任务级配置）
# ============================================================
TASK_CONFIG_DIR = os.path.join(PROJECT_ROOT, 'tasks', 'configs')
os.makedirs(TASK_CONFIG_DIR, exist_ok=True)

def _task_config_path(task_id):
    """获取任务配置文件路径"""
    return os.path.join(TASK_CONFIG_DIR, f'{task_id}.json')

@app.route('/api/ui/task/<task_id>/config', methods=['GET'])
def api_ui_task_config(task_id):
    """获取任务配置（新闻稿/音色/字幕）"""
    config_path = _task_config_path(task_id)
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            return jsonify(json.load(f))
    # 返回默认配置
    return jsonify({
        'task_id': task_id,
        'script': '',
        'voice': 'zh-CN-XiaoxiaoNeural',
        'subtitle': {
            'enabled': True,
            'position': 'bottom',
            'font_size': 36,
        },
    })

@app.route('/api/ui/task/<task_id>/config', methods=['POST'])
def api_ui_task_config_save(task_id):
    # v12.7: token 校验
    _pg = request.get_json(silent=True) or {}
    guard = _token_guard(task_id, _pg.get('token', ''))
    if guard:
        return guard
    """保存任务配置（支持新闻播报 / 纯音乐混剪双模式）"""
    data = request.get_json(silent=True) or {}
    config_path = _task_config_path(task_id)
    
    # 读取现有配置
    existing = {}
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            existing = json.load(f)
    
    # 直接合并所有传入字段
    passthrough_keys = [
        'edit_mode', 'audio_mode', 'script', 'voice',
        'target_duration', 'bgm_tos_key', 'storyboard_note',
        'news_subtitle', 'music_subtitle', 'subtitle',
    ]
    for key in passthrough_keys:
        if key in data:
            existing[key] = data[key]
    
    existing['task_id'] = task_id
    existing['updated_at'] = datetime.now().isoformat()
    
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    
    return jsonify({'success': True, 'task_id': task_id})


# 可用音色列表
AVAILABLE_VOICES = [
    # 豆包火山引擎声音复刻（正式主链音色）
    {'id': 'S_x249qIGO1', 'name': '复刻女声（默认）', 'lang': '豆包', 'provider': 'volcengine'},
    {'id': 'S_BY29qIGO1', 'name': '复刻男声', 'lang': '豆包', 'provider': 'volcengine'},
    # Azure Edge TTS（备用）
    {'id': 'zh-CN-XiaoxiaoNeural', 'name': '晓晓（女）', 'lang': 'zh-CN', 'provider': 'edge'},
    {'id': 'zh-CN-YunxiNeural', 'name': '云希（男）', 'lang': 'zh-CN', 'provider': 'edge'},
    {'id': 'zh-CN-YunjianNeural', 'name': '云健（男）', 'lang': 'zh-CN', 'provider': 'edge'},
    {'id': 'zh-CN-XiaoyiNeural', 'name': '晓艺（女）', 'lang': 'zh-CN', 'provider': 'edge'},
    {'id': 'zh-CN-YunyangNeural', 'name': '云扬（男）', 'lang': 'zh-CN', 'provider': 'edge'},
    {'id': 'zh-CN-XiaohanNeural', 'name': '晓涵（女）', 'lang': 'zh-CN', 'provider': 'edge'},
]



# ============================================================
# BGM 上传 API
# ============================================================
BGM_ALLOWED_EXTS = {'.mp3', '.wav', '.m4a'}
BGM_MAX_SIZE = 20 * 1024 * 1024  # 20MB
BGM_TOS_PREFIX = 'Music/custom/'

@app.route('/api/ui/bgm/upload', methods=['POST'])
def api_ui_bgm_upload():
    """上传背景音乐到 TOS Music/custom/ 目录"""
    if 'file' not in request.files:
        return jsonify({'error': '未上传文件'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': '文件名为空'}), 400

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in BGM_ALLOWED_EXTS:
        return jsonify({'error': f'不支持的格式 {ext}，仅允许 mp3/wav/m4a'}), 400

    # 读取内容并校验大小
    content = f.read()
    if len(content) > BGM_MAX_SIZE:
        return jsonify({'error': f'文件大小 {len(content)/(1024*1024):.1f}MB 超过 20MB 限制'}), 400

    # 上传到 TOS
    try:
        from tos import TosClientV2
        from dotenv import load_dotenv
        load_dotenv(os.path.join(PROJECT_ROOT, 'config', '.env'))
        client = TosClientV2(
            ak=os.environ['TOS_PUBLISH_AK'],
            sk=os.environ['TOS_PUBLISH_SK'],
            endpoint='tos-cn-beijing.volces.com',
            region='cn-beijing'
        )
        bucket = os.environ.get('TOS_BUCKET', 'e23-video')
        safe_name = f.filename.replace(' ', '_')
        tos_key = f'{BGM_TOS_PREFIX}{safe_name}'

        client.put_object(bucket, tos_key, content=content)
        return jsonify({
            'success': True,
            'tos_key': tos_key,
            'filename': safe_name,
            'size': len(content),
        })
    except Exception as e:
        return jsonify({'error': f'TOS 上传失败: {str(e)[:200]}'}), 500


@app.route('/api/ui/presigned-url', methods=['GET'])
def api_ui_presigned_url():
    """生成 TOS 预签名 URL（用于前端视频播放）"""
    from tos import TosClientV2, HttpMethodType
    
    tos_key = request.args.get('tos_key', '')
    if not tos_key:
        return jsonify({'error': 'Missing tos_key'}), 400
    
    ak = os.environ.get('TOS_INGEST_AK', os.environ.get('TOS_PUBLISH_AK', ''))
    sk = os.environ.get('TOS_INGEST_SK', os.environ.get('TOS_PUBLISH_SK', ''))
    bucket = os.environ.get('TOS_BUCKET', 'e23-video')
    
    try:
        client = TosClientV2(ak=ak, sk=sk, endpoint='tos-cn-beijing.volces.com', region='cn-beijing')
        resp = client.pre_signed_url(
            http_method=HttpMethodType.Http_Method_Get,
            bucket=bucket,
            key=tos_key,
            expires=3600
        )
        return jsonify({'url': resp.signed_url, 'tos_key': tos_key})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/ui/voices', methods=['GET'])
def api_ui_voices():
    """获取可用音色列表"""
    return jsonify({'voices': AVAILABLE_VOICES})


# ============================================================
# v7.3.2: 正式 retry / reprocess API
# ============================================================

@app.route('/api/ui/task/<task_id>/retry_pool', methods=['POST'])
def api_retry_pool(task_id):
    # v12.7: token 校验
    _pg = request.get_json(silent=True) or {}
    guard = _token_guard(task_id, _pg.get('token', ''))
    if guard:
        return guard
    """v7.4: 重试候选池阶段（Pro 分层 + L2 三档审查）"""
    task = get_task(task_id)
    if not task:
        return jsonify({'error': 'task not found'}), 404
    
    # 检查是否有已完成的 Flash 分析（重试候选池的前提）
    ms = task.get('material_status', {})
    analyzed = sum(1 for v in ms.values() if isinstance(v, dict) and v.get('analysis_status') == 'analyzed')
    if analyzed == 0:
        return jsonify({'error': '没有已分析的素材，请先完成素材分析'}), 400
    
    # 重置候选池状态
    task['status'] = 'processing'
    task['pool_phase'] = 'processing'
    task['pool_sub_stage'] = 'retrying'
    task['pool_stage_text'] = '候选池重试中...'
    task['progress'] = 55
    task['error'] = None
    task['generate_error'] = None
    if isinstance(task.get('pool_status'), dict):
        task['pool_status']['error'] = None
    task['updated_at'] = datetime.now().isoformat()
    save_task(task)
    
    # 后台线程启动重试
    import threading
    def _retry_pool():
        try:
            from pipeline.tasks import process_v15_task
            process_v15_task(task_id, task)
        except Exception as e:
            t = get_task(task_id)
            if t:
                t['pool_phase'] = 'failed'
                t['pool_stage_text'] = f'候选池重试失败：{str(e)[:80]}'
                t['updated_at'] = datetime.now().isoformat()
                save_task(t)
    
    threading.Thread(target=_retry_pool, daemon=True).start()
    
    return jsonify({
        'message': f'候选池重试已启动（{analyzed} 条已分析素材）',
        'task_id': task_id,
        'analyzed_materials': analyzed,
    })


@app.route('/api/ui/task/<task_id>/retry_material', methods=['POST'])
def api_retry_material(task_id):
    # v12.7: token 校验
    _pg = request.get_json(silent=True) or {}
    guard = _token_guard(task_id, _pg.get('token', ''))
    if guard:
        return guard
    """重跑单个失败素材的 Flash 分析"""
    data = request.get_json(silent=True) or {}
    filename = data.get('filename', '')
    if not filename:
        return jsonify({'error': 'filename required'}), 400
    
    task = get_task(task_id)
    if not task:
        return jsonify({'error': 'task not found'}), 404
    
    ms = task.get('material_status', {})
    if filename not in ms:
        return jsonify({'error': f'{filename} not in task'}), 404
    
    # 重置该素材状态
    ms[filename]['analysis_status'] = 'pending_retry'
    ms[filename]['flash_error'] = None
    task['updated_at'] = datetime.now().isoformat()
    save_task(task)
    
    # 加入处理队列
    with task_lock:
        task_queue.append(task_id)
    
    return jsonify({'message': f'{filename} queued for retry', 'task_id': task_id})


@app.route('/api/ui/task/<task_id>/reprocess', methods=['POST'])
def api_reprocess_task(task_id):
    # v12.7: token 校验
    _pg = request.get_json(silent=True) or {}
    guard = _token_guard(task_id, _pg.get('token', ''))
    if guard:
        return guard
    """从指定阶段断点续跑任务"""
    data = request.get_json(silent=True) or {}
    from_stage = data.get('from_stage', 'flash')  # flash | pro | l2
    resume = data.get('resume', True)
    
    task = get_task(task_id)
    if not task:
        return jsonify({'error': 'task not found'}), 404
    
    # 重置任务状态
    task['status'] = 'processing'
    task['error'] = None
    if from_stage == 'flash':
        task['progress'] = 20
        task['pool_phase'] = None
    elif from_stage == 'pro':
        task['progress'] = 50
        task['pool_phase'] = None
    elif from_stage == 'l2':
        task['progress'] = 55
        task['pool_phase'] = 'processing'
    task['updated_at'] = datetime.now().isoformat()
    save_task(task)
    
    # 加入处理队列
    with task_lock:
        task_queue.append(task_id)
    
    return jsonify({
        'message': f'Task reprocess from {from_stage} (resume={resume})',
        'task_id': task_id,
        'from_stage': from_stage,
    })

if __name__ == '__main__':
    # 启动时配置完整性检查（主链配置缺失拦截器）
    try:
        from pipeline.startup_check import run_startup_integrity_check
        run_startup_integrity_check()
    except SystemExit:
        raise
    except Exception as _e:
        print(f"[启动检查] 非致命异常: {_e}（继续启动）")
    
    start_background_worker()
    # v12.9.2: 启动 task watchdog
    try:
        from pipeline.watchdog import start_watchdog, _set_globals
        _set_globals(_gen_lock, _free_global_slot, _task_locks, _task_locks_mu)
        start_watchdog()
    except Exception as _e:
        print(f"[v12.9.2] watchdog init failed (non-fatal): {_e}")
    app.run(
        host=resolved_config['server']['host'],
        port=int(resolved_config['server']['port']),
        debug=False,
        threaded=True
    )


