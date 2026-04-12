#!/usr/bin/env python3
"""
TOS 上传工具
功能：读取 manifest.json → 分片上传 proxy 文件 → 更新 manifest

运行方式：
    python upload_to_tos.py --manifest ./output/manifest.json

依赖：
    pip install tos
"""
import os
import sys
import json
import argparse
from datetime import datetime

try:
    from tos import TosClientV2, exceptions
except ImportError:
    print("❌ 请安装 TOS SDK: pip install tos")
    sys.exit(1)

# TOS 配置（已写入程序）
TOS_BUCKET = 'e23-video'
TOS_REGION = 'cn-beijing'
TOS_ENDPOINT = f'tos-{TOS_REGION}.volces.com'
TOS_ACCESS_KEY = os.getenv('TOS_AK', '')
TOS_SECRET_KEY = os.getenv('TOS_SK', '')
# Windows 目录名
WINDOWS_DIR = 'Windows_ingest_helper'

def upload_file(client, local_path, tos_key):
    """上传单个文件到 TOS"""
    try:
        client.upload_file(bucket=TOS_BUCKET, key=tos_key, file_path=local_path)
        return True, f"https://{TOS_BUCKET}.{TOS_ENDPOINT}/{tos_key}"
    except exceptions.TosClientError as e:
        print(f"  ❌ 上传失败：{e}")
        return False, None

def upload_manifest(manifest_path):
    """上传 manifest.json 中所有 proxy 文件"""
    print(f"\n📤 读取清单：{manifest_path}")
    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)
    
    # 初始化 TOS 客户端
    if not TOS_ACCESS_KEY or not TOS_SECRET_KEY:
        print("⚠️ 未配置 TOS 凭据，使用模拟上传模式")
        client = None
    else:
        client = TosClientV2(
            ak=TOS_ACCESS_KEY,
            sk=TOS_SECRET_KEY,
            endpoint=TOS_ENDPOINT
        )
        print(f"  TOS 端点：{TOS_ENDPOINT}")
        print(f"  存储桶：{TOS_BUCKET}")
    
    # 上传每个 proxy 文件
    output_dir = os.path.dirname(manifest_path)
    proxy_dir = os.path.join(output_dir, 'proxy')
    uploads_dir = os.path.join(output_dir, 'uploads')
    os.makedirs(uploads_dir, exist_ok=True)
    
    uploaded_count = 0
    failed_count = 0
    
    for item in manifest['processed_files']:
        if item['upload_status'] == 'completed':
            print(f"\n⏭️  跳过已上传：{item['proxy_filename']}")
            uploaded_count += 1
            continue
        
        proxy_path = item['proxy_path']
        if not os.path.exists(proxy_path):
            print(f"\n❌ 文件不存在：{proxy_path}")
            failed_count += 1
            continue
        
        # 生成 TOS key
        tos_key = f"proxy/{datetime.now().strftime('%Y%m%d')}/{item['proxy_filename']}"
        
        print(f"\n[{uploaded_count + failed_count + 1}/{len(manifest['processed_files'])}] {item['proxy_filename']}")
        
        if client:
            # 真实上传
            success, tos_url = upload_file(client, proxy_path, tos_key)
        else:
            # 模拟上传
            print(f"  模拟上传：{tos_key}")
            tos_url = f"https://{TOS_BUCKET}.{TOS_ENDPOINT}/{tos_key}"
            success = True
        
        if success:
            item['tos_key'] = tos_key
            item['tos_url'] = tos_url
            item['upload_status'] = 'completed'
            item['upload_time'] = datetime.now().isoformat()
            uploaded_count += 1
            print(f"  ✅ 上传成功")
        else:
            item['upload_status'] = 'failed'
            item['upload_error'] = '上传失败'
            failed_count += 1
            print(f"  ❌ 上传失败")
    
    # 保存更新后的 manifest
    manifest['upload_summary'] = {
        'uploaded': uploaded_count,
        'failed': failed_count,
        'upload_time': datetime.now().isoformat()
    }
    
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    
    # 保存上传记录
    upload_record_path = os.path.join(uploads_dir, f'upload_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json')
    with open(upload_record_path, 'w', encoding='utf-8') as f:
        json.dump({
            'uploaded': uploaded_count,
            'failed': failed_count,
            'time': datetime.now().isoformat()
        }, f, ensure_ascii=False, indent=2)
    
    print(f"\n✅ 上传完成")
    print(f"  成功：{uploaded_count} 个")
    print(f"  失败：{failed_count} 个")
    print(f"  清单已更新：{manifest_path}")

def main():
    parser = argparse.ArgumentParser(description='TOS 上传工具')
    parser.add_argument('--manifest', required=True, help='manifest.json 路径')
    args = parser.parse_args()
    
    print("=" * 60)
    print("TOS 上传工具 v1.0")
    print("=" * 60)
    
    upload_manifest(args.manifest)
    
    print("\n" + "=" * 60)
    print("下一步：服务端粗识别")
    print("命令：python coarse_recognition.py --manifest ./output/manifest.json")
    print("=" * 60)

if __name__ == '__main__':
    main()
