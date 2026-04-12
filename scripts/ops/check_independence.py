#!/usr/bin/env python3
"""
视频项目独立化检查脚本

验证项目是否已具备独立部署能力。
"""
import os
import sys
import json
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent.parent  # scripts/ops/ -> scripts/ -> video-tool/

def check_directory_structure():
    """检查目录结构"""
    print("\n[1/8] 检查目录结构...")
    required_dirs = [
        'app', 'pipeline', 'core', 'config', 'deploy', 'scripts',
        'uploads', 'workdir', 'outputs', 'cache', 'logs', 'archive',
        'baselines', 'samples', 'docs', 'tests'
    ]
    
    missing = []
    for d in required_dirs:
        if not (BASE_DIR / d).exists():
            missing.append(d)
    
    if missing:
        print(f"  ❌ 缺少目录：{missing}")
        return False
    else:
        print(f"  ✅ 目录结构完整 ({len(required_dirs)} 个目录)")
        return True

def check_config_templates():
    """检查配置模板"""
    print("\n[2/8] 检查配置模板...")
    required_files = [
        'config/config.example.json',
        'config/.env.example',
        'config/paths.example.json'
    ]
    
    missing = []
    for f in required_files:
        if not (BASE_DIR / f).exists():
            missing.append(f)
    
    if missing:
        print(f"  ❌ 缺少配置模板：{missing}")
        return False
    else:
        print(f"  ✅ 配置模板完整 ({len(required_files)} 个文件)")
        return True

def check_deploy_assets():
    """检查部署资产"""
    print("\n[3/8] 检查部署资产...")
    required_files = [
        'deploy/systemd/video-tool.service',
        'deploy/nginx/video-tool.conf',
        'deploy/scripts/start.sh',
        'deploy/scripts/healthcheck.sh',
        'deploy/DEPLOYMENT.md'
    ]
    
    missing = []
    for f in required_files:
        if not (BASE_DIR / f).exists():
            missing.append(f)
    
    if missing:
        print(f"  ❌ 缺少部署资产：{missing}")
        return False
    else:
        print(f"  ✅ 部署资产完整 ({len(required_files)} 个文件)")
        return True

def check_entry_point():
    """检查运行入口"""
    print("\n[4/8] 检查运行入口...")
    run_py = BASE_DIR / 'run.py'
    
    if not run_py.exists():
        print(f"  ❌ 缺少运行入口：run.py")
        return False
    
    # 检查是否有 shebang
    with open(run_py, 'r') as f:
        first_line = f.readline()
        if not first_line.startswith('#!'):
            print(f"  ⚠️  run.py 缺少 shebang")
        else:
            print(f"  ✅ 运行入口存在：run.py")
            return True
    
    return True

def check_no_openclaw_paths():
    """检查代码中是否仍有 OpenClaw 路径"""
    print("\n[5/8] 检查 OpenClaw 路径依赖...")
    
    openclaw_paths = []
    for py_file in (BASE_DIR / 'core').glob('*.py'):
        with open(py_file, 'r') as f:
            content = f.read()
            if '/home/admin/.openclaw' in content:
                openclaw_paths.append(str(py_file))
    
    # 检查 pipeline/tasks.py
    tasks_py = BASE_DIR / 'pipeline' / 'tasks.py'
    if tasks_py.exists():
        with open(tasks_py, 'r') as f:
            content = f.read()
            if '/home/admin/.openclaw' in content:
                openclaw_paths.append(str(tasks_py))
    
    if openclaw_paths:
        print(f"  ❌ 仍包含 OpenClaw 路径：{openclaw_paths}")
        return False
    else:
        print(f"  ✅ 无 OpenClaw 路径依赖")
        return True

def check_project_docs():
    """检查项目文档"""
    print("\n[6/8] 检查项目文档...")
    required_files = [
        'README.md',
        'CHANGELOG.md',
        'PROJECT_STATE.md',
        'MILESTONES.md'
    ]
    
    missing = []
    for f in required_files:
        if not (BASE_DIR / f).exists():
            missing.append(f)
    
    if missing:
        print(f"  ❌ 缺少项目文档：{missing}")
        return False
    else:
        print(f"  ✅ 项目文档完整 ({len(required_files)} 个文件)")
        return True

def check_archive():
    """检查项目档案"""
    print("\n[7/8] 检查项目档案...")
    archive_dir = BASE_DIR / 'archive'
    
    if not archive_dir.exists():
        print(f"  ❌ 缺少档案目录：archive/")
        return False
    
    # 检查是否有内容
    files = list(archive_dir.rglob('*'))
    if len(files) < 5:
        print(f"  ⚠️ 档案目录内容较少：{len(files)} 个文件")
        return True
    else:
        print(f"  ✅ 项目档案完整 ({len(files)} 个文件)")
        return True

def check_baselines():
    """检查基线文档"""
    print("\n[8/8] 检查基线文档...")
    baselines_dir = BASE_DIR / 'baselines'
    
    if not baselines_dir.exists():
        print(f"  ❌ 缺少基线目录：baselines/")
        return False
    
    phase1_doc = baselines_dir / 'phase1_baseline.md'
    if not phase1_doc.exists():
        print(f"  ❌ 缺少阶段 1 基线文档")
        return False
    else:
        print(f"  ✅ 基线文档完整")
        return True

def main():
    print("="*60)
    print("视频项目独立化检查")
    print("="*60)
    
    results = []
    results.append(check_directory_structure())
    results.append(check_config_templates())
    results.append(check_deploy_assets())
    results.append(check_entry_point())
    results.append(check_no_openclaw_paths())
    results.append(check_project_docs())
    results.append(check_archive())
    results.append(check_baselines())
    
    print("\n" + "="*60)
    passed = sum(results)
    total = len(results)
    print(f"检查结果：{passed}/{total} 通过")
    
    if all(results):
        print("\n✅ 独立化检查通过 - 项目已具备独立部署能力")
        return 0
    else:
        print(f"\n⚠️  独立化检查部分通过 - 仍有 {total - passed} 项需要完善")
        return 1

if __name__ == '__main__':
    sys.exit(main())
