# -*- coding: utf-8 -*-
"""
Windows Ingest Helper v9
- 本地素材扫描
- 720p proxy 转码
- 本地 TOS 上传（支持配置文件）
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

VERSION = "v9"
BUILD_TIME = "2026-04-13T09:30:00+08:00"

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
                # 合并默认值
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
    """获取 ffmpeg 路径"""
    if getattr(sys, 'frozen', False):
        # 打包后环境
        base_path = sys._MEIPASS
        bin_path = os.path.join(base_path, 'bin')
        ffmpeg_exe = os.path.join(bin_path, 'ffmpeg.exe')
        ffprobe_exe = os.path.join(bin_path, 'ffprobe.exe')
        if os.path.exists(ffmpeg_exe) and os.path.exists(ffprobe_exe):
            return ffmpeg_exe, ffprobe_exe
    # 开发环境
    bin_path = os.path.join(os.path.dirname(__file__), 'bin')
    ffmpeg_exe = os.path.join(bin_path, 'ffmpeg.exe')
    ffprobe_exe = os.path.join(bin_path, 'ffprobe.exe')
    if os.path.exists(ffmpeg_exe) and os.path.exists(ffprobe_exe):
        return ffmpeg_exe, ffprobe_exe
    return None, None


def get_video_info(video_path, ffprobe_exe):
    """获取视频信息"""
    cmd = [
        ffprobe_exe, '-v', 'quiet', '-print_format', 'json',
        '-show_streams', '-show_format', video_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return json.loads(result.stdout)


def transcode_to_proxy(video_path, output_path, ffmpeg_exe):
    """转码为 720p proxy"""
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
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


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


class IngestHelperApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"Windows 上传/预处理助手 {VERSION}")
        self.root.geometry("900x700")
        
        self.config = load_config()
        self.source_dir = ""
        self.output_dir = ""
        self.proxy_files = []
        self.manifest = {"files": [], "created_at": "", "version": VERSION}
        
        self.create_widgets()
    
    def create_widgets(self):
        # 标题
        title_frame = ttk.Frame(self.root)
        title_frame.pack(fill='x', padx=10, pady=10)
        ttk.Label(title_frame, text=f"Windows 上传/预处理助手 {VERSION}", 
                  font=('Arial', 16, 'bold')).pack(side='left')
        ttk.Label(title_frame, text=f"Build: {BUILD_TIME}", 
                  font=('Arial', 8)).pack(side='left', padx=10)
        
        # 配置按钮
        ttk.Button(title_frame, text="上传配置", 
                   command=self.show_config_dialog).pack(side='right')
        
        # 配置状态
        self.config_status_var = tk.StringVar()
        self.update_config_status()
        ttk.Label(self.root, textvariable=self.config_status_var, 
                  foreground='red').pack(anchor='ne', padx=10)
        
        # 源目录
        dir_frame = ttk.LabelFrame(self.root, text="源目录", padding=10)
        dir_frame.pack(fill='x', padx=10, pady=5)
        
        self.source_var = tk.StringVar()
        ttk.Entry(dir_frame, textvariable=self.source_var, width=60).pack(side='left')
        ttk.Button(dir_frame, text="浏览", command=self.browse_source).pack(side='left', padx=5)
        
        # 输出目录
        out_frame = ttk.LabelFrame(self.root, text="输出目录", padding=10)
        out_frame.pack(fill='x', padx=10, pady=5)
        
        self.output_var = tk.StringVar()
        ttk.Entry(out_frame, textvariable=self.output_var, width=60).pack(side='left')
        ttk.Button(out_frame, text="浏览", command=self.browse_output).pack(side='left', padx=5)
        
        # 操作按钮
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill='x', padx=10, pady=10)
        
        ttk.Button(btn_frame, text="扫描素材", command=self.scan_videos).pack(side='left', padx=5)
        ttk.Button(btn_frame, text="批量转码", command=self.batch_transcode).pack(side='left', padx=5)
        ttk.Button(btn_frame, text="上传 TOS", command=self.upload_tos).pack(side='left', padx=5)
        ttk.Button(btn_frame, text="保存清单", command=self.save_manifest).pack(side='left', padx=5)
        
        # 进度
        progress_frame = ttk.LabelFrame(self.root, text="进度", padding=10)
        progress_frame.pack(fill='both', expand=True, padx=10, pady=5)
        
        self.progress_text = tk.Text(progress_frame, height=20, wrap='word')
        self.progress_text.pack(fill='both', expand=True)
        scrollbar = ttk.Scrollbar(self.progress_text, command=self.progress_text.yview)
        scrollbar.pack(side='right', fill='y')
        self.progress_text.config(yscrollcommand=scrollbar.set)
        
        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status_var, relief='sunken').pack(fill='x', side='bottom')
    
    def update_config_status(self):
        """更新配置状态显示"""
        if self.config['tos_ak'] and self.config['tos_sk']:
            self.config_status_var.set(f"✅ TOS 配置：{self.config['bucket']}")
            self.config_status_var.set(f"✅ TOS 配置：{self.config['bucket']} @ {self.config['region']}")
        else:
            self.config_status_var.set("⚠️ 未配置 TOS 上传凭据（点击"上传配置"设置）")
    
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
        
        # AK
        ttk.Label(form_frame, text="Access Key (AK):").grid(row=0, column=0, sticky='w', pady=5)
        ak_var = tk.StringVar(value=self.config.get('tos_ak', ''))
        ak_entry = ttk.Entry(form_frame, textvariable=ak_var, width=50)
        ak_entry.grid(row=0, column=1, pady=5)
        
        # SK
        ttk.Label(form_frame, text="Secret Key (SK):").grid(row=1, column=0, sticky='w', pady=5)
        sk_var = tk.StringVar(value=self.config.get('tos_sk', ''))
        sk_entry = ttk.Entry(form_frame, textvariable=sk_var, width=50, show='*')
        sk_entry.grid(row=1, column=1, pady=5)
        
        # Bucket
        ttk.Label(form_frame, text="Bucket:").grid(row=2, column=0, sticky='w', pady=5)
        bucket_var = tk.StringVar(value=self.config.get('bucket', 'e23-video'))
        ttk.Entry(form_frame, textvariable=bucket_var, width=50).grid(row=2, column=1, pady=5)
        
        # Region
        ttk.Label(form_frame, text="Region:").grid(row=3, column=0, sticky='w', pady=5)
        region_var = tk.StringVar(value=self.config.get('region', 'cn-beijing'))
        ttk.Entry(form_frame, textvariable=region_var, width=50).grid(row=3, column=1, pady=5)
        
        # Endpoint
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
    
    def scan_videos(self):
        """扫描视频素材"""
        if not self.source_dir:
            messagebox.showwarning("警告", "请先选择源目录")
            return
        
        self.log(f"扫描目录：{self.source_dir}")
        video_extensions = ('.mp4', '.mov', '.avi', '.mkv', '.flv')
        self.proxy_files = []
        
        for root, dirs, files in os.walk(self.source_dir):
            for file in files:
                if file.lower().endswith(video_extensions):
                    full_path = os.path.join(root, file)
                    self.proxy_files.append({
                        'source_path': full_path,
                        'filename': file,
                        'proxy_path': '',
                        'tos_key': '',
                        'tos_url': '',
                        'upload_status': 'pending'
                    })
                    self.log(f"  找到：{file}")
        
        self.log(f"共找到 {len(self.proxy_files)} 个视频文件")
        self.status_var.set(f"已扫描 {len(self.proxy_files)} 个视频")
    
    def batch_transcode(self):
        """批量转码"""
        if not self.proxy_files:
            messagebox.showwarning("警告", "请先扫描素材")
            return
        
        if not self.output_dir:
            messagebox.showwarning("警告", "请先选择输出目录")
            return
        
        ffmpeg_exe, ffprobe_exe = get_ffmpeg_path()
        if not ffmpeg_exe:
            messagebox.showerror("错误", "未找到 ffmpeg，请确保 bin/ffmpeg.exe 存在")
            return
        
        self.log("开始批量转码...")
        success_count = 0
        
        for i, item in enumerate(self.proxy_files):
            filename = os.path.splitext(item['filename'])[0]
            proxy_filename = f"{filename}_720p.mp4"
            proxy_path = os.path.join(self.output_dir, proxy_filename)
            
            self.log(f"[{i+1}/{len(self.proxy_files)}] 转码：{item['filename']}")
            
            if transcode_to_proxy(item['source_path'], proxy_path, ffmpeg_exe):
                item['proxy_path'] = proxy_path
                success_count += 1
                self.log(f"  ✅ 成功：{proxy_filename}")
            else:
                self.log(f"  ❌ 失败：{item['filename']}")
        
        self.log(f"转码完成：{success_count}/{len(self.proxy_files)} 成功")
        self.status_var.set(f"转码完成：{success_count}/{len(self.proxy_files)}")
    
    def upload_tos(self):
        """上传 TOS"""
        if not self.proxy_files:
            messagebox.showwarning("警告", "请先扫描并转码素材")
            return
        
        # 检查配置
        if not self.config['tos_ak'] or not self.config['tos_sk']:
            messagebox.showerror("错误", 
                "未配置本地 TOS 上传凭据！\n\n"
                "请点击右上角"上传配置"按钮，填写：\n"
                "- TOS Access Key (AK)\n"
                "- TOS Secret Key (SK)\n"
                "- Bucket\n"
                "- Region\n"
                "- Endpoint")
            return
        
        if not TOS_AVAILABLE:
            messagebox.showerror("错误", "TOS SDK 未安装，请运行：pip install tos")
            return
        
        self.log("开始上传 TOS...")
        upload_count = 0
        
        for i, item in enumerate(self.proxy_files):
            if not item['proxy_path'] or not os.path.exists(item['proxy_path']):
                self.log(f"[{i+1}/{len(self.proxy_files)}] 跳过（无 proxy 文件）：{item['filename']}")
                continue
            
            # 生成 TOS key
            file_hash = hashlib.md5(open(item['proxy_path'], 'rb').read()).hexdigest()[:8]
            tos_key = f"windows_ingest/{datetime.now().strftime('%Y%m%d')}/{file_hash}_{os.path.basename(item['proxy_path'])}"
            
            self.log(f"[{i+1}/{len(self.proxy_files)}] 上传：{os.path.basename(item['proxy_path'])}")
            
            success, result = upload_to_tos(item['proxy_path'], tos_key, self.config)
            
            if success:
                item['tos_key'] = tos_key
                item['tos_url'] = result
                item['upload_status'] = 'completed'
                upload_count += 1
                self.log(f"  ✅ 成功：{result}")
            else:
                item['upload_status'] = f'failed: {result}'
                self.log(f"  ❌ 失败：{result}")
        
        self.log(f"上传完成：{upload_count}/{len(self.proxy_files)} 成功")
        self.status_var.set(f"TOS 上传完成：{upload_count} 个文件")
        
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
