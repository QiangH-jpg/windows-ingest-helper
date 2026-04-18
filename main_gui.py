#!/usr/bin/env python3
"""
Windows 上传/预处理助手 v3.1 — 任务语境版

v3.1 核心变更：
- 新增"任务语境（粗筛用）"输入区：活动主题/目标/重点主体/优先镜头/回避镜头
- 任务语境随 task/init 提交保存，第二层 Pro 读取用于候选池分层

v3.0 核心变更：
- 放弃 onefile 分发，改用 onedir（主EXE + bin/ffmpeg.exe + bin/ffprobe.exe）
- ffmpeg/ffprobe 只从 程序目录/bin 查找，不依赖 _MEIPASS 临时目录
- 启动时打印程序目录、bin 路径、文件存在性
- bin 缺失时明确报错"分发包缺失 ffprobe"

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


def format_duration(seconds):
    """格式化时长为中文口径：4.5秒"""
    return f"{seconds:.1f}秒"


def format_size_mb(size_bytes):
    """格式化大小：12.3MB"""
    return f"{size_bytes / 1024 / 1024:.1f}MB"


class IngestHelperGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Windows 上传/预处理助手 v3.1")
        self.root.geometry("900x700")

        # 状态变量
        self.input_dir = tk.StringVar()
        self.output_dir = tk.StringVar(value="./output")
        self.is_processing = False
        self.video_files = []
        self.processed_count = 0
        self.bad_count = 0
        self.uploaded_count = 0
        self.scan_count = 0  # 已扫描文件数

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
        """查找 ffmpeg/ffprobe 路径 — onedir 方案：只从程序目录/bin 查找"""
        global FFMPEG, FFPROBE

        # 【onedir 核心规则】无论是否 frozen，程序目录 = exe 所在目录
        # --onedir 模式下：sys.executable 就是 <程序目录>/主程序.exe
        # 源码开发模式下：__file__ 所在目录即程序目录
        if getattr(sys, 'frozen', False):
            app_dir = os.path.dirname(os.path.abspath(sys.executable))
        else:
            app_dir = os.path.dirname(os.path.abspath(__file__))

        bin_dir = os.path.join(app_dir, "bin")

        # 只在 bin/ 目录下查找，不兜底系统 PATH
        ffmpeg_path = os.path.join(bin_dir, "ffmpeg.exe")
        ffprobe_path = os.path.join(bin_dir, "ffprobe.exe")

        self._bin_dir = bin_dir
        self._app_dir = app_dir

        if os.path.exists(ffmpeg_path):
            FFMPEG = ffmpeg_path
        if os.path.exists(ffprobe_path):
            FFPROBE = ffprobe_path

        # 记录解析结果（setup_ui 中打印到日志）
        self._ffmpeg_resolved = True
        self._ffmpeg_path = FFMPEG
        self._ffprobe_path = FFPROBE
        self._ffmpeg_exists = os.path.exists(FFMPEG) if FFMPEG else False
        self._ffprobe_exists = os.path.exists(FFPROBE) if FFPROBE else False

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

        # 任务语境区
        ctx_frame = ttk.LabelFrame(self.root, text="1.5 任务语境（粗筛用）", padding=10)
        ctx_frame.pack(fill="x", padx=10, pady=5)

        self.ctx_theme = tk.StringVar()
        self.ctx_target = tk.StringVar(value="1分钟以内新闻短视频")
        self.ctx_subjects = tk.StringVar()
        self.ctx_preferred = tk.StringVar()
        self.ctx_avoid = tk.StringVar(value="纯空镜, 重复角度")

        ttk.Label(ctx_frame, text="活动主题:").grid(row=0, column=0, sticky="w")
        ttk.Entry(ctx_frame, textvariable=self.ctx_theme, width=70).grid(row=0, column=1, padx=5, columnspan=3, sticky="ew")

        ttk.Label(ctx_frame, text="目标输出:").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(ctx_frame, textvariable=self.ctx_target, width=30).grid(row=1, column=1, padx=5, sticky="w")
        ttk.Label(ctx_frame, text="重点主体:").grid(row=1, column=2, sticky="w")
        ttk.Entry(ctx_frame, textvariable=self.ctx_subjects, width=30).grid(row=1, column=3, padx=5, sticky="w")

        ttk.Label(ctx_frame, text="优先镜头:").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(ctx_frame, textvariable=self.ctx_preferred, width=30).grid(row=2, column=1, padx=5, sticky="w")
        ttk.Label(ctx_frame, text="回避镜头:").grid(row=2, column=2, sticky="w")
        ttk.Entry(ctx_frame, textvariable=self.ctx_avoid, width=30).grid(row=2, column=3, padx=5, sticky="w")

        ctx_frame.columnconfigure(1, weight=1)
        ctx_frame.columnconfigure(3, weight=1)

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
        self.tree.column("status", width=180)

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

        # 启动诊断日志
        self._print_startup_diag()

    def _print_startup_diag(self):
        """启动时打印程序目录、bin 目录、ffprobe 路径"""
        self.log("=" * 50)
        self.log("Windows 上传/预处理助手 v3.0 — onedir 正式版")
        self.log("=" * 50)
        self.log(f"程序目录: {self._app_dir}")
        self.log(f"bin 目录: {self._bin_dir}")
        self.log(f"ffmpeg 路径: {self._ffmpeg_path}")
        self.log(f"ffmpeg 存在: {'✅ 是' if self._ffmpeg_exists else '❌ 否'}")
        self.log(f"ffprobe 路径: {self._ffprobe_path}")
        self.log(f"ffprobe 存在: {'✅ 是' if self._ffprobe_exists else '❌ 否'}")
        if not self._ffmpeg_exists or not self._ffprobe_exists:
            self.log("⚠️ 分发包缺失 ffmpeg/ffprobe，请将 bin/ 目录放在程序同级位置")
        else:
            self.log("✅ ffmpeg/ffprobe 已就绪")
        self.log("-" * 50)

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
    # 扫描（后台线程 + 逐条刷新 + 短片初筛）
    # ============================================================
    def scan_videos(self):
        input_dir = self.input_dir.get()
        if not input_dir or not os.path.exists(input_dir):
            messagebox.showerror("错误", "请先选择有效的素材目录")
            return

        # 清空列表
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.video_files = []
        self.scan_count = 0

        # 收集文件
        video_extensions = {'.mp4', '.mov', '.avi', '.mkv', '.m4v'}
        for root, dirs, files in os.walk(input_dir):
            for file in files:
                if Path(file).suffix.lower() in video_extensions:
                    file_path = os.path.join(root, file)
                    self.video_files.append(file_path)

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
                    # 无法读取
                    vals = (filename, "-", "-", "-", "❌ 无法读取")
                    self.root.after(0, lambda v=vals: self.tree.insert("", "end", values=v))
                    self.log(f"  ❌ {filename}：无法读取元数据")
                    self.root.after(0, lambda c=i+1, t=total: self._update_scan_progress(c, t))
                    continue

                duration = info['duration']

                # 短片筛除
                if duration <= SHORT_DURATION_THRESHOLD:
                    vals = (filename, format_duration(duration),
                            f"{info['width']}x{info['height']}",
                            format_size_mb(info['size']),
                            "⏭️ 过短，已跳过")
                    self.root.after(0, lambda v=vals: self.tree.insert("", "end", values=v))
                    self.log(f"  ⏭️ {filename}：时长过短（{format_duration(duration)}）")
                    self.root.after(0, lambda c=i+1, t=total: self._update_scan_progress(c, t))
                    continue

                # 正常视频 → 逐条插入
                valid += 1
                vals = (filename, format_duration(duration),
                        f"{info['width']}x{info['height']}",
                        format_size_mb(info['size']),
                        "待处理")
                self.root.after(0, lambda v=vals: self.tree.insert("", "end", values=v))
                self.log(f"  ✅ {filename}：{format_duration(duration)}，{info['width']}x{info['height']}")
                self.root.after(0, lambda c=i+1, t=total: self._update_scan_progress(c, t))

            # 扫描完成
            skipped = total - valid
            self.root.after(0, lambda v=valid, t=total: self._scan_complete(v, t))

        threading.Thread(target=run_scan, daemon=True).start()

    def _update_scan_progress(self, current, total):
        """更新扫描进度（在主线程调用）"""
        self.status_var.set(f"正在扫描 {current}/{total}...")
        self.progress_var.set(int(current / total * 100))

    def _scan_complete(self, valid_count, total):
        """扫描完成回调"""
        skipped = total - valid_count
        self.scan_count = valid_count
        self.progress_var.set(100)
        self.status_var.set(f"扫描完成：{valid_count} 个有效，{skipped} 个跳过")
        self.log(f"扫描完成：{valid_count} 个有效文件，{skipped} 个过短/无效文件已跳过")
        self.scan_btn.config(state="normal")
        self.transcode_btn.config(state="normal" if valid_count > 0 else "disabled")

    # ============================================================
    # 元数据读取（onedir 方案：只从 bin/ 读取 ffprobe）
    # ============================================================
    def get_video_info(self, video_path):
        global FFPROBE
        ffprobe_path = FFPROBE

        # 【关键】如果 ffprobe 不在 bin 目录下，直接报错
        if not ffprobe_path or ffprobe_path == "ffprobe":
            self.log(f"  ❌ ffprobe 未找到 — 请确认 bin/ffprobe.exe 在程序目录下")
            return None

        ffprobe_exists = os.path.exists(ffprobe_path)
        if not ffprobe_exists:
            self.log(f"  ❌ ffprobe 文件不存在: {ffprobe_path}")
            return None

        if not os.path.exists(video_path):
            self.log(f"  ❌ 视频文件不存在: {video_path}")
            return None

        # 首次调用打印完整诊断信息
        if not getattr(self, '_probe_first_logged', False):
            self._probe_first_logged = True
            self.log(f"  [诊断] ffprobe={ffprobe_path}")

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

            if not getattr(self, '_probe_first_done', False):
                self._probe_first_done = True
                self.log(f"  [诊断] returncode={result.returncode}")
                self.log(f"  [诊断] stdout={repr(result.stdout[:500] if result.stdout else 'None')}")
                self.log(f"  [诊断] stderr={repr(result.stderr[:500] if result.stderr else 'None')}")

            if result.stdout is None or result.stdout.strip() == '':
                err_msg = result.stderr.strip() if result.stderr else '无输出'
                self.log(f"  ❌ {os.path.basename(video_path)}: ffprobe 返回空 (exit {result.returncode})")
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
        except json.JSONDecodeError as e:
            self.log(f"  ❌ {os.path.basename(video_path)}: JSON 解析失败: {e}")
            return None
        except subprocess.TimeoutExpired:
            self.log(f"  ❌ {os.path.basename(video_path)}: ffprobe 超时")
            return None
        except Exception as e:
            self.log(f"  ❌ {os.path.basename(video_path)}: {type(e).__name__}: {e}")
            return None

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
            'version': '2.1',
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

            # 从 Treeview 获取所有已扫描项
            items = self.tree.get_children()
            for i, item_id in enumerate(items):
                values = self.tree.item(item_id)['values']
                filename = values[0]
                status = values[4]

                progress = int((i + 1) / len(items) * 100)
                self.progress_var.set(progress)
                self.status_var.set(f"处理中：{i+1}/{len(items)}")

                self.log(f"[{i+1}/{len(items)}] {filename}")

                # 过短/无效文件跳过
                if "过短" in status or "无法读取" in status:
                    self.log(f"  ⏭️ 已跳过")
                    manifest['bad_files'].append({
                        'original_path': '',
                        'original_filename': filename,
                        'reasons': [status],
                        'status': 'skipped_scan'
                    })
                    self.bad_count += 1
                    continue

                # 查找原始文件路径
                file_path = None
                for fp in self.video_files:
                    if os.path.basename(fp) == filename:
                        file_path = fp
                        break

                if not file_path or not os.path.exists(file_path):
                    self.log(f"  ❌ 文件不存在")
                    manifest['bad_files'].append({
                        'original_path': '',
                        'original_filename': filename,
                        'reason': '文件不存在',
                        'status': 'bad'
                    })
                    self.bad_count += 1
                    self.update_item_status(i, "❌ 文件不存在")
                    continue

                info = self.get_video_info(file_path)
                if not info:
                    self.log(f"  ❌ 无法读取元数据")
                    manifest['bad_files'].append({
                        'original_path': file_path,
                        'original_filename': filename,
                        'reason': '无法读取元数据',
                        'status': 'bad'
                    })
                    self.bad_count += 1
                    self.update_item_status(i, "❌ 无法读取")
                    continue

                # 短片二次筛除
                if info['duration'] <= SHORT_DURATION_THRESHOLD:
                    self.log(f"  ⏭️ 跳过短片 ({format_duration(info['duration'])})")
                    manifest['bad_files'].append({
                        'original_path': file_path,
                        'original_filename': filename,
                        'original_info': info,
                        'reasons': [f"时长过短 ({format_duration(info['duration'])})"],
                        'status': 'skipped_short'
                    })
                    self.bad_count += 1
                    self.update_item_status(i, f"⏭️ 跳过：{format_duration(info['duration'])}")
                    continue

                # 坏片检测
                is_bad, reasons = self._check_bad_video(info)
                if is_bad:
                    self.log(f"  ❌ 坏片：{', '.join(reasons)}")
                    manifest['bad_files'].append({
                        'original_path': file_path,
                        'original_filename': filename,
                        'original_info': info,
                        'reasons': reasons,
                        'status': 'bad'
                    })
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
                    'total_files': len(items),
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
                self.log("❌ task/init 失败，终止上传")
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
                self.log("❌ 没有文件上传成功，终止")
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
            self.log(f"任务已提交，正在打开云端工作台...")
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
            self.log("  ❌ urllib 不可用")
            return None, None, None

        url = f"{SERVER_URL}/api/ui/task/init"
        filenames = [f.get('original_filename', 'unknown') for f in processed_files]
        
        # 构造任务语境
        task_context = {}
        try:
            theme = self.ctx_theme.get().strip()
            if theme:
                task_context['activity_theme'] = theme
            target = self.ctx_target.get().strip()
            if target:
                task_context['target_output'] = target
            subjects = self.ctx_subjects.get().strip()
            if subjects:
                task_context['key_subjects'] = [s.strip() for s in subjects.split(',') if s.strip()]
            preferred = self.ctx_preferred.get().strip()
            if preferred:
                task_context['preferred_shots'] = [s.strip() for s in preferred.split(',') if s.strip()]
            avoid = self.ctx_avoid.get().strip()
            if avoid:
                task_context['avoid_shots'] = [s.strip() for s in avoid.split(',') if s.strip()]
        except Exception:
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
    # 主链：TOS SDK 直传（tos 包已随 PyInstaller 打包）
    # 兜底：服务器代理上传（小文件可用）
    # ============================================================
    def _upload_one(self, local_path, tos_key):
        """
        上传单个文件到 TOS。

        主链：TOS SDK 直传（tos 包随 EXE 打包，内置 AK/SK）
        兜底：服务器代理上传（/api/ui/upload/presign）
        """
        if not os.path.exists(local_path):
            return False, f"文件不存在: {local_path}"

        # --- 主链: TOS SDK 直传 ---
        try:
            from tos import TosClientV2
            # 内置凭据（随 EXE 打包）
            ak = TOS_INGEST_AK or os.environ.get("TOS_INGEST_AK", "")
            sk = TOS_INGEST_SK or os.environ.get("TOS_INGEST_SK", "")
            if ak and sk:
                self.log(f"  云端上传中...")
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
            else:
                self.log(f"  通过服务器代理上传...")
        except ImportError:
            self.log(f"  通过服务器代理上传...")
        except Exception as e:
            self.log(f"  TOS 直传失败，切换服务器代理: {e}")
            self.log(f"  通过服务器代理上传...")

        # --- 兜底: 服务器代理上传 ---
        if HAS_URLLIB:
            try:
                presign_url = f"{SERVER_URL}/api/ui/upload/presign"
                with open(local_path, 'rb') as f:
                    file_data = base64.b64encode(f.read()).decode('ascii')

                payload = json.dumps({
                    "tos_key": tos_key,
                    "file_base64": file_data,
                }).encode("utf-8")

                presign_req = urllib.request.Request(
                    presign_url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(presign_req, timeout=300) as resp:
                    result = json.loads(resp.read().decode("utf-8"))

                if result.get("success"):
                    return True, None
                else:
                    return False, result.get("error", "unknown")
            except Exception as e:
                self.log(f"  服务器代理上传失败: {e}")

        return False, "TOS 上传失败"


def main():
    root = tk.Tk()
    app = IngestHelperGUI(root)
    root.mainloop()


if __name__ == '__main__':
    main()
