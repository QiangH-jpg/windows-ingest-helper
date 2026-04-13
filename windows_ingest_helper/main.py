# -*- coding: utf-8 -*-
"""
Windows Ingest Helper v12
- 本地素材扫描（读取元数据）
- 720p proxy 转码（异步执行 + 详细硬日志 + 超时机制）
- 本地 TOS 上传
- 修复：批量转码无输出文件问题
"""

import os
import sys
import json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import subprocess
import hashlib
import threading
import time
from datetime import datetime

# TOS SDK
try:
    from tos import TosClientV2
    TOS_AVAILABLE = True
except ImportError:
    TOS_AVAILABLE = False

VERSION = "v12"
BUILD_TIME = "2026-04-13T11:30:00+08:00"
TRANSCODE_TIMEOUT = 300  # 单文件超时 5 分钟

CONFIG_FILE = "config.json"
MANIFEST_FILE = "manifest.json"

DEFAULT_CONFIG = {
    "tos_ak": "",
    "tos_sk": "",
    "bucket": "e23-video",
    "region": "cn-beijing",
    "endpoint": "tos-cn-beijing.volces.com"
}


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                for key, value in DEFAULT_CONFIG.items():
                    if key not in config:
                        config[key] = value
                return config
        except Exception as e:
            print(f"读取配置文件失败：{e}")
    return DEFAULT_CONFIG.copy()


def save_config(config):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"保存配置文件失败：{e}")
        return False


def get_ffmpeg_path():
    """获取 ffmpeg 路径"""
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(sys.executable)
    else:
        exe_dir = os.path.dirname(os.path.abspath(__file__))
    
    bin_dir = os.path.join(exe_dir, 'bin')
    ffmpeg_path = os.path.join(bin_dir, 'ffmpeg.exe')
    ffprobe_path = os.path.join(bin_dir, 'ffprobe.exe')
    
    if os.path.exists(ffmpeg_path) and os.path.exists(ffprobe_path):
        return ffmpeg_path, ffprobe_path
    return None, None


def get_video_metadata(video_path, ffprobe_exe):
    """读取视频元数据"""
    try:
        file_size = os.path.getsize(video_path)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        cmd = [ffprobe_exe, '-v', 'quiet', '-print_format', 'json', '-show_streams', '-show_format', video_path]
        result = subprocess.run(cmd, capture_output=True, text=True, startupinfo=startupinfo, timeout=30)
        
        if result.returncode != 0:
            return None
        
        data = json.loads(result.stdout)
        video_stream = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), None)
        if not video_stream:
            return None
        
        return {
            'duration': float(data.get('format', {}).get('duration', 0)),
            'width': video_stream.get('width', 0),
            'height': video_stream.get('height', 0),
            'file_size': file_size,
            'codec': video_stream.get('codec_name', 'unknown')
        }
    except Exception as e:
        print(f"读取元数据失败：{e}")
        return None


def format_file_size(size_bytes):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f}{unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f}TB"


def format_duration(seconds):
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{mins}:{secs:02d}"


def upload_to_tos(file_path, tos_key, config):
    if not TOS_AVAILABLE:
        return False, "TOS SDK 未安装"
    try:
        endpoint = f"https://{config['endpoint']}"
        client = TosClientV2(ak=config['tos_ak'], sk=config['tos_sk'], endpoint=endpoint, region=config['region'])
        client.put_object_from_file(bucket=config['bucket'], key=tos_key, file_path=file_path)
        return True, f"https://{config['bucket']}.{config['endpoint']}/{tos_key}"
    except Exception as e:
        return False, str(e)


class IngestHelperApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"Windows 上传/预处理助手 {VERSION}")
        self.root.geometry("1000x750")
        
        self.config = load_config()
        self.source_dir = ""
        self.output_dir = ""
        self.proxy_files = []
        self.transcode_running = False
        self.transcode_thread = None
        
        self.create_widgets()
        self.update_config_status()
    
    def create_widgets(self):
        toolbar = ttk.Frame(self.root)
        toolbar.pack(fill='x', padx=10, pady=10)
        ttk.Label(toolbar, text=f"Windows 上传/预处理助手 {VERSION}", font=('Arial', 14, 'bold')).pack(side='left')
        ttk.Label(toolbar, text=f"Build: {BUILD_TIME}", font=('Arial', 8)).pack(side='left', padx=10)
        ttk.Button(toolbar, text="上传配置", command=self.show_config_dialog).pack(side='right')
        
        self.config_status_var = tk.StringVar(value="⚠️ 未配置 TOS 上传凭据")
        ttk.Label(self.root, textvariable=self.config_status_var, foreground='red').pack(anchor='ne', padx=10)
        
        dir_frame = ttk.LabelFrame(self.root, text="目录设置", padding=10)
        dir_frame.pack(fill='x', padx=10, pady=5)
        
        source_frame = ttk.Frame(dir_frame)
        source_frame.pack(fill='x', pady=5)
        ttk.Label(source_frame, text="源目录:", width=10).pack(side='left')
        self.source_var = tk.StringVar()
        ttk.Entry(source_frame, textvariable=self.source_var, width=60).pack(side='left', padx=5)
        ttk.Button(source_frame, text="浏览", command=self.browse_source).pack(side='left')
        
        output_frame = ttk.Frame(dir_frame)
        output_frame.pack(fill='x', pady=5)
        ttk.Label(output_frame, text="输出目录:", width=10).pack(side='left')
        self.output_var = tk.StringVar()
        ttk.Entry(output_frame, textvariable=self.output_var, width=60).pack(side='left', padx=5)
        ttk.Button(output_frame, text="浏览", command=self.browse_output).pack(side='left')
        
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill='x', padx=10, pady=10)
        
        self.scan_btn = ttk.Button(btn_frame, text="1. 扫描素材", command=self.scan_videos)
        self.scan_btn.pack(side='left', padx=5)
        
        self.transcode_btn = ttk.Button(btn_frame, text="2. 批量转码", command=self.start_batch_transcode)
        self.transcode_btn.pack(side='left', padx=5)
        
        self.stop_btn = ttk.Button(btn_frame, text="停止转码", command=self.stop_transcode, state='disabled')
        self.stop_btn.pack(side='left', padx=5)
        
        self.upload_btn = ttk.Button(btn_frame, text="3. 上传 TOS", command=self.upload_tos)
        self.upload_btn.pack(side='left', padx=5)
        
        self.save_btn = ttk.Button(btn_frame, text="4. 保存清单", command=self.save_manifest)
        self.save_btn.pack(side='left', padx=5)
        
        list_frame = ttk.LabelFrame(self.root, text="素材列表", padding=10)
        list_frame.pack(fill='both', expand=True, padx=10, pady=5)
        
        columns = ('filename', 'duration', 'resolution', 'size', 'status')
        self.tree = ttk.Treeview(list_frame, columns=columns, show='headings', height=15)
        self.tree.heading('filename', text='文件名')
        self.tree.heading('duration', text='时长')
        self.tree.heading('resolution', text='分辨率')
        self.tree.heading('size', text='大小')
        self.tree.heading('status', text='状态')
        self.tree.column('filename', width=300)
        self.tree.column('duration', width=80)
        self.tree.column('resolution', width=100)
        self.tree.column('size', width=100)
        self.tree.column('status', width=150)
        
        scrollbar = ttk.Scrollbar(list_frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        
        log_frame = ttk.LabelFrame(self.root, text="进度日志", padding=10)
        log_frame.pack(fill='x', padx=10, pady=5)
        self.progress_text = tk.Text(log_frame, height=8, wrap='word')
        self.progress_text.pack(fill='both', expand=True)
        log_scrollbar = ttk.Scrollbar(self.progress_text, command=self.progress_text.yview)
        log_scrollbar.pack(side='right', fill='y')
        self.progress_text.config(yscrollcommand=log_scrollbar.set)
        
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status_var, relief='sunken').pack(fill='x', side='bottom')
    
    def update_config_status(self):
        if self.config['tos_ak'] and self.config['tos_sk']:
            self.config_status_var.set(f"✅ TOS 配置：{self.config['bucket']} @ {self.config['region']}")
        else:
            self.config_status_var.set("⚠️ 未配置 TOS 上传凭据")
    
    def show_config_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("TOS 上传配置")
        dialog.geometry("500x400")
        dialog.transient(self.root)
        dialog.grab_set()
        
        ttk.Label(dialog, text="TOS 上传配置", font=('Arial', 14, 'bold')).pack(pady=10)
        form_frame = ttk.Frame(dialog, padding=20)
        form_frame.pack(fill='both', expand=True)
        
        ttk.Label(form_frame, text="Access Key (AK):").grid(row=0, column=0, sticky='w', pady=5)
        ak_var = tk.StringVar(value=self.config.get('tos_ak', ''))
        ttk.Entry(form_frame, textvariable=ak_var, width=50).grid(row=0, column=1, pady=5)
        
        ttk.Label(form_frame, text="Secret Key (SK):").grid(row=1, column=0, sticky='w', pady=5)
        sk_var = tk.StringVar(value=self.config.get('tos_sk', ''))
        ttk.Entry(form_frame, textvariable=sk_var, width=50, show='*').grid(row=1, column=1, pady=5)
        
        ttk.Label(form_frame, text="Bucket:").grid(row=2, column=0, sticky='w', pady=5)
        bucket_var = tk.StringVar(value=self.config.get('bucket', 'e23-video'))
        ttk.Entry(form_frame, textvariable=bucket_var, width=50).grid(row=2, column=1, pady=5)
        
        ttk.Label(form_frame, text="Region:").grid(row=3, column=0, sticky='w', pady=5)
        region_var = tk.StringVar(value=self.config.get('region', 'cn-beijing'))
        ttk.Entry(form_frame, textvariable=region_var, width=50).grid(row=3, column=1, pady=5)
        
        ttk.Label(form_frame, text="Endpoint:").grid(row=4, column=0, sticky='w', pady=5)
        endpoint_var = tk.StringVar(value=self.config.get('endpoint', 'tos-cn-beijing.volces.com'))
        ttk.Entry(form_frame, textvariable=endpoint_var, width=50).grid(row=4, column=1, pady=5)
        
        def on_save():
            new_config = {'tos_ak': ak_var.get(), 'tos_sk': sk_var.get(), 'bucket': bucket_var.get(), 'region': region_var.get(), 'endpoint': endpoint_var.get()}
            if save_config(new_config):
                self.config = new_config
                self.update_config_status()
                messagebox.showinfo("成功", "配置已保存")
                dialog.destroy()
        
        ttk.Button(dialog, text="保存配置", command=on_save).pack(pady=10)
    
    def browse_source(self):
        directory = filedialog.askdirectory()
        if directory:
            self.source_var.set(directory)
            self.source_dir = directory
    
    def browse_output(self):
        directory = filedialog.askdirectory()
        if directory:
            self.output_var.set(directory)
            self.output_dir = directory
    
    def log(self, message):
        """日志输出（线程安全）"""
        def _log():
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.progress_text.insert('end', f"[{timestamp}] {message}\n")
            self.progress_text.see('end')
        if threading.current_thread() is threading.main_thread():
            _log()
        else:
            self.root.after(0, _log)
    
    def update_tree_status(self, index, status):
        """更新列表状态（线程安全）"""
        def _update():
            if 0 <= index < len(self.proxy_files):
                item = self.proxy_files[index]
                item['status'] = status
                values = (item['filename'], item.get('duration_fmt', ''), item.get('resolution', ''), item.get('size_fmt', ''), status)
                self.tree.item(index, values=values)
        if threading.current_thread() is threading.main_thread():
            _update()
        else:
            self.root.after(0, _update)
    
    def scan_videos(self):
        if not self.source_dir:
            messagebox.showwarning("警告", "请先选择源目录")
            return
        
        ffmpeg_exe, ffprobe_exe = get_ffmpeg_path()
        if not ffprobe_exe:
            messagebox.showerror("错误", "未找到 ffprobe")
            return
        
        self.log("=" * 60)
        self.log("开始扫描素材（读取元数据）")
        self.log("=" * 60)
        
        video_extensions = ('.mp4', '.mov', '.avi', '.mkv', '.flv')
        self.proxy_files = []
        
        for item in self.tree.get_children():
            self.tree.delete(item)
        
        for root, dirs, files in os.walk(self.source_dir):
            for file in files:
                if file.lower().endswith(video_extensions):
                    full_path = os.path.join(root, file)
                    metadata = get_video_metadata(full_path, ffprobe_exe)
                    
                    if metadata:
                        duration_fmt = format_duration(metadata['duration'])
                        resolution = f"{metadata['width']}x{metadata['height']}"
                        size_fmt = format_file_size(metadata['file_size'])
                        status = "已读取元数据"
                    else:
                        duration_fmt = resolution = size_fmt = ""
                        status = "无法读取元数据"
                    
                    item = {'source_path': full_path, 'filename': file, 'proxy_path': '', 'status': status, 'duration_fmt': duration_fmt, 'resolution': resolution, 'size_fmt': size_fmt}
                    self.proxy_files.append(item)
                    self.tree.insert('', 'end', values=(file, duration_fmt, resolution, size_fmt, status))
        
        self.log(f"扫描完成：共 {len(self.proxy_files)} 个视频文件")
        self.status_var.set(f"已扫描 {len(self.proxy_files)} 个视频")
    
    def start_batch_transcode(self):
        """启动批量转码"""
        if not self.proxy_files:
            messagebox.showwarning("警告", "请先扫描素材")
            return
        
        if not self.output_dir:
            messagebox.showwarning("警告", "请先选择输出目录")
            return
        
        ffmpeg_exe, ffprobe_exe = get_ffmpeg_path()
        if not ffmpeg_exe:
            messagebox.showerror("错误", "未找到 ffmpeg")
            return
        
        # 确保输出目录存在
        os.makedirs(self.output_dir, exist_ok=True)
        
        self.transcode_running = True
        self.stop_btn.config(state='normal')
        self.transcode_btn.config(state='disabled')
        
        # 启动异步转码线程
        self.transcode_thread = threading.Thread(target=self._batch_transcode_worker, args=(ffmpeg_exe,), daemon=True)
        self.transcode_thread.start()
        
        # 启动 UI 刷新循环
        self._refresh_ui_loop()
    
    def _refresh_ui_loop(self):
        if self.transcode_running and self.transcode_thread and self.transcode_thread.is_alive():
            self.root.after(100, self._refresh_ui_loop)
    
    def _batch_transcode_worker(self, ffmpeg_exe):
        """转码工作线程（带详细硬日志）"""
        try:
            total = len(self.proxy_files)
            success_count = 0
            fail_count = 0
            
            self.log("=" * 60)
            self.log("[批量转码入口] 已进入")
            self.log(f"[任务总数] {total}")
            self.log(f"[输出目录] {self.output_dir}")
            self.log(f"[超时时间] {TRANSCODE_TIMEOUT}秒/文件")
            self.log("=" * 60)
            
            for i, item in enumerate(self.proxy_files):
                if not self.transcode_running:
                    self.log("⚠️ 用户停止转码")
                    break
                
                filename = item['filename']
                base_name = os.path.splitext(filename)[0]
                proxy_filename = f"{base_name}_720p.mp4"
                proxy_path = os.path.join(self.output_dir, proxy_filename)
                
                self.log(f"[{i+1}/{total}] 开始转码：{filename}")
                self.log(f"[{i+1}/{total}] 输出路径：{proxy_path}")
                self.update_tree_status(i, "转码中...")
                
                start_time = time.time()
                success, result = self._transcode_single(item['source_path'], proxy_path, ffmpeg_exe, filename, start_time)
                elapsed = time.time() - start_time
                
                if not self.transcode_running:
                    break
                
                if success:
                    item['proxy_path'] = proxy_path
                    item['proxy_size'] = result
                    success_count += 1
                    size_str = format_file_size(result)
                    self.update_tree_status(i, f"✅ 成功 ({size_str})")
                    self.log(f"[{i+1}/{total}] ✅ 转码成功：{size_str} (耗时{elapsed:.1f}秒)")
                else:
                    fail_count += 1
                    self.update_tree_status(i, f"❌ 失败")
                    self.log(f"[{i+1}/{total}] ❌ 转码失败：{result}")
            
            self.log("=" * 60)
            self.log(f"转码完成：成功 {success_count}/{total}, 失败 {fail_count}")
            self.status_var.set(f"转码完成：{success_count}/{total}")
            
        except Exception as e:
            import traceback
            error_trace = traceback.format_exc()
            self.log("=" * 60)
            self.log(f"❌ 转码线程异常：{e}")
            self.log(error_trace)
            self.log("=" * 60)
        
        finally:
            def _finish():
                self.transcode_running = False
                self.stop_btn.config(state='disabled')
                self.transcode_btn.config(state='normal')
                if success_count > 0:
                    messagebox.showinfo("成功", f"转码完成：\n成功 {success_count}/{total}\n失败 {fail_count}")
            self.root.after(0, _finish)
    
    def _transcode_single(self, video_path, output_path, ffmpeg_exe, filename, start_time):
        """转码单个文件（带超时）"""
        try:
            # 构建 ffmpeg 命令
            cmd = [
                ffmpeg_exe, '-i', video_path,
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '28',
                '-vf', 'scale=1280:720:force_original_aspect_ratio=decrease',
                '-c:a', 'aac', '-b:a', '128k', '-y', output_path
            ]
            
            self.log(f"  [ffmpeg 命令] {' '.join(cmd[:3])} ... (完整命令共{len(cmd)}个参数)")
            
            # Windows 特有：隐藏控制台窗口
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = subprocess.CREATE_NO_WINDOW
            
            # 启动 subprocess
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                startupinfo=startupinfo, creationflags=creationflags
            )
            self.log(f"  [subprocess] 已启动 (PID: {process.pid})")
            
            # 等待完成，带超时
            last_heartbeat = start_time
            while process.poll() is None:
                if not self.transcode_running:
                    process.terminate()
                    return False, "用户停止"
                
                elapsed = time.time() - start_time
                if elapsed > TRANSCODE_TIMEOUT:
                    process.terminate()
                    return False, f"超时 ({elapsed:.0f}秒 > {TRANSCODE_TIMEOUT}秒)"
                
                # 心跳日志
                now = time.time()
                if now - last_heartbeat >= 30:
                    self.log(f"  ⏳ 仍在转码：{filename} (已耗时{elapsed:.0f}秒)")
                    last_heartbeat = now
                
                time.sleep(1)
            
            # 检查结果
            if process.returncode == 0 and os.path.exists(output_path):
                output_size = os.path.getsize(output_path)
                self.log(f"  [输出检测] 文件已生成 ({format_file_size(output_size)})")
                return True, output_size
            else:
                stderr = process.stderr.read().decode('utf-8', errors='ignore')[:200]
                self.log(f"  [输出检测] 文件未生成")
                self.log(f"  [ffmpeg 错误] {stderr}")
                return False, stderr or f"返回码：{process.returncode}"
                
        except FileNotFoundError as e:
            return False, f"ffmpeg 未找到：{e}"
        except Exception as e:
            import traceback
            return False, f"异常：{e}\n{traceback.format_exc()[:200]}"
    
    def stop_transcode(self):
        self.transcode_running = False
        self.log("正在停止转码...")
    
    def upload_tos(self):
        if not self.proxy_files:
            messagebox.showwarning("警告", "请先扫描素材")
            return
        if not self.config['tos_ak'] or not self.config['tos_sk']:
            messagebox.showerror("错误", '未配置本地 TOS 上传凭据！')
            return
        if not TOS_AVAILABLE:
            messagebox.showerror("错误", "TOS SDK 未安装")
            return
        
        self.log("=" * 60)
        self.log("开始上传 TOS")
        self.log("=" * 60)
        
        upload_count = 0
        for i, item in enumerate(self.proxy_files):
            if not item.get('proxy_path') or not os.path.exists(item['proxy_path']):
                continue
            
            self.update_tree_status(i, "上传中...")
            self.log(f"上传：{item['filename']}")
            
            file_hash = hashlib.md5(open(item['proxy_path'], 'rb').read()).hexdigest()[:8]
            tos_key = f"windows_ingest/{datetime.now().strftime('%Y%m%d')}/{file_hash}_{os.path.basename(item['proxy_path'])}"
            success, result = upload_to_tos(item['proxy_path'], tos_key, self.config)
            
            if success:
                item['tos_key'] = tos_key
                item['tos_url'] = result
                item['upload_status'] = 'completed'
                upload_count += 1
                self.update_tree_status(i, "✅ 已上传")
                self.log(f"  ✅ {result}")
            else:
                self.update_tree_status(i, "❌ 上传失败")
                self.log(f"  ❌ {result}")
        
        self.log(f"上传完成：{upload_count} 个文件")
        self.status_var.set(f"TOS 上传完成：{upload_count}")
        if upload_count > 0:
            messagebox.showinfo("成功", f"成功上传 {upload_count} 个文件到 TOS")
    
    def save_manifest(self):
        if not self.output_dir:
            messagebox.showwarning("警告", "请先选择输出目录")
            return
        
        self.manifest['files'] = self.proxy_files
        self.manifest['created_at'] = datetime.now().isoformat()
        self.manifest['config'] = {'bucket': self.config['bucket'], 'region': self.config['region']}
        
        manifest_path = os.path.join(self.output_dir, MANIFEST_FILE)
        try:
            with open(manifest_path, 'w', encoding='utf-8') as f:
                json.dump(self.manifest, f, indent=2, ensure_ascii=False)
            self.log(f"清单已保存：{manifest_path}")
            messagebox.showinfo("成功", f"清单已保存到：\n{manifest_path}")
        except Exception as e:
            messagebox.showerror("错误", f"保存清单失败：{e}")


def main():
    root = tk.Tk()
    app = IngestHelperApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
