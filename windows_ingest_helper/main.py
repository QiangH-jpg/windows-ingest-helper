# -*- coding: utf-8 -*-
"""
Windows Ingest Helper v11
- 本地素材扫描（读取元数据）
- 720p proxy 转码（后台静默，不弹黑框）
- 本地 TOS 上传
- 修复：ffmpeg 路径定位 + 恢复旧版可理解交互
"""

import os
import sys
import json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import subprocess
import hashlib
from datetime import datetime

# TOS SDK
try:
    from tos import TosClientV2
    TOS_AVAILABLE = True
except ImportError:
    TOS_AVAILABLE = False

VERSION = "v11"
BUILD_TIME = "2026-04-13T11:00:00+08:00"

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
    """加载本地配置文件"""
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
    """保存配置文件"""
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"保存配置文件失败：{e}")
        return False


def get_ffmpeg_path():
    """获取 ffmpeg 和 ffprobe 路径（带调试输出）"""
    print("=" * 60)
    print("FFmpeg 路径诊断")
    print("=" * 60)
    print(f"sys.executable = {sys.executable}")
    print(f"os.getcwd() = {os.getcwd()}")
    
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(sys.executable)
        print(f"【打包环境】exe_dir = {exe_dir}")
    else:
        exe_dir = os.path.dirname(os.path.abspath(__file__))
        print(f"【开发环境】script_dir = {exe_dir}")
    
    bin_dir = os.path.join(exe_dir, 'bin')
    ffmpeg_path = os.path.join(bin_dir, 'ffmpeg.exe')
    ffprobe_path = os.path.join(bin_dir, 'ffprobe.exe')
    
    print(f"检查 bin 目录：{bin_dir}")
    print(f"ffmpeg_path = {ffmpeg_path}")
    print(f"ffmpeg_exists = {os.path.exists(ffmpeg_path)}")
    print(f"ffprobe_path = {ffprobe_path}")
    print(f"ffprobe_exists = {os.path.exists(ffprobe_path)}")
    
    if os.path.exists(ffmpeg_path) and os.path.exists(ffprobe_path):
        print(f"✅ 找到 ffmpeg 和 ffprobe")
        return ffmpeg_path, ffprobe_path
    
    print("❌ 未找到 ffmpeg 和 ffprobe")
    return None, None


def get_video_metadata(video_path, ffprobe_exe):
    """读取视频元数据（时长、分辨率、文件大小）"""
    try:
        file_size = os.path.getsize(video_path)
        
        cmd = [
            ffprobe_exe, '-v', 'quiet', '-print_format', 'json',
            '-show_streams', '-show_format', video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, 
                               creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0)
        
        if result.returncode != 0:
            return None
        
        data = json.loads(result.stdout)
        
        # 获取视频流信息
        video_stream = None
        for stream in data.get('streams', []):
            if stream.get('codec_type') == 'video':
                video_stream = stream
                break
        
        if not video_stream:
            return None
        
        duration = float(data.get('format', {}).get('duration', 0))
        width = video_stream.get('width', 0)
        height = video_stream.get('height', 0)
        codec = video_stream.get('codec_name', 'unknown')
        
        return {
            'duration': duration,
            'width': width,
            'height': height,
            'file_size': file_size,
            'codec': codec
        }
    except Exception as e:
        print(f"读取元数据失败：{e}")
        return None


def transcode_to_proxy(video_path, output_path, ffmpeg_exe, log_callback=None):
    """
    转码为 720p proxy（后台静默执行，不弹黑框）
    """
    # 使用 CREATE_NO_WINDOW 标志（Windows 特有）避免弹出控制台窗口
    startupinfo = None
    creationflags = 0
    
    if sys.platform == 'win32':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = subprocess.CREATE_NO_WINDOW
    
    cmd = [
        ffmpeg_exe, '-i', video_path,
        '-c:v', 'libx264',
        '-preset', 'fast',
        '-crf', '28',
        '-vf', 'scale=1280:720:force_original_aspect_ratio=decrease',
        '-c:a', 'aac',
        '-b:a', '128k',
        '-y',
        output_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, 
                           startupinfo=startupinfo, creationflags=creationflags)
    
    if result.returncode == 0 and os.path.exists(output_path):
        output_size = os.path.getsize(output_path)
        return True, output_size
    else:
        return False, result.stderr[:200] if result.stderr else "未知错误"


