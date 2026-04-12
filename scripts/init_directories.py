#!/usr/bin/env python3
"""
初始化脚本 - 创建必要目录和配置文件

用法：
python scripts/init_directories.py
"""
import os
import sys
from pathlib import Path

# 添加项目根目录到路径
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (
    CONFIG_DIR, DATA_DIR, CACHE_DIR, OUTPUT_DIR, TASK_DIR,
    V1_MATERIALS_DIR, V2_SEMANTIC_DIR, V3_TIMELINE_DIR,
    V4_RENDER_DIR, V5_GATE_DIR,
    OUTPUTS_RAW_DIR, OUTPUTS_APPROVED_DIR,
    UPLOADS_DIR, init_directories
)

def main():
    print("=" * 60)
    print("视频项目初始化")
    print("=" * 60)
    
    # 初始化目录
    print("\n[1/3] 初始化目录...")
    init_directories()
    print("  ✅ 目录初始化完成")
    
    # 检查配置文件
    print("\n[2/3] 检查配置文件...")
    env_example = CONFIG_DIR / '.env.example'
    env_file = CONFIG_DIR / '.env'
    
    if env_example.exists():
        if not env_file.exists():
            print(f"  ⚠️  {env_file} 不存在，请复制 {env_example} 并修改")
            print(f"  命令：cp {env_example} {env_file}")
        else:
            print(f"  ✅ {env_file} 已存在")
    else:
        print(f"  ❌ {env_example} 不存在")
    
    # 检查素材目录
    print("\n[3/3] 检查素材目录...")
    if UPLOADS_DIR.exists():
        uploads = list(UPLOADS_DIR.glob('*.mp4'))
        print(f"  素材数量：{len(uploads)}个")
        if len(uploads) == 0:
            print(f"  ⚠️  请上传素材到 {UPLOADS_DIR}")
    else:
        print(f"  ❌ {UPLOADS_DIR} 不存在")
    
    print("\n" + "=" * 60)
    print("初始化完成")
    print("=" * 60)
    print("\n下一步：")
    print("1. 复制 config/.env.example 为 config/.env 并修改配置")
    print("2. 上传视频素材到 uploads/")
    print("3. 运行 python scripts/run_production.py")

if __name__ == '__main__':
    main()
