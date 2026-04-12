import os
import sys
import asyncio
import threading
from flask import Flask, request, jsonify, render_template, send_file, send_from_directory
from flask_cors import CORS

# Add project root to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import config
from core.storage import storage
from pipeline.tasks import create_task, get_task, list_tasks, process_task

app = Flask(__name__, 
            template_folder=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'templates'),
            static_folder=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static'))
CORS(app)

# Background task processing
task_queue = []
task_lock = threading.Lock()

def run_task_async(task_id):
    """Run task in background thread"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(process_task(task_id))
    finally:
        loop.close()

# Debug version

def process_queue_thread_old():
    """Background thread to process task queue"""
    global task_queue
    while True:
        task_id = None
        with task_lock:
            if task_queue:
                task_id = task_queue.pop(0)
        if task_id:
            run_task_async(task_id)
        else:
            threading.Event().wait(1)

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
    task = get_task(task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    
    # Priority 1: TOS URL (if local file cleaned)
    if task.get('tos_verified') and task.get('output_url'):
        from flask import redirect
        return redirect(task['output_url'])
    
    # Priority 2: Local file
    output_path = task.get('output_path')
    if output_path and os.path.exists(output_path):
        return send_file(output_path, as_attachment=True, download_name=f"{task_id}.mp4")
    
    return jsonify({'error': 'File not found (local cleaned, TOS not available)'}), 404

@app.route('/api/health')
def health():
    return jsonify({'status': 'ok', 'version': '0.1.0-test'})

if __name__ == '__main__':
    start_background_worker()
    app.run(
        host=config['server']['host'],
        port=config['server']['port'],
        debug=False,
        threaded=True
    )

def process_queue_thread():
    """Background thread to process task queue (with debug)"""
    global task_queue
    import sys
    print("[DEBUG] Queue thread started", file=sys.stderr, flush=True)
    iteration = 0
    while True:
        iteration += 1
        task_id = None
        with task_lock:
            if task_queue:
                task_id = task_queue.pop(0)
                print(f"[DEBUG] Popped task: {task_id}, queue size: {len(task_queue)}", file=sys.stderr, flush=True)
        if task_id:
            print(f"[DEBUG] Processing task: {task_id}", file=sys.stderr, flush=True)
            try:
                run_task_async(task_id)
                print(f"[DEBUG] Task completed: {task_id}", file=sys.stderr, flush=True)
            except Exception as e:
                print(f"[DEBUG] Task failed: {task_id}, error: {e}", file=sys.stderr, flush=True)
        else:
            if iteration % 10 == 0:
                print(f"[DEBUG] Queue empty, waiting... iteration={iteration}", file=sys.stderr, flush=True)
            threading.Event().wait(1)
