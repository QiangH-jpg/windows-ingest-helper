#!/usr/bin/env python3
"""
Windows 上传/预处理助手 v1.0
主入口 - GUI 版本

运行方式：
    Windows: 双击 main.py 或 main.exe
    命令行：python main.py
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
from pathlib import Path

# 配置
FFMPEG = "ffmpeg"
PROXY_WIDTH = 1280
PROXY_HEIGHT = 720
PROXY_FPS = 25

class IngestHelperGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Windows 上传/预处理助手 v1.0")
        self.root.geometry("900x700")
        
        # 状态变量
        self.input_dir = tk.StringVar()
        self.output_dir = tk.StringVar(value="./output")
        self.is_processing = False
        self.video_files = []
        self.processed_count = 0
        self.bad_count = 0
        self.uploaded_count = 0
        
        self.setup_ui()
    
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
        
        # 创建树形列表
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
    
    def log(self, message):
        """添加日志"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_text.see("end")
        self.root.update_idletasks()
    
    def browse_input(self):
        """浏览输入目录"""
        directory = filedialog.askdirectory(title="选择素材目录")
        if directory:
            self.input_dir.set(directory)
            self.log(f"选择素材目录：{directory}")
    
    def browse_output(self):
        """浏览输出目录"""
        directory = filedialog.askdirectory(title="选择输出目录")
        if directory:
            self.output_dir.set(directory)
            self.log(f"选择输出目录：{directory}")
    
    def scan_videos(self):
        """扫描视频文件"""
        input_dir = self.input_dir.get()
        if not input_dir or not os.path.exists(input_dir):
            messagebox.showerror("错误", "请先选择有效的素材目录")
            return
        
        self.log(f"开始扫描：{input_dir}")
        self.status_var.set("正在扫描...")
        
        # 清空列表
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.video_files = []
        
        # 扫描视频文件
        video_extensions = {'.mp4', '.mov', '.avi', '.mkv', '.m4v'}
        for root, dirs, files in os.walk(input_dir):
            for file in files:
                if Path(file).suffix.lower() in video_extensions:
                    file_path = os.path.join(root, file)
                    self.video_files.append(file_path)
        
        self.log(f"找到 {len(self.video_files)} 个视频文件")
        
        # 获取每个视频信息
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
    
    def get_video_info(self, video_path):
        """获取视频元数据"""
        cmd = [
            FFMPEG, '-v', 'error',
            '-show_entries', 'stream=width,height,duration,r_frame_rate,codec_name',
            '-show_entries', 'format=filename,size',
            '-of', 'json',
            video_path
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
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
    
    def transcode_all(self):
        """转码所有视频"""
        if not self.video_files:
            return
        
        self.is_processing = True
        self.transcode_btn.config(state="disabled")
        self.scan_btn.config(state="disabled")
        
        # 创建输出目录
        output_dir = self.output_dir.get()
        proxy_dir = os.path.join(output_dir, 'proxy')
        logs_dir = os.path.join(output_dir, 'logs')
        os.makedirs(proxy_dir, exist_ok=True)
        os.makedirs(logs_dir, exist_ok=True)
        
        # 初始化 manifest
        manifest = {
            'version': '1.0',
            'created_at': datetime.now().isoformat(),
            'input_directory': self.input_dir.get(),
            'total_files': len(self.video_files),
            'processed_files': [],
            'bad_files': [],
            'duplicates': []
        }
        
        # 转码线程
        def run_transcode():
            self.processed_count = 0
            self.bad_count = 0
            
            for i, video_path in enumerate(self.video_files):
                progress = int((i + 1) / len(self.video_files) * 100)
                self.progress_var.set(progress)
                self.status_var.set(f"处理中：{i+1}/{len(self.video_files)}")
                
                filename = os.path.basename(video_path)
                self.log(f"[{i+1}/{len(self.video_files)}] {filename}")
                
                # 获取元数据
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
                
                # 检测坏片
                is_bad, reasons = self.is_bad_video(info)
                if is_bad:
                    self.log(f"  ⚠️ 坏片：{', '.join(reasons)}")
                    manifest['bad_files'].append({
                        'original_path': video_path,
                        'original_info': info,
                        'reasons': reasons,
                        'status': 'bad'
                    })
                    self.bad_count += 1
                    self.update_item_status(i, f"❌ 坏片：{reasons[0]}")
                    continue
                
                # 转码 proxy
                proxy_filename = f"proxy_{i:04d}_{Path(video_path).stem}.mp4"
                proxy_path = os.path.join(proxy_dir, proxy_filename)
                
                self.log(f"  转码 720p proxy...")
                if self.transcode_to_proxy(video_path, proxy_path):
                    proxy_size = os.path.getsize(proxy_path)
                    self.log(f"  ✅ 转码成功 {proxy_size/1024/1024:.1f}MB")
                    
                    # 计算哈希
                    file_hash = self.compute_file_hash(video_path)
                    
                    # 添加到 manifest
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
            
            # 保存 manifest
            manifest_path = os.path.join(output_dir, 'manifest.json')
            with open(manifest_path, 'w', encoding='utf-8') as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
            
            # 保存日志
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
            
            # 存储 manifest 路径供上传使用
            self.manifest_path = manifest_path
        
        threading.Thread(target=run_transcode, daemon=True).start()
    
    def is_bad_video(self, info):
        """检测坏片"""
        reasons = []
        if info['duration'] < 2:
            reasons.append(f"时长过短 ({info['duration']:.1f}s)")
        if info['height'] < 360:
            reasons.append(f"分辨率过低 ({info['width']}x{info['height']})")
        if info['size'] < 102400:
            reasons.append(f"文件大小异常 ({info['size']/1024:.1f}KB)")
        return len(reasons) > 0, reasons
    
    def transcode_to_proxy(self, input_path, output_path):
        """转码为 720p proxy"""
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
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            return result.returncode == 0
        except Exception as e:
            self.log(f"  转码错误：{e}")
            return False
    
    def compute_file_hash(self, file_path):
        """计算文件 SHA256 哈希"""
        sha256 = hashlib.sha256()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                sha256.update(chunk)
        return sha256.hexdigest()
    
    def update_item_status(self, index, status):
        """更新列表项状态"""
        items = self.tree.get_children()
        if index < len(items):
            values = list(self.tree.item(items[index])['values'])
            values[4] = status
            self.tree.item(items[index], values=values)
    
    def upload_all(self):
        """上传到 TOS"""
        if not hasattr(self, 'manifest_path'):
            messagebox.showerror("错误", "请先完成转码")
            return
        
        messagebox.showinfo("提示", "TOS 上传功能需要配置凭据\n当前版本仅生成上传清单\n下一步将实现真实上传")
        self.log("TOS 上传：需要配置 TOS_ACCESS_KEY 和 TOS_SECRET_KEY 环境变量")
        self.status_var.set("TOS 上传需配置凭据")

def main():
    root = tk.Tk()
    app = IngestHelperGUI(root)
    root.mainloop()

if __name__ == '__main__':
    main()
