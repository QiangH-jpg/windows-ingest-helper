#!/usr/bin/env python3
"""
Windows 上传/预处理助手 v2.0
主入口 - GUI 版本（接入 task_id 链路）

新链路：
- 上传前调用 task/init 获取 task_id + task_url
- TOS 路径固定为 windows_ingest/YYYY-MM-DD/<task_id>/
- 上传完成后调用 notify
- notify 成功后自动打开对应任务页

运行方式：
    Windows: 双击 main_gui.exe
    命令行：python main_gui.py
"""
import os
import sys
import json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from datetime import datetime
import threading
import subprocess
import hashlib
import webbrowser
from pathlib import Path

try:
    import urllib.request
    import urllib.error
    HAS_URLLIB = True
except ImportError:
    HAS_URLLIB = False

# ============================================================
# 服务器配置
# ============================================================
SERVER_URL = os.environ.get("VIDEO_TOOL_SERVER", "http://47.93.194.154:8088")
TOS_BUCKET = os.environ.get("TOS_BUCKET", "e23-video")
TOS_REGION = os.environ.get("TOS_REGION", "cn-beijing")
TOS_ENDPOINT = f"tos-{TOS_REGION}.volces.com"

# TOS 凭据（直传时使用）
TOS_INGEST_AK = os.environ.get("TOS_INGEST_AK", "")
TOS_INGEST_SK = os.environ.get("TOS_INGEST_SK", "")

# ============================================================
# FFmpeg 配置
# ============================================================
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
PROXY_WIDTH = 1280
PROXY_HEIGHT = 720
PROXY_FPS = 25

# 短片阈值
SHORT_DURATION_THRESHOLD = 1.5  # 秒


class IngestHelperGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Windows 上传/预处理助手 v2.0")
        self.root.geometry("900x700")

        # 状态变量
        self.input_dir = tk.StringVar()
        self.output_dir = tk.StringVar(value="./output")
        self.is_processing = False
        self.video_files = []
        self.processed_count = 0
        self.bad_count = 0
        self.uploaded_count = 0

        # 新链路状态
        self.task_id = None
        self.task_url = None
        self.tos_prefix = None
        self.manifest_path = None
        self.uploaded_tos_keys = []

        # 查找 ffmpeg/ffprobe
        self._resolve_ffmpeg()

        self.setup_ui()

    def _resolve_ffmpeg(self):
        """查找 ffmpeg/ffprobe 路径"""
        global FFMPEG, FFPROBE

        # 在 PyInstaller --onefile 模式下，__file__ 指向临时解压目录
        # 必须用 sys.executable 来获取 EXE 所在目录
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(os.path.abspath(sys.executable))
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))

        # 1. 同目录 bin/
        bin_dir = os.path.join(base_dir, "bin")
        if os.name == "nt":
            for name in ("ffmpeg.exe", "ffprobe.exe"):
                p = os.path.join(bin_dir, name)
                if os.path.exists(p):
                    if name == "ffmpeg.exe":
                        FFMPEG = p
                    else:
                        FFPROBE = p
        # 2. 同目录
        for name in ("ffmpeg.exe", "ffprobe"):
            p = os.path.join(base_dir, name)
            if os.path.exists(p) and name.startswith("ffm"):
                FFMPEG = p
        for name in ("ffprobe.exe", "ffprobe"):
            p = os.path.join(base_dir, name)
            if os.path.exists(p) and name.startswith("ffp"):
                FFPROBE = p

    # ============================================================
    # UI 设置
    # ============================================================
    def setup_ui(self):
        """设置界面"""
        # 顶部：目录选择
        top_frame = ttk.LabelFrame(self.root, text="1. 选择目录", padding=10)
        top_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(top_frame, text="素材目录:").grid(row=0, column=0, sticky="w")
        ttk.Entry(top_frame, textvariable=self.input_dir, width=60).grid(row=0, column=1, padx=5)
        ttk.Button(top_frame, text="浏览...", command=self.browse_input).grid(row=0, column=2)

        ttk.Label(top_frame, text="输出目录:").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(top_frame, textvariable=self.output_dir, width=60).grid(row=1, column=1, padx=5)
        ttk.Button(top_frame, text="浏览...", command=self.browse_output).grid(row=1, column=2)

        # 中部：视频列表
        list_frame = ttk.LabelFrame(self.root, text="2. 视频列表", padding=10)
        list_frame.pack(fill="both", expand=True, padx=10, pady=5)

        columns = ("filename", "duration", "resolution", "size", "status")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=15)
        self.tree.heading("filename", text="文件名")
        self.tree.heading("duration", text="时长")
        self.tree.heading("resolution", text="分辨率")
        self.tree.heading("size", text="大小")
        self.tree.heading("status", text="状态")
        self.tree.column("filename", width=300)
        self.tree.column("duration", width=80)
        self.tree.column("resolution", width=100)
        self.tree.column("size", width=80)
        self.tree.column("status", width=150)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # 底部：操作按钮和日志
        bottom_frame = ttk.Frame(self.root, padding=10)
        bottom_frame.pack(fill="x", padx=10, pady=5)

        self.scan_btn = ttk.Button(bottom_frame, text="扫描素材", command=self.scan_videos)
        self.scan_btn.pack(side="left", padx=5)

        self.transcode_btn = ttk.Button(bottom_frame, text="转码 Proxy", command=self.transcode_all, state="disabled")
        self.transcode_btn.pack(side="left", padx=5)

        self.upload_btn = ttk.Button(bottom_frame, text="上传 TOS", command=self.upload_all, state="disabled")
        self.upload_btn.pack(side="left", padx=5)

        self.progress_var = tk.DoubleVar()
        self.progress = ttk.Progressbar(bottom_frame, variable=self.progress_var, maximum=100)
        self.progress.pack(side="top", fill="x", pady=5)

        # 日志区域
        log_frame = ttk.LabelFrame(self.root, text="3. 处理日志", padding=10)
        log_frame.pack(fill="x", padx=10, pady=5)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=8, wrap="word")
        self.log_text.pack(fill="x")

        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief="sunken")
        status_bar.pack(fill="x", padx=10, pady=5)

    # ============================================================
    # 日志
    # ============================================================
    def log(self, message):
        """添加日志"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_text.see("end")
        self.root.update_idletasks()

    # ============================================================
    # 目录浏览
    # ============================================================
    def browse_input(self):
        directory = filedialog.askdirectory(title="选择素材目录")
        if directory:
            self.input_dir.set(directory)
            self.log(f"选择素材目录：{directory}")

    def browse_output(self):
        directory = filedialog.askdirectory(title="选择输出目录")
        if directory:
            self.output_dir.set(directory)
            self.log(f"选择输出目录：{directory}")

    # ============================================================
    # 扫描
    # ============================================================
    def scan_videos(self):
        input_dir = self.input_dir.get()
        if not input_dir or not os.path.exists(input_dir):
            messagebox.showerror("错误", "请先选择有效的素材目录")
            return

        self.log(f"开始扫描：{input_dir}")
        self.status_var.set("正在扫描...")

        for item in self.tree.get_children():
            self.tree.delete(item)
        self.video_files = []

        video_extensions = {'.mp4', '.mov', '.avi', '.mkv', '.m4v'}
        for root, dirs, files in os.walk(input_dir):
            for file in files:
                if Path(file).suffix.lower() in video_extensions:
                    file_path = os.path.join(root, file)
                    self.video_files.append(file_path)

        self.log(f"找到 {len(self.video_files)} 个视频文件")

        for i, file_path in enumerate(self.video_files):
            info = self.get_video_info(file_path)
            if info:
                size_mb = info['size'] / 1024 / 1024
                self.tree.insert("", "end", values=(
                    os.path.basename(file_path),
                    f"{info['duration']:.1f}s",
                    f"{info['width']}x{info['height']}",
                    f"{size_mb:.1f}MB",
                    "待处理"
                ))
            else:
                self.tree.insert("", "end", values=(
                    os.path.basename(file_path),
                    "-",
                    "-",
                    "-",
                    "❌ 无法读取"
                ))

        self.status_var.set(f"扫描完成：{len(self.video_files)} 个文件")
        self.transcode_btn.config(state="normal" if self.video_files else "disabled")
        self.log("扫描完成")

    # ============================================================
    # 元数据读取
    # ============================================================
    def get_video_info(self, video_path):
        cmd = [
            FFPROBE, '-v', 'error',
            '-show_entries', 'stream=width,height,duration,r_frame_rate,codec_name',
            '-show_entries', 'format=filename,size',
            '-of', 'json',
            video_path
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
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
            self.log(f"  获取元数据失败：{os.path.basename(video_path)} - {e}")
            return None

    # ============================================================
    # 转码
    # ============================================================
    def transcode_all(self):
        if not self.video_files:
            return

        self.is_processing = True
        self.transcode_btn.config(state="disabled")
        self.scan_btn.config(state="disabled")

        output_dir = self.output_dir.get()
        proxy_dir = os.path.join(output_dir, 'proxy')
        logs_dir = os.path.join(output_dir, 'logs')
        os.makedirs(proxy_dir, exist_ok=True)
        os.makedirs(logs_dir, exist_ok=True)

        manifest = {
            'version': '2.0',
            'created_at': datetime.now().isoformat(),
            'input_directory': self.input_dir.get(),
            'total_files': len(self.video_files),
            'processed_files': [],
            'bad_files': [],
            'duplicates': []
        }

        def run_transcode():
            self.processed_count = 0
            self.bad_count = 0

            for i, video_path in enumerate(self.video_files):
                progress = int((i + 1) / len(self.video_files) * 100)
                self.progress_var.set(progress)
                self.status_var.set(f"处理中：{i+1}/{len(self.video_files)}")

                filename = os.path.basename(video_path)
                self.log(f"[{i+1}/{len(self.video_files)}] {filename}")

                info = self.get_video_info(video_path)
                if not info:
                    self.log(f"  ❌ 无法读取元数据")
                    manifest['bad_files'].append({
                        'original_path': video_path,
                        'reason': '无法读取元数据',
                        'status': 'bad'
                    })
                    self.bad_count += 1
                    self.update_item_status(i, "❌ 无法读取")
                    continue

                # 短片筛除（≤1.5 秒）
                if info['duration'] <= SHORT_DURATION_THRESHOLD:
                    self.log(f"  ⚠️ 跳过短片 ({info['duration']:.1f}s)")
                    manifest['bad_files'].append({
                        'original_path': video_path,
                        'original_info': info,
                        'reasons': [f"时长过短 ({info['duration']:.1f}s)"],
                        'status': 'skipped_short'
                    })
                    self.bad_count += 1
                    self.update_item_status(i, f"⚠️ 跳过：{info['duration']:.1f}s")
                    continue

                # 坏片检测
                is_bad, reasons = self._check_bad_video(info)
                if is_bad:
                    self.log(f"  ❌ 坏片：{', '.join(reasons)}")
                    manifest['bad_files'].append({
                        'original_path': video_path,
                        'original_info': info,
                        'reasons': reasons,
                        'status': 'bad'
                    })
                    self.bad_count += 1
                    self.update_item_status(i, f"❌ 坏片：{reasons[0]}")
                    continue

                proxy_filename = f"proxy_{i:04d}_{Path(video_path).stem}.mp4"
                proxy_path = os.path.join(proxy_dir, proxy_filename)

                self.log(f"  转码 720p proxy...")
                if self._transcode_one(video_path, proxy_path):
                    proxy_size = os.path.getsize(proxy_path)
                    self.log(f"  ✅ 转码成功 {proxy_size/1024/1024:.1f}MB")
                    file_hash = self.compute_file_hash(video_path)
                    manifest['processed_files'].append({
                        'index': i,
                        'original_path': video_path,
                        'original_filename': filename,
                        'original_info': info,
                        'file_hash': file_hash,
                        'proxy_path': proxy_path,
                        'proxy_filename': proxy_filename,
                        'proxy_size': proxy_size,
                        'tos_key': None,
                        'tos_url': None,
                        'upload_status': 'pending',
                        'preprocess_notes': []
                    })
                    self.update_item_status(i, "✅ 转码完成")
                    self.processed_count += 1
                else:
                    self.log(f"  ❌ 转码失败")
                    self.update_item_status(i, "❌ 转码失败")

            manifest_path = os.path.join(output_dir, 'manifest.json')
            with open(manifest_path, 'w', encoding='utf-8') as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)

            log_path = os.path.join(logs_dir, f'ingest_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json')
            with open(log_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'input_directory': self.input_dir.get(),
                    'output_directory': output_dir,
                    'total_files': len(self.video_files),
                    'processed': self.processed_count,
                    'bad': self.bad_count,
                    'time': datetime.now().isoformat()
                }, f, ensure_ascii=False, indent=2)

            self.log(f"\n✅ 转码完成：成功 {self.processed_count} 个，失败 {self.bad_count} 个")
            self.log(f"清单已保存：{manifest_path}")

            self.is_processing = False
            self.progress_var.set(100)
            self.status_var.set(f"转码完成：{self.processed_count} 成功，{self.bad_count} 失败")
            self.transcode_btn.config(state="disabled")
            self.scan_btn.config(state="normal")
            self.upload_btn.config(state="normal" if self.processed_count > 0 else "disabled")

            self.manifest_path = manifest_path

        threading.Thread(target=run_transcode, daemon=True).start()

    def _check_bad_video(self, info):
        reasons = []
        if info['height'] < 360:
            reasons.append(f"分辨率过低 ({info['width']}x{info['height']})")
        if info['size'] < 102400:
            reasons.append(f"文件大小异常 ({info['size']/1024:.1f}KB)")
        return len(reasons) > 0, reasons

    def _transcode_one(self, input_path, output_path):
        cmd = [
            FFMPEG, '-y',
            '-i', input_path,
            '-vf', f'scale={PROXY_WIDTH}:{PROXY_HEIGHT}:force_original_aspect_ratio=decrease,pad={PROXY_WIDTH}:{PROXY_HEIGHT}:(ow-iw)/2:(oh-ih)/2',
            '-c:v', 'libx264',
            '-preset', 'fast',
            '-b:v', '3M',
            '-r', str(PROXY_FPS),
            '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart',
            '-c:a', 'aac',
            '-b:a', '128k',
            output_path
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=600,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            return result.returncode == 0
        except Exception as e:
            self.log(f"  转码错误：{e}")
            return False

    def compute_file_hash(self, file_path):
        sha256 = hashlib.sha256()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                sha256.update(chunk)
        return sha256.hexdigest()

    def update_item_status(self, index, status):
        items = self.tree.get_children()
        if index < len(items):
            values = list(self.tree.item(items[index])['values'])
            values[4] = status
            self.tree.item(items[index], values=values)

    # ============================================================
    # 上传（接入 task_id 新链路）
    # ============================================================
    def upload_all(self):
        """上传到 TOS（接入 task/init + notify + task_id 路径）"""
        if not hasattr(self, 'manifest_path') or not os.path.exists(self.manifest_path):
            messagebox.showerror("错误", "请先完成转码")
            return

        # 读取 manifest
        with open(self.manifest_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)

        processed = manifest.get('processed_files', [])
        if not processed:
            messagebox.showinfo("提示", "没有可上传的文件")
            return

        self.upload_btn.config(state="disabled")
        self.scan_btn.config(state="disabled")
        self.transcode_btn.config(state="disabled")

        def run_upload():
            self.uploaded_count = 0
            self.uploaded_tos_keys = []

            # ---- Step 1: task/init ----
            self.log(">>> Step 1: 初始化任务...")
            self.status_var.set("初始化任务...")
            task_id, task_url, tos_prefix = self._call_task_init(processed)
            if not task_id:
                self.log("❌ task/init 失败，终止上传", "ERROR")
                self.status_var.set("上传失败")
                self._restore_buttons()
                return

            self.task_id = task_id
            self.task_url = task_url
            self.tos_prefix = tos_prefix
            self.log(f"✅ task_id: {task_id}")
            self.log(f"   task_url: {task_url}")
            self.log(f"   tos_prefix: {tos_prefix}")

            # ---- Step 2: 逐文件上传 ----
            self.log(">>> Step 2: 上传到 TOS...")
            self.status_var.set("上传中...")
            total = len(processed)
            upload_failures = []

            for i, item in enumerate(processed):
                proxy_path = item.get('proxy_path', '')
                original_filename = item.get('original_filename', os.path.basename(proxy_path))
                # 统一使用原始文件名上传
                tos_key = f"{tos_prefix}{original_filename}"

                progress = int((i + 1) / total * 100)
                self.progress_var.set(progress)
                self.status_var.set(f"上传：{i+1}/{total}")

                self.log(f"[{i+1}/{total}] {original_filename}")
                self.log(f"  tos_key: {tos_key}")

                ok, err = self._upload_one(proxy_path, tos_key)
                if ok:
                    item['tos_key'] = tos_key
                    item['upload_status'] = 'uploaded'
                    self.uploaded_tos_keys.append(tos_key)
                    self.uploaded_count += 1
                    idx = item.get('index', i)
                    self.update_item_status(idx, "✅ 已上传")
                    self.log(f"  ✅ 上传成功")
                else:
                    item['upload_status'] = 'failed'
                    upload_failures.append(original_filename)
                    idx = item.get('index', i)
                    self.update_item_status(idx, f"❌ 上传失败")
                    self.log(f"  ❌ 上传失败: {err}")

            # 保存更新后的 manifest
            with open(self.manifest_path, 'w', encoding='utf-8') as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)

            if not self.uploaded_tos_keys:
                self.log("❌ 没有文件上传成功，终止", "ERROR")
                self.status_var.set("上传失败")
                self._restore_buttons()
                return

            self.log(f"\n上传完成：成功 {self.uploaded_count}/{total}")

            # ---- Step 3: notify ----
            self.log(">>> Step 3: 通知服务器...")
            self.status_var.set("通知服务器...")
            notify_ok, notify_err = self._call_notify(self.task_id, self.uploaded_tos_keys)
            if notify_ok:
                self.log("✅ 服务器已接收，进入处理状态")
            else:
                self.log(f"⚠️ notify 失败: {notify_err}")

            # ---- Step 4: 自动打开任务页 ----
            if self.task_url:
                self.log(">>> Step 4: 打开任务页...")
                try:
                    webbrowser.open(self.task_url)
                    self.log(f"✅ 已打开任务页: {self.task_url}")
                except Exception as e:
                    self.log(f"⚠️ 打开浏览器失败: {e}")
                    self.log(f"   请手动打开: {self.task_url}")

            # ---- 完成 ----
            self.progress_var.set(100)
            if upload_failures:
                self.status_var.set(
                    f"上传完成：{self.uploaded_count}/{total} 成功，"
                    f"{len(upload_failures)} 失败"
                )
                self.log(f"\n⚠️ 上传失败文件: {', '.join(upload_failures)}")
            else:
                self.status_var.set(f"上传完成：{self.uploaded_count}/{total} 全部成功")

            self.log(f"\n✅ 全部完成！task_id: {self.task_id}")
            self._restore_buttons()

        threading.Thread(target=run_upload, daemon=True).start()

    def _restore_buttons(self):
        self.upload_btn.config(state="normal")
        self.scan_btn.config(state="normal")
        if self.processed_count > 0:
            self.transcode_btn.config(state="disabled")

    # ============================================================
    # task/init 调用
    # ============================================================
    def _call_task_init(self, processed_files):
        if not HAS_URLLIB:
            self.log("  ❌ urllib 不可用", "ERROR")
            return None, None, None

        url = f"{SERVER_URL}/api/ui/task/init"
        filenames = [f.get('original_filename', 'unknown') for f in processed_files]
        payload = json.dumps({
            "file_count": len(processed_files),
            "filenames": filenames,
        }).encode("utf-8")

        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            task_id = data.get("task_id")
            task_url = data.get("task_url")
            tos_prefix = data.get("tos_prefix")
            if task_id:
                return task_id, task_url, tos_prefix
            self.log(f"  task/init 返回异常: {data}")
            return None, None, None
        except Exception as e:
            self.log(f"  task/init 失败: {e}")
            return None, None, None

    # ============================================================
    # notify 调用
    # ============================================================
    def _call_notify(self, task_id, tos_keys):
        if not HAS_URLLIB:
            return False, "urllib 不可用"

        url = f"{SERVER_URL}/api/ui/task/{task_id}/notify"
        payload = json.dumps({
            "tos_keys": tos_keys,
            "file_count": len(tos_keys),
        }).encode("utf-8")

        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data.get("status") == "processing", data.get("message", "")
        except Exception as e:
            return False, str(e)

    # ============================================================
    # 单文件上传到 TOS
    # ============================================================
    def _upload_one(self, local_path, tos_key):
        """
        上传单个文件到 TOS。

        优先级：
        1. TOS SDK 直传（如果 tos 包可用且已配置 AK/SK）
        2. 服务器代理（通过预签名 URL）
        """
        if not os.path.exists(local_path):
            return False, f"文件不存在: {local_path}"

        # --- 方式 1: TOS SDK 直传 ---
        try:
            from tos import TosClientV2
            ak = TOS_INGEST_AK or os.environ.get("TOS_INGEST_AK", "")
            sk = TOS_INGEST_SK or os.environ.get("TOS_INGEST_SK", "")
            if ak and sk:
                client = TosClientV2(
                    ak=ak, sk=sk,
                    endpoint=TOS_ENDPOINT,
                    region=TOS_REGION,
                )
                client.put_object_from_file(
                    bucket=TOS_BUCKET,
                    key=tos_key,
                    file_path=local_path,
                )
                return True, None
        except ImportError:
            pass
        except Exception as e:
            self.log(f"  TOS SDK 直传失败: {e}")

        # --- 方式 2: 服务器预签名 URL ---
        if HAS_URLLIB:
            try:
                presign_url = f"{SERVER_URL}/api/ui/upload/presign"
                presign_req = urllib.request.Request(
                    presign_url,
                    data=json.dumps({"tos_key": tos_key}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(presign_req, timeout=30) as resp:
                    presign_data = json.loads(resp.read().decode("utf-8"))
                presigned = presign_data.get("url")
                if presigned:
                    with open(local_path, 'rb') as f:
                        file_data = f.read()
                    put_req = urllib.request.Request(
                        presigned, data=file_data,
                        method="PUT",
                        headers={"Content-Type": "application/octet-stream"}
                    )
                    with urllib.request.urlopen(put_req, timeout=300) as resp:
                        if resp.status == 200:
                            return True, None
            except Exception as e:
                self.log(f"  服务器代理上传失败: {e}")

        return False, "TOS 上传失败：SDK 和服务器代理均不可用"


def main():
    root = tk.Tk()
    app = IngestHelperGUI(root)
    root.mainloop()


if __name__ == '__main__':
    main()
