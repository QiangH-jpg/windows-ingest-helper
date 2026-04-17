#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V15 Windows 素材上传助手 — task_id 链路集成版

功能：
1. 选择本地素材文件
2. 调用 task/init 获取 task_id 和 task_url
3. 本地 ffmpeg 转码
4. 上传到 TOS（windows_ingest/YYYY-MM-DD/<task_id>/）
5. 调用 notify 通知服务器
6. 自动打开任务页

打包方式：PyInstaller --onefile --icon=icon.ico
"""

import os
import sys
import json
import time
import webbrowser
import subprocess
import traceback
from datetime import datetime
from pathlib import Path

# ============================================================
# 配置（可通过同目录 config.json 覆盖）
# ============================================================
DEFAULT_CONFIG = {
    "server_url": "http://47.93.194.154:8088",
    "tos_bucket": "e23-video",
    "tos_region": "cn-beijing",
    "tos_endpoint": "tos-cn-beijing.volces.com",
}

def load_config():
    """加载配置，优先读取同目录 config.json"""
    config = dict(DEFAULT_CONFIG)
    config_path = Path(__file__).parent / "config.json"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                user_config = json.load(f)
            config.update(user_config)
        except Exception as e:
            print(f"[WARN] 读取 config.json 失败: {e}")
    return config


# ============================================================
# 日志
# ============================================================
def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}")


# ============================================================
# Step 1: task/init
# ============================================================
def call_task_init(config, file_paths):
    """
    调用 POST /api/ui/task/init
    返回: (task_id, task_url, tos_prefix) 或 (None, None, None)
    """
    import urllib.request

    url = f"{config['server_url']}/api/ui/task/init"
    filenames = [Path(f).name for f in file_paths]
    payload = json.dumps({
        "file_count": len(file_paths),
        "filenames": filenames,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    log(f"调用 task/init: {url}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        task_id = data.get("task_id")
        task_url = data.get("task_url")
        tos_prefix = data.get("tos_prefix")

        if not task_id:
            log(f"task/init 返回异常: {data}", "ERROR")
            return None, None, None

        log(f"task/init 成功: task_id={task_id}")
        log(f"task_url={task_url}")
        log(f"tos_prefix={tos_prefix}")
        return task_id, task_url, tos_prefix

    except Exception as e:
        log(f"task/init 调用失败: {e}", "ERROR")
        return None, None, None


# ============================================================
# Step 2: 本地转码
# ============================================================
def find_ffmpeg():
    """查找 ffmpeg 路径"""
    # 优先查找同目录 bin/
    local_ffmpeg = Path(__file__).parent / "bin" / "ffmpeg.exe"
    if local_ffmpeg.exists():
        return str(local_ffmpeg)
    # 查找系统 PATH
    for name in ["ffmpeg.exe", "ffmpeg"]:
        try:
            result = subprocess.run(
                ["where", name] if os.name == "nt" else ["which", name],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return result.stdout.strip().split("\n")[0].strip()
        except:
            pass
    return None


def transcode_file(ffmpeg_path, input_path, output_path):
    """
    用 ffmpeg 转码为 720p H.264 + AAC
    返回: (success, error_msg)
    """
    cmd = [
        ffmpeg_path,
        "-i", input_path,
        "-vf", "scale=-2:720",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-y",
        output_path,
    ]
    log(f"转码: {Path(input_path).name} -> {Path(output_path).name}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 10 分钟超时
        )
        if result.returncode == 0 and os.path.exists(output_path):
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            log(f"转码成功: {Path(output_path).name} ({size_mb:.1f} MB)")
            return True, None
        else:
            err = result.stderr.strip()[-200:] if result.stderr else "unknown"
            log(f"转码失败: {err}", "ERROR")
            return False, err
    except subprocess.TimeoutExpired:
        log("转码超时 (600s)", "ERROR")
        return False, "timeout"
    except Exception as e:
        log(f"转码异常: {e}", "ERROR")
        return False, str(e)


# ============================================================
# Step 3: TOS 上传
# ============================================================
def upload_to_tos(config, local_path, tos_key):
    """
    上传单个文件到 TOS
    使用 tos SDK（如果可用）或 requests 直传
    返回: (success, error_msg)
    """
    # 尝试使用 tos SDK
    try:
        from tos import TosClientV2
        return _upload_with_sdk(config, local_path, tos_key)
    except ImportError:
        pass

    # fallback: 通过服务器代理上传
    log("tos SDK 不可用，使用服务器代理上传", "WARN")
    return _upload_via_server(config, local_path, tos_key)


def _upload_with_sdk(config, local_path, tos_key):
    """使用 tos SDK 直接上传"""
    from tos import TosClientV2

    ak = os.environ.get("TOS_INGEST_AK", "")
    sk = os.environ.get("TOS_INGEST_SK", "")

    if not ak or not sk:
        # 尝试从 config.json 读取
        config_path = Path(__file__).parent / "config.json"
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            ak = cfg.get("tos_ak", "") or cfg.get("tos_ingest_ak", "")
            sk = cfg.get("tos_sk", "") or cfg.get("tos_ingest_sk", "")

    if not ak or not sk:
        log("TOS AK/SK 未配置，请设置环境变量 TOS_INGEST_AK/TOS_INGEST_SK 或在 config.json 中配置", "ERROR")
        return False, "TOS credentials not configured"

    try:
        client = TosClientV2(
            ak=ak, sk=sk,
            endpoint=config["tos_endpoint"],
            region=config["tos_region"],
        )
        client.put_object_from_file(
            bucket=config["tos_bucket"],
            key=tos_key,
            file_path=local_path,
        )
        log(f"TOS 上传成功: {tos_key}")
        return True, None
    except Exception as e:
        log(f"TOS 上传失败: {e}", "ERROR")
        return False, str(e)


def _upload_via_server(config, local_path, tos_key):
    """通过服务器代理上传（需要服务器支持 /api/ui/upload 端点）"""
    import urllib.request

    # 先尝试直传 TOS（预签名 URL 方式）
    log("尝试通过服务器获取预签名 URL...", "INFO")
    url = f"{config['server_url']}/api/ui/upload/presign"
    payload = json.dumps({"tos_key": tos_key}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            presign_data = json.loads(resp.read().decode("utf-8"))
        presigned_url = presign_data.get("url")
        if presigned_url:
            log(f"使用预签名 URL 上传: {tos_key}")
            file_req = urllib.request.Request(
                presigned_url,
                data=open(local_path, "rb").read(),
                method="PUT",
                headers={"Content-Type": "application/octet-stream"},
            )
            with urllib.request.urlopen(file_req, timeout=300) as resp:
                if resp.status == 200:
                    log(f"TOS 上传成功: {tos_key}")
                    return True, None
    except Exception as e:
        log(f"预签名上传失败: {e}", "WARN")

    return False, "TOS upload failed: no SDK and no presign endpoint"


# ============================================================
# Step 4: notify
# ============================================================
def call_notify(config, task_id, tos_keys):
    """
    调用 POST /api/ui/task/<task_id>/notify
    返回: (success, error_msg)
    """
    import urllib.request

    url = f"{config['server_url']}/api/ui/task/{task_id}/notify"
    payload = json.dumps({
        "tos_keys": tos_keys,
        "file_count": len(tos_keys),
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    log(f"调用 notify: {url}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        status = data.get("status")
        log(f"notify 成功: status={status}")
        return True, None
    except Exception as e:
        log(f"notify 调用失败: {e}", "ERROR")
        return False, str(e)


# ============================================================
# Step 5: 打开任务页
# ============================================================
def open_task_page(task_url):
    """在默认浏览器中打开任务页"""
    log(f"打开任务页: {task_url}")
    try:
        webbrowser.open(task_url)
        log("已打开浏览器")
        return True
    except Exception as e:
        log(f"打开浏览器失败: {e}", "ERROR")
        return False


# ============================================================
# 主流程
# ============================================================
def main():
    print("=" * 60)
    print("  V15 素材上传助手 — task_id 链路集成版")
    print("  规则: windows_ingest/YYYY-MM-DD/<task_id>/")
    print("=" * 60)
    print()

    config = load_config()
    log(f"服务器: {config['server_url']}")

    # 获取文件列表（命令行参数或交互式选择）
    file_paths = []
    if len(sys.argv) > 1:
        file_paths = [f for f in sys.argv[1:] if os.path.isfile(f)]
    else:
        log("请在命令行传入素材文件路径:")
        log("  ingest_helper_v15.exe file1.mp4 file2.mp4 ...")
        print()
        log("按回车键退出...", "INFO")
        input()
        return

    if not file_paths:
        log("未找到有效文件", "ERROR")
        input("按回车键退出...")
        return

    log(f"共 {len(file_paths)} 个文件:")
    for f in file_paths:
        log(f"  - {Path(f).name}")
    print()

    # Step 1: task/init
    log(">>> Step 1: 初始化任务")
    task_id, task_url, tos_prefix = call_task_init(config, file_paths)
    if not task_id:
        log("task/init 失败，终止", "ERROR")
        input("按回车键退出...")
        return
    print()

    # Step 2: 转码
    log(">>> Step 2: 本地转码")
    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        log("ffmpeg 未找到，跳过转码，使用原始文件上传", "WARN")

    transcode_dir = Path(__file__).parent / "transcode_temp" / task_id
    transcode_dir.mkdir(parents=True, exist_ok=True)

    transcode_paths = []
    transcode_ok = True
    for fp in file_paths:
        fname = Path(fp).stem + "_720p.mp4"
        out_path = str(transcode_dir / fname)

        if ffmpeg_path:
            ok, err = transcode_file(ffmpeg_path, fp, out_path)
            if ok:
                transcode_paths.append(out_path)
            else:
                log(f"转码失败，尝试使用原文件: {Path(fp).name}", "WARN")
                transcode_paths.append(fp)
                transcode_ok = False
        else:
            transcode_paths.append(fp)
    print()

    # Step 3: 上传到 TOS
    log(">>> Step 3: 上传到 TOS")
    uploaded_keys = []
    upload_ok = True
    for tp in transcode_paths:
        fname = Path(tp).name
        tos_key = f"{tos_prefix}{fname}"
        ok, err = upload_to_tos(config, tp, tos_key)
        if ok:
            uploaded_keys.append(tos_key)
        else:
            log(f"上传失败: {fname} - {err}", "ERROR")
            upload_ok = False
    print()

    if not uploaded_keys:
        log("没有文件上传成功，终止", "ERROR")
        input("按回车键退出...")
        return

    log(f"上传成功 {len(uploaded_keys)}/{len(transcode_paths)} 个文件")
    print()

    # Step 4: notify
    log(">>> Step 4: 通知服务器")
    ok, err = call_notify(config, task_id, uploaded_keys)
    if not ok:
        log(f"notify 失败: {err}", "ERROR")
    print()

    # Step 5: 打开任务页
    log(">>> Step 5: 打开任务页")
    if task_url:
        open_task_page(task_url)
    print()

    # 完成
    print("=" * 60)
    if upload_ok and ok:
        log("✅ 全部完成！任务已提交，浏览器已打开")
    else:
        log("⚠️ 部分步骤有异常，请检查上方日志", "WARN")
    log(f"task_id: {task_id}")
    log(f"task_url: {task_url}")
    print("=" * 60)
    print()
    log("按回车键退出...", "INFO")
    input()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n用户取消操作")
        sys.exit(0)
    except Exception as e:
        log(f"程序异常: {e}", "ERROR")
        traceback.print_exc()
        input("按回车键退出...")
        sys.exit(1)
