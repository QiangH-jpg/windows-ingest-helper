#!/usr/bin/env python3
"""
Windows 上传/预处理助手 v3.2

v3.2 变更：
- "活动主题"→"视频主题"，输入框加宽与素材目录同宽
- 新增"新闻事件"多行文本框（两行高，与视频列表同宽）
- 删除"目标输出""重点主体""优先镜头""回避镜头"（已迁移到工作台）
- 视频列表高度缩减为约 2/3，让处理日志默认完整可见
- 区块编号统一为 1-4

v3.1 变更：
- 新增"任务语境（粗筛用）"输入区

v3.0 变更：
- onedir 分发（主EXE + bin/ffmpeg.exe + bin/ffprobe.exe）
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
import base64
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
SHORT_DURATION_THRESHOLD = 1.5


def format_duration(seconds):
    return f"{seconds:.1f}秒"

def format_size_mb(size_bytes):
    return f"{size_bytes / 1024 / 1024:.1f}MB"


class IngestHelperGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Windows 上传/预处理助手 v3.2")
        self.root.geometry("900x780")

        self.input_dir = tk.StringVar()
        self.output_dir = tk.StringVar(value="./output")
        self.is_processing = False
        self.video_files = []
        self.processed_count = 0
        self.bad_count = 0
        self.uploaded_count = 0
        self.scan_count = 0

        self.task_id = None
        self.task_url = None
        self.tos_prefix = None
        self.manifest_path = None
        self.uploaded_tos_keys = []

        self._resolve_ffmpeg()
        self.setup_ui()

    def _resolve_ffmpeg(self):
        global FFMPEG, FFPROBE
        if getattr(sys, 'frozen', False):
            app_dir = os.path.dirname(os.path.abspath(sys.executable))
        else:
            app_dir = os.path.dirname(os.path.abspath(__file__))

        self._app_dir = app_dir

        # 搜索顺序：bin/ → _internal/bin/ → 上级/bin/ → 同级目录
        search_dirs = [
            os.path.join(app_dir, "bin"),
            os.path.join(app_dir, "_internal", "bin"),
            os.path.join(os.path.dirname(app_dir), "bin"),
            app_dir,
        ]

        ffmpeg_found = None
        ffprobe_found = None
        bin_dir_found = None

        for sd in search_dirs:
            if not os.path.isdir(sd):
                continue
            ff = os.path.join(sd, "ffmpeg.exe")
            fp = os.path.join(sd, "ffprobe.exe")
            if os.path.exists(fp) and not ffprobe_found:
                ffprobe_found = fp
                bin_dir_found = sd
            if os.path.exists(ff) and not ffmpeg_found:
                ffmpeg_found = ff
                if not bin_dir_found:
                    bin_dir_found = sd

        self._bin_dir = bin_dir_found or os.path.join(app_dir, "bin")

        if ffmpeg_found:
            FFMPEG = ffmpeg_found
        if ffprobe_found:
            FFPROBE = ffprobe_found

        self._ffmpeg_path = FFMPEG
        self._ffprobe_path = FFPROBE
        self._ffmpeg_exists = os.path.exists(FFMPEG) if FFMPEG != "ffmpeg" else False
        self._ffprobe_exists = os.path.exists(FFPROBE) if FFPROBE != "ffprobe" else False

    # ============================================================
    # UI 设置
    # ============================================================
    def setup_ui(self):
        # ---- 1. 选择目录 ----
        top_frame = ttk.LabelFrame(self.root, text="1. 选择目录", padding=10)
        top_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(top_frame, text="素材目录:").grid(row=0, column=0, sticky="w")
        ttk.Entry(top_frame, textvariable=self.input_dir, width=60).grid(row=0, column=1, padx=5, sticky="ew")
        ttk.Button(top_frame, text="浏览...", command=self.browse_input).grid(row=0, column=2)

        ttk.Label(top_frame, text="输出目录:").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(top_frame, textvariable=self.output_dir, width=60).grid(row=1, column=1, padx=5, sticky="ew")
        ttk.Button(top_frame, text="浏览...", command=self.browse_output).grid(row=1, column=2)

        top_frame.columnconfigure(1, weight=1)

        # ---- 2. 任务语境 ----
        ctx_frame = ttk.LabelFrame(self.root, text="2. 任务语境", padding=10)
        ctx_frame.pack(fill="x", padx=10, pady=5)

        self.ctx_theme = tk.StringVar()

        ttk.Label(ctx_frame, text="视频主题:").grid(row=0, column=0, sticky="w")
        ttk.Entry(ctx_frame, textvariable=self.ctx_theme, width=60).grid(row=0, column=1, padx=5, sticky="ew")

        ttk.Label(ctx_frame, text="新闻事件:").grid(row=1, column=0, sticky="nw", pady=(5, 0))
        self.ctx_event = tk.Text(ctx_frame, height=2, width=60, wrap="word")
        self.ctx_event.grid(row=1, column=1, padx=5, pady=(5, 0), sticky="ew")

        ctx_frame.columnconfigure(1, weight=1)

        # ---- 3. 视频列表（高度缩减为 height=8） ----
        list_frame = ttk.LabelFrame(self.root, text="3. 视频列表", padding=10)
        list_frame.pack(fill="both", expand=True, padx=10, pady=5)

        columns = ("filename", "duration", "resolution", "size", "status")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=8)
        self.tree.heading("filename", text="文件名")
        self.tree.heading("duration", text="时长")
        self.tree.heading("resolution", text="分辨率")
        self.tree.heading("size", text="大小")
        self.tree.heading("status", text="状态")
        self.tree.column("filename", width=300)
        self.tree.column("duration", width=80)
        self.tree.column("resolution", width=100)
        self.tree.column("size", width=80)
        self.tree.column("status", width=180)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # ---- 操作按钮 ----
        btn_frame = ttk.Frame(self.root, padding=(10, 0))
        btn_frame.pack(fill="x", padx=10)

        self.scan_btn = ttk.Button(btn_frame, text="扫描素材", command=self.scan_videos)
        self.scan_btn.pack(side="left", padx=5)

        self.transcode_btn = ttk.Button(btn_frame, text="转码 Proxy", command=self.transcode_all, state="disabled")
        self.transcode_btn.pack(side="left", padx=5)

        self.upload_btn = ttk.Button(btn_frame, text="上传 TOS", command=self.upload_all, state="disabled")
        self.upload_btn.pack(side="left", padx=5)

        self.progress_var = tk.DoubleVar()
        self.progress = ttk.Progressbar(btn_frame, variable=self.progress_var, maximum=100)
        self.progress.pack(side="top", fill="x", pady=5)

        # ---- 4. 处理日志（高度增大为 height=12） ----
        log_frame = ttk.LabelFrame(self.root, text="4. 处理日志", padding=10)
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=12, wrap="word")
        self.log_text.pack(fill="both", expand=True)

        # ---- 状态栏 ----
        self.status_var = tk.StringVar(value="就绪")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief="sunken")
        status_bar.pack(fill="x", padx=10, pady=5)

        self._print_startup_diag()

    def _print_startup_diag(self):
        self.log("=" * 50)
        self.log("Windows 上传/预处理助手 v3.2")
        self.log("=" * 50)
        self.log(f"程序目录: {self._app_dir}")
        self.log(f"bin 目录: {self._bin_dir}")
        self.log(f"bin 目录存在: {'✅' if os.path.isdir(self._bin_dir) else '❌ 不存在!'}")
        
        # 列出 bin 目录内容
        if os.path.isdir(self._bin_dir):
            bin_files = os.listdir(self._bin_dir)
            self.log(f"bin 目录内容: {bin_files}")
        else:
            self.log("⚠️ bin 目录不存在！请确认解压后 bin/ 文件夹在 EXE 同级目录")
            # 尝试在上级、_internal 等位置查找
            for alt in [
                os.path.join(self._app_dir, '_internal', 'bin'),
                os.path.join(os.path.dirname(self._app_dir), 'bin'),
                self._app_dir,
            ]:
                if os.path.isdir(alt):
                    alt_files = [f for f in os.listdir(alt) if 'ff' in f.lower()]
                    if alt_files:
                        self.log(f"  → 在 {alt} 找到: {alt_files}")
        
        self.log(f"ffmpeg: {'✅' if self._ffmpeg_exists else '❌'} {self._ffmpeg_path}")
        self.log(f"ffprobe: {'✅' if self._ffprobe_exists else '❌'} {self._ffprobe_path}")
        
        # ffprobe 可执行性测试
        if self._ffprobe_exists:
            try:
                test_result = subprocess.run(
                    [self._ffprobe_path, '-version'],
                    capture_output=True, text=True, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                )
                if test_result.returncode == 0:
                    version_line = test_result.stdout.split('\n')[0] if test_result.stdout else '?'
                    self.log(f"ffprobe 自检: ✅ {version_line}")
                else:
                    self.log(f"ffprobe 自检: ❌ 返回码 {test_result.returncode}")
                    if test_result.stderr:
                        self.log(f"  stderr: {test_result.stderr[:200]}")
            except Exception as e:
                self.log(f"ffprobe 自检: ❌ 执行异常: {e}")
        else:
            self.log("⚠️ ffprobe 不可用，将无法读取视频元数据！")
            self.log("  解决方法: 确认 bin/ffprobe.exe 在程序同级目录")
        
        # 临时目录可写性测试
        import tempfile
        try:
            tmp = tempfile.gettempdir()
            test_file = os.path.join(tmp, '_v15_write_test.tmp')
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
            self.log(f"临时目录: ✅ {tmp}")
        except Exception as e:
            self.log(f"临时目录: ❌ 不可写 {e}")
        
        if not self._ffmpeg_exists or not self._ffprobe_exists:
            self.log("")
            self.log("⚠️ 素材读取功能不可用！")
            self.log("  请确认解压后目录结构如下:")
            self.log("  Windows素材上传助手_v3.2/")
            self.log("    ├── Windows素材上传助手_v3.2.exe")
            self.log("    ├── bin/")
            self.log("    │   ├── ffmpeg.exe")
            self.log("    │   └── ffprobe.exe")
            self.log("    └── _internal/")
        self.log("-" * 50)

    # ============================================================
    # 日志
    # ============================================================
    def log(self, message):
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

        for item in self.tree.get_children():
            self.tree.delete(item)
        self.video_files = []
        self.scan_count = 0

        # 目录可读性自检
        self.log(f"扫描目录: {input_dir}")
        if any(ord(c) > 127 for c in input_dir):
            self.log(f"  ⚠️ 目录路径含中文/特殊字符")
        if ' ' in input_dir:
            self.log(f"  ⚠️ 目录路径含空格")
        try:
            all_files = os.listdir(input_dir)
            self.log(f"  目录可读: ✅ ({len(all_files)} 个文件/文件夹)")
        except PermissionError:
            self.log(f"  目录可读: ❌ 权限不足")
            messagebox.showerror("错误", f"无法读取目录：权限不足\n{input_dir}")
            return
        except Exception as e:
            self.log(f"  目录可读: ❌ {e}")
            messagebox.showerror("错误", f"无法读取目录：{e}\n{input_dir}")
            return

        video_extensions = {'.mp4', '.mov', '.avi', '.mkv', '.m4v', '.MP4', '.MOV', '.AVI', '.MKV', '.M4V'}
        for root, dirs, files in os.walk(input_dir):
            for file in files:
                if Path(file).suffix.lower() in {'.mp4', '.mov', '.avi', '.mkv', '.m4v'}:
                    self.video_files.append(os.path.join(root, file))

        total = len(self.video_files)
        if total == 0:
            self.log("未找到视频文件")
            self.status_var.set("未找到视频文件")
            return

        self.log(f"找到 {total} 个视频文件，开始扫描...")
        self.status_var.set(f"正在扫描 0/{total}...")
        self.progress_var.set(0)
        self.scan_btn.config(state="disabled")

        def run_scan():
            valid = 0
            for i, file_path in enumerate(self.video_files):
                filename = os.path.basename(file_path)
                info = self.get_video_info(file_path)

                if not info:
                    vals = (filename, "-", "-", "-", "❌ 无法读取")
                    self.root.after(0, lambda v=vals: self.tree.insert("", "end", values=v))
                    self.root.after(0, lambda c=i+1, t=total: self._update_scan_progress(c, t))
                    continue

                duration = info['duration']
                if duration <= SHORT_DURATION_THRESHOLD:
                    vals = (filename, format_duration(duration),
                            f"{info['width']}x{info['height']}",
                            format_size_mb(info['size']),
                            "⏭️ 过短，已跳过")
                    self.root.after(0, lambda v=vals: self.tree.insert("", "end", values=v))
                    self.root.after(0, lambda c=i+1, t=total: self._update_scan_progress(c, t))
                    continue

                valid += 1
                vals = (filename, format_duration(duration),
                        f"{info['width']}x{info['height']}",
                        format_size_mb(info['size']),
                        "待处理")
                self.root.after(0, lambda v=vals: self.tree.insert("", "end", values=v))
                self.root.after(0, lambda c=i+1, t=total: self._update_scan_progress(c, t))

            self.root.after(0, lambda v=valid, t=total: self._scan_complete(v, t))

        threading.Thread(target=run_scan, daemon=True).start()

    def _update_scan_progress(self, current, total):
        self.status_var.set(f"正在扫描 {current}/{total}...")
        self.progress_var.set(int(current / total * 100))

    def _scan_complete(self, valid_count, total):
        skipped = total - valid_count
        self.scan_count = valid_count
        self.progress_var.set(100)
        self.status_var.set(f"扫描完成：{valid_count} 个有效，{skipped} 个跳过")
        self.log(f"扫描完成：{valid_count} 个有效文件，{skipped} 个过短/无效已跳过")
        self.scan_btn.config(state="normal")
        self.transcode_btn.config(state="normal" if valid_count > 0 else "disabled")

    # ============================================================
    # 元数据读取
    # ============================================================
    def get_video_info(self, video_path):
        global FFPROBE
        ffprobe_path = FFPROBE
        filename = os.path.basename(video_path)

        if not ffprobe_path or ffprobe_path == "ffprobe":
            self.log(f"  ❌ {filename}: ffprobe 未配置（bin/ffprobe.exe 缺失）")
            return None
        if not os.path.exists(ffprobe_path):
            self.log(f"  ❌ {filename}: ffprobe 文件不存在: {ffprobe_path}")
            return None
        if not os.path.exists(video_path):
            self.log(f"  ❌ {filename}: 视频文件不存在: {video_path}")
            return None

        if not getattr(self, '_probe_first_logged', False):
            self._probe_first_logged = True
            self.log(f"  [诊断] ffprobe={ffprobe_path}")
            self.log(f"  [诊断] 素材路径={video_path}")
            # 检查路径是否含特殊字符
            if any(ord(c) > 127 for c in video_path):
                self.log(f"  [诊断] ⚠️ 路径含非 ASCII 字符（中文/特殊字符）")
            if ' ' in video_path:
                self.log(f"  [诊断] ⚠️ 路径含空格")

        cmd = [
            ffprobe_path, '-v', 'error',
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
            if result.returncode != 0:
                err = result.stderr.strip()[:200] if result.stderr else '无错误信息'
                self.log(f"  ❌ {filename}: ffprobe 返回码 {result.returncode}: {err}")
                return None
            if not result.stdout or result.stdout.strip() == '':
                self.log(f"  ❌ {filename}: ffprobe 无输出")
                return None
            data = json.loads(result.stdout)
            streams = data.get('streams', [])
            if not streams:
                self.log(f"  ❌ {filename}: ffprobe 未检测到视频流")
                return None
            stream = streams[0]
            fmt = data.get('format', {})
            duration = float(stream.get('duration', 0))
            if duration == 0:
                # 尝试从 format 读取 duration
                duration = float(fmt.get('duration', 0))
            return {
                'width': stream.get('width', 0),
                'height': stream.get('height', 0),
                'duration': duration,
                'fps': stream.get('r_frame_rate', '0/1'),
                'codec': stream.get('codec_name', 'unknown'),
                'size': int(fmt.get('size', 0)),
            }
        except subprocess.TimeoutExpired:
            self.log(f"  ❌ {filename}: ffprobe 超时（30s）")
            return None
        except json.JSONDecodeError as e:
            self.log(f"  ❌ {filename}: ffprobe 输出 JSON 解析失败: {e}")
            return None
        except FileNotFoundError:
            self.log(f"  ❌ {filename}: ffprobe 可执行文件未找到（可能被杀毒软件拦截）")
            return None
        except PermissionError:
            self.log(f"  ❌ {filename}: ffprobe 权限不足（可能被杀毒软件阻止）")
            return None
        except OSError as e:
            self.log(f"  ❌ {filename}: 系统错误: {e}")
            return None
        except Exception as e:
            self.log(f"  ❌ {filename}: 未知错误: {type(e).__name__}: {e}")
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
            'version': '3.2',
            'created_at': datetime.now().isoformat(),
            'input_directory': self.input_dir.get(),
            'total_files': len(self.video_files),
            'processed_files': [],
            'bad_files': [],
        }

        def run_transcode():
            self.processed_count = 0
            self.bad_count = 0
            items = self.tree.get_children()

            for i, item_id in enumerate(items):
                values = self.tree.item(item_id)['values']
                filename = values[0]
                status = values[4]

                self.progress_var.set(int((i + 1) / len(items) * 100))
                self.status_var.set(f"处理中：{i+1}/{len(items)}")
                self.log(f"[{i+1}/{len(items)}] {filename}")

                if "过短" in status or "无法读取" in status:
                    self.log(f"  ⏭️ 已跳过")
                    self.bad_count += 1
                    continue

                file_path = None
                for fp in self.video_files:
                    if os.path.basename(fp) == filename:
                        file_path = fp
                        break

                if not file_path or not os.path.exists(file_path):
                    self.log(f"  ❌ 文件不存在")
                    self.bad_count += 1
                    self.update_item_status(i, "❌ 文件不存在")
                    continue

                info = self.get_video_info(file_path)
                if not info:
                    self.bad_count += 1
                    self.update_item_status(i, "❌ 无法读取")
                    continue

                if info['duration'] <= SHORT_DURATION_THRESHOLD:
                    self.bad_count += 1
                    self.update_item_status(i, f"⏭️ 跳过：{format_duration(info['duration'])}")
                    continue

                is_bad, reasons = self._check_bad_video(info)
                if is_bad:
                    self.bad_count += 1
                    self.update_item_status(i, f"❌ 坏片：{reasons[0]}")
                    continue

                proxy_filename = f"proxy_{i:04d}_{Path(file_path).stem}.mp4"
                proxy_path = os.path.join(proxy_dir, proxy_filename)

                self.log(f"  转码 720p proxy...")
                if self._transcode_one(file_path, proxy_path):
                    proxy_size = os.path.getsize(proxy_path)
                    self.log(f"  ✅ 转码成功 {proxy_size/1024/1024:.1f}MB")
                    file_hash = self.compute_file_hash(file_path)
                    manifest['processed_files'].append({
                        'index': i,
                        'original_path': file_path,
                        'original_filename': filename,
                        'original_info': info,
                        'file_hash': file_hash,
                        'proxy_path': proxy_path,
                        'proxy_filename': proxy_filename,
                        'proxy_size': proxy_size,
                        'tos_key': None,
                        'upload_status': 'pending',
                    })
                    self.update_item_status(i, "✅ 转码完成")
                    self.processed_count += 1
                else:
                    self.log(f"  ❌ 转码失败")
                    self.update_item_status(i, "❌ 转码失败")

            manifest_path = os.path.join(output_dir, 'manifest.json')
            with open(manifest_path, 'w', encoding='utf-8') as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)

            self.log(f"\n✅ 转码完成：成功 {self.processed_count}，失败 {self.bad_count}")
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
            FFMPEG, '-y', '-i', input_path,
            '-vf', f'scale={PROXY_WIDTH}:{PROXY_HEIGHT}:force_original_aspect_ratio=decrease,pad={PROXY_WIDTH}:{PROXY_HEIGHT}:(ow-iw)/2:(oh-ih)/2',
            '-c:v', 'libx264', '-preset', 'fast', '-b:v', '3M',
            '-r', str(PROXY_FPS), '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
            '-c:a', 'aac', '-b:a', '128k', output_path
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=600,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            return result.returncode == 0
        except:
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
    # 上传
    # ============================================================
    def upload_all(self):
        if not hasattr(self, 'manifest_path') or not os.path.exists(self.manifest_path):
            messagebox.showerror("错误", "请先完成转码")
            return

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

            self.log(">>> Step 1: 初始化任务...")
            task_id, task_url, tos_prefix = self._call_task_init(processed)
            if not task_id:
                self.log("❌ task/init 失败")
                self._restore_buttons()
                return

            self.task_id = task_id
            self.task_url = task_url
            self.tos_prefix = tos_prefix
            self.log(f"✅ task_id: {task_id}")

            self.log(">>> Step 2: 上传到 TOS...")
            total = len(processed)
            for i, item in enumerate(processed):
                proxy_path = item.get('proxy_path', '')
                original_filename = item.get('original_filename', os.path.basename(proxy_path))
                tos_key = f"{tos_prefix}{original_filename}"

                self.progress_var.set(int((i + 1) / total * 100))
                self.log(f"[{i+1}/{total}] {original_filename}")

                ok, err = self._upload_one(proxy_path, tos_key)
                if ok:
                    item['tos_key'] = tos_key
                    item['upload_status'] = 'uploaded'
                    self.uploaded_tos_keys.append(tos_key)
                    self.uploaded_count += 1
                    self.update_item_status(item.get('index', i), "✅ 已上传")
                else:
                    self.update_item_status(item.get('index', i), "❌ 上传失败")

            with open(self.manifest_path, 'w', encoding='utf-8') as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)

            if not self.uploaded_tos_keys:
                self.log("❌ 没有文件上传成功")
                self._restore_buttons()
                return

            self.log(f"上传完成：{self.uploaded_count}/{total}")

            self.log(">>> Step 3: 通知服务器...")
            ok, _ = self._call_notify(self.task_id, self.uploaded_tos_keys)
            if ok:
                self.log("✅ 服务器已接收")

            if self.task_url:
                self.log(">>> Step 4: 打开任务页...")
                try:
                    webbrowser.open(self.task_url)
                    self.log(f"✅ 已打开: {self.task_url}")
                except:
                    self.log(f"请手动打开: {self.task_url}")

            self.progress_var.set(100)
            self.status_var.set(f"上传完成：{self.uploaded_count}/{total}")
            self.log(f"\n✅ 全部完成！task_id: {self.task_id}")
            self._restore_buttons()

        threading.Thread(target=run_upload, daemon=True).start()

    def _restore_buttons(self):
        self.upload_btn.config(state="normal")
        self.scan_btn.config(state="normal")

    # ============================================================
    # task/init
    # ============================================================
    def _call_task_init(self, processed_files):
        if not HAS_URLLIB:
            return None, None, None

        url = f"{SERVER_URL}/api/ui/task/init"
        filenames = [f.get('original_filename', 'unknown') for f in processed_files]

        task_context = {}
        try:
            theme = self.ctx_theme.get().strip()
            if theme:
                task_context['video_theme'] = theme
            event_text = self.ctx_event.get("1.0", "end").strip()
            if event_text:
                task_context['news_event'] = event_text
        except:
            pass

        payload = json.dumps({
            "file_count": len(processed_files),
            "filenames": filenames,
            "task_context": task_context,
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
            return None, None, None
        except Exception as e:
            self.log(f"  task/init 失败: {e}")
            return None, None, None

    # ============================================================
    # notify
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
            return data.get("status") == "processing", ""
        except Exception as e:
            return False, str(e)

    # ============================================================
    # TOS 上传
    # ============================================================
    def _upload_one(self, local_path, tos_key):
        if not os.path.exists(local_path):
            return False, "文件不存在"

        try:
            from tos import TosClientV2
            ak = TOS_INGEST_AK or os.environ.get("TOS_INGEST_AK", "")
            sk = TOS_INGEST_SK or os.environ.get("TOS_INGEST_SK", "")
            if ak and sk:
                self.log(f"  云端上传中...")
                client = TosClientV2(ak=ak, sk=sk, endpoint=TOS_ENDPOINT, region=TOS_REGION)
                client.put_object_from_file(bucket=TOS_BUCKET, key=tos_key, file_path=local_path)
                return True, None
        except ImportError:
            pass
        except Exception as e:
            self.log(f"  TOS 直传失败: {e}")

        if HAS_URLLIB:
            try:
                presign_url = f"{SERVER_URL}/api/ui/upload/presign"
                with open(local_path, 'rb') as f:
                    file_data = base64.b64encode(f.read()).decode('ascii')
                payload = json.dumps({"tos_key": tos_key, "file_base64": file_data}).encode("utf-8")
                req = urllib.request.Request(presign_url, data=payload,
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=300) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                if result.get("success"):
                    return True, None
            except Exception as e:
                self.log(f"  代理上传失败: {e}")

        return False, "上传失败"


def main():
    root = tk.Tk()
    app = IngestHelperGUI(root)
    root.mainloop()


if __name__ == '__main__':
    main()