def upload_to_tos(file_path, tos_key, config):
    """上传文件到 TOS"""
    if not TOS_AVAILABLE:
        return False, "TOS SDK 未安装"
    
    try:
        endpoint = f"https://{config['endpoint']}"
        client = TosClientV2(
            ak=config['tos_ak'],
            sk=config['tos_sk'],
            endpoint=endpoint,
            region=config['region']
        )
        client.put_object_from_file(
            bucket=config['bucket'],
            key=tos_key,
            file_path=file_path
        )
        tos_url = f"https://{config['bucket']}.{config['endpoint']}/{tos_key}"
        return True, tos_url
    except Exception as e:
        return False, str(e)


def format_file_size(size_bytes):
    """格式化文件大小"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f}{unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f}TB"


def format_duration(seconds):
    """格式化时长"""
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{mins}:{secs:02d}"


class IngestHelperApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"Windows 上传/预处理助手 {VERSION}")
        self.root.geometry("1000x750")
        
        self.config = load_config()
        self.source_dir = ""
        self.output_dir = ""
        self.proxy_files = []
        self.manifest = {"files": [], "created_at": "", "version": VERSION}
        
        self.create_widgets()
        self.update_config_status()
    
    def create_widgets(self):
        # 顶部工具栏
        toolbar = ttk.Frame(self.root)
        toolbar.pack(fill='x', padx=10, pady=10)
        
        ttk.Label(toolbar, text=f"Windows 上传/预处理助手 {VERSION}", 
                  font=('Arial', 14, 'bold')).pack(side='left')
        ttk.Label(toolbar, text=f"Build: {BUILD_TIME}", 
                  font=('Arial', 8)).pack(side='left', padx=10)
        
        ttk.Button(toolbar, text="上传配置", command=self.show_config_dialog).pack(side='right')
        
        # 配置状态
        self.config_status_var = tk.StringVar(value="⚠️ 未配置 TOS 上传凭据")
        ttk.Label(self.root, textvariable=self.config_status_var, 
                  foreground='red').pack(anchor='ne', padx=10)
        
        # 目录选择
        dir_frame = ttk.LabelFrame(self.root, text="目录设置", padding=10)
        dir_frame.pack(fill='x', padx=10, pady=5)
        
        # 源目录
        source_frame = ttk.Frame(dir_frame)
        source_frame.pack(fill='x', pady=5)
        ttk.Label(source_frame, text="源目录:", width=10).pack(side='left')
        self.source_var = tk.StringVar()
        ttk.Entry(source_frame, textvariable=self.source_var, width=60).pack(side='left', padx=5)
        ttk.Button(source_frame, text="浏览", command=self.browse_source).pack(side='left')
        
        # 输出目录
        output_frame = ttk.Frame(dir_frame)
        output_frame.pack(fill='x', pady=5)
        ttk.Label(output_frame, text="输出目录:", width=10).pack(side='left')
        self.output_var = tk.StringVar()
        ttk.Entry(output_frame, textvariable=self.output_var, width=60).pack(side='left', padx=5)
        ttk.Button(output_frame, text="浏览", command=self.browse_output).pack(side='left')
        
        # 操作按钮
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill='x', padx=10, pady=10)
        
        self.scan_btn = ttk.Button(btn_frame, text="1. 扫描素材", command=self.scan_videos)
        self.scan_btn.pack(side='left', padx=5)
        
        self.transcode_btn = ttk.Button(btn_frame, text="2. 批量转码", command=self.batch_transcode)
        self.transcode_btn.pack(side='left', padx=5)
        
        self.upload_btn = ttk.Button(btn_frame, text="3. 上传 TOS", command=self.upload_tos)
        self.upload_btn.pack(side='left', padx=5)
        
        self.save_btn = ttk.Button(btn_frame, text="4. 保存清单", command=self.save_manifest)
        self.save_btn.pack(side='left', padx=5)
        
        # 素材列表（带状态列）
        list_frame = ttk.LabelFrame(self.root, text="素材列表", padding=10)
        list_frame.pack(fill='both', expand=True, padx=10, pady=5)
        
        # 创建 Treeview
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
        
        # 滚动条
        scrollbar = ttk.Scrollbar(list_frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        
        self.tree.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        
        # 进度日志
        log_frame = ttk.LabelFrame(self.root, text="进度日志", padding=10)
        log_frame.pack(fill='x', padx=10, pady=5)
        
        self.progress_text = tk.Text(log_frame, height=8, wrap='word')
        self.progress_text.pack(fill='both', expand=True)
        log_scrollbar = ttk.Scrollbar(self.progress_text, command=self.progress_text.yview)
        log_scrollbar.pack(side='right', fill='y')
        self.progress_text.config(yscrollcommand=log_scrollbar.set)
        
        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status_var, relief='sunken').pack(fill='x', side='bottom')
    
    def update_config_status(self):
        """更新配置状态显示"""
        if self.config['tos_ak'] and self.config['tos_sk']:
            self.config_status_var.set(f"✅ TOS 配置：{self.config['bucket']} @ {self.config['region']}")
        else:
            self.config_status_var.set("⚠️ 未配置 TOS 上传凭据（点击'上传配置'设置）")
    
    def show_config_dialog(self):
        """显示配置对话框"""
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
            new_config = {
                'tos_ak': ak_var.get(),
                'tos_sk': sk_var.get(),
                'bucket': bucket_var.get(),
                'region': region_var.get(),
                'endpoint': endpoint_var.get()
            }
            if save_config(new_config):
                self.config = new_config
                self.update_config_status()
                messagebox.showinfo("成功", "配置已保存")
                dialog.destroy()
            else:
                messagebox.showerror("错误", "保存配置失败")
        
        ttk.Button(dialog, text="保存配置", command=on_save).pack(pady=10)
    
    def browse_source(self):
        """浏览源目录"""
        directory = filedialog.askdirectory()
        if directory:
            self.source_var.set(directory)
            self.source_dir = directory
    
    def browse_output(self):
        """浏览输出目录"""
        directory = filedialog.askdirectory()
        if directory:
            self.output_var.set(directory)
            self.output_dir = directory
    
    def log(self, message):
        """日志输出"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.progress_text.insert('end', f"[{timestamp}] {message}\n")
        self.progress_text.see('end')
        self.root.update_idletasks()
    
    def update_tree_status(self, index, status):
        """更新列表中某条素材的状态"""
        if 0 <= index < len(self.proxy_files):
            item = self.proxy_files[index]
            item['status'] = status
            
            # 更新 Treeview 显示
            values = (
                item['filename'],
                item.get('duration_fmt', ''),
                item.get('resolution', ''),
                item.get('size_fmt', ''),
                status
            )
            self.tree.item(index, values=values)
            self.root.update_idletasks()
    
    def scan_videos(self):
        """扫描视频素材（读取元数据）"""
        if not self.source_dir:
            messagebox.showwarning("警告", "请先选择源目录")
            return
        
        ffmpeg_exe, ffprobe_exe = get_ffmpeg_path()
        if not ffprobe_exe:
            messagebox.showerror("错误", "未找到 ffprobe，请确保 bin/ffprobe.exe 存在")
            return
        
        self.log("=" * 60)
        self.log("开始扫描素材（读取元数据）")
        self.log("=" * 60)
        self.log(f"源目录：{self.source_dir}")
        
        video_extensions = ('.mp4', '.mov', '.avi', '.mkv', '.flv')
        self.proxy_files = []
        
        # 清空列表
        for item in self.tree.get_children():
            self.tree.delete(item)
        
        self.status_var.set("正在扫描...")
        self.root.update_idletasks()
        
        # 扫描并读取元数据
        for root, dirs, files in os.walk(self.source_dir):
            for file in files:
                if file.lower().endswith(video_extensions):
                    full_path = os.path.join(root, file)
                    
                    # 读取元数据
                    self.log(f"读取元数据：{file}")
                    metadata = get_video_metadata(full_path, ffprobe_exe)
                    
                    if metadata:
                        duration_fmt = format_duration(metadata['duration'])
                        resolution = f"{metadata['width']}x{metadata['height']}"
                        size_fmt = format_file_size(metadata['file_size'])
                        status = "已读取元数据"
                        
                        self.log(f"  ✅ {duration_fmt} | {resolution} | {size_fmt}")
                    else:
                        duration_fmt = ""
                        resolution = ""
                        size_fmt = ""
                        status = "无法读取元数据"
                        self.log(f"  ❌ 无法读取元数据")
                    
                    item = {
                        'source_path': full_path,
                        'filename': file,
                        'proxy_path': '',
                        'tos_key': '',
                        'tos_url': '',
                        'upload_status': 'pending',
                        'status': status,
                        'duration_fmt': duration_fmt,
                        'resolution': resolution,
                        'size_fmt': size_fmt,
                        'metadata': metadata
                    }
                    self.proxy_files.append(item)
                    
                    # 添加到列表
                    self.tree.insert('', 'end', values=(
                        file, duration_fmt, resolution, size_fmt, status
                    ))
        
        total = len(self.proxy_files)
        self.log(f"扫描完成：共 {total} 个视频文件")
        self.status_var.set(f"已扫描 {total} 个视频")
        
        if total == 0:
            messagebox.showinfo("提示", f"在目录中未找到视频文件")
    
    def batch_transcode(self):
        """批量转码（后台静默，实时进度）"""
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
        
        self.log("=" * 60)
        self.log("开始批量转码（后台静默执行）")
        self.log("=" * 60)
        
        total = len(self.proxy_files)
        success_count = 0
        fail_count = 0
        
        for i, item in enumerate(self.proxy_files):
            filename = item['filename']
            base_name = os.path.splitext(filename)[0]
            proxy_filename = f"{base_name}_720p.mp4"
            proxy_path = os.path.join(self.output_dir, proxy_filename)
            
            # 更新状态：转码中
            self.update_tree_status(i, "转码中...")
            self.log(f"[{i+1}/{total}] 正在转码：{filename}")
            self.root.update_idletasks()
            
            success, result = transcode_to_proxy(item['source_path'], proxy_path, ffmpeg_exe)
            
            if success:
                item['proxy_path'] = proxy_path
                item['proxy_size'] = result
                success_count += 1
                size_str = format_file_size(result)
                self.update_tree_status(i, f"✅ 转码成功 ({size_str})")
                self.log(f"[{i+1}/{total}] ✅ 转码成功：{size_str}")
            else:
                fail_count += 1
                error_msg = str(result)[:100]
                self.update_tree_status(i, f"❌ 转码失败")
                self.log(f"[{i+1}/{total}] ❌ 转码失败：{error_msg}")
        
        self.log("=" * 60)
        self.log(f"转码完成：成功 {success_count}/{total}, 失败 {fail_count}")
        self.status_var.set(f"转码完成：{success_count}/{total}")
        
        if success_count > 0:
            messagebox.showinfo("成功", f"转码完成：\n成功 {success_count}/{total}\n失败 {fail_count}")
    
    def upload_tos(self):
        """上传 TOS"""
        if not self.proxy_files:
            messagebox.showwarning("警告", "请先扫描素材")
            return
        
        if not self.config['tos_ak'] or not self.config['tos_sk']:
            messagebox.showerror("错误", 
                '未配置本地 TOS 上传凭据！\n\n'
                '请点击右上角"上传配置"按钮填写。')
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
            
            filename = item['filename']
            self.update_tree_status(i, "上传中...")
            self.log(f"上传：{filename}")
            
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
        """保存清单"""
        if not self.output_dir:
            messagebox.showwarning("警告", "请先选择输出目录")
            return
        
        self.manifest['files'] = self.proxy_files
        self.manifest['created_at'] = datetime.now().isoformat()
        self.manifest['config'] = {
            'bucket': self.config['bucket'],
            'region': self.config['region']
        }
        
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
