#!/usr/bin/env python3
"""
Windows EXE 打包脚本
使用 PyInstaller 打包为单个 EXE 文件

运行方式：
    python build_exe.py

输出：
    dist/ingest_helper.exe
"""
import os
import subprocess
import sys

def build():
    """打包 EXE"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    main_py = os.path.join(script_dir, 'main.py')
    
    # PyInstaller 命令
    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--onefile',
        '--windowed',
        '--name', 'ingest_helper',
        '--icon', 'DEFAULT',
        '--add-data', '*.py;.',
        main_py
    ]
    
    print("=" * 60)
    print("Windows EXE 打包")
    print("=" * 60)
    print(f"主脚本：{main_py}")
    print(f"输出：dist/ingest_helper.exe")
    print()
    
    try:
        result = subprocess.run(cmd, cwd=script_dir, capture_output=True, text=True)
        if result.returncode == 0:
            print("✅ 打包成功")
            print(f"EXE 位置：{os.path.join(script_dir, 'dist', 'ingest_helper.exe')}")
        else:
            print("❌ 打包失败")
            print(result.stderr)
    except FileNotFoundError:
        print("❌ PyInstaller 未安装")
        print("请先运行：pip install pyinstaller")

if __name__ == '__main__':
    build()
