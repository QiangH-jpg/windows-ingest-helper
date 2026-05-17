"""
启动时配置完整性检查 — 主链配置缺失拦截器
位置：app/main.py 启动入口调用
时机：Flask app.run() 之前
规则：任何一项 FAIL → sys.exit(1)，禁止启动 worker
"""

import os
import sys
import json

CHECKS = []
_ALL_PASS = True


def _record(name: str, ok: bool, detail: str = ''):
    global _ALL_PASS
    status = 'PASS' if ok else 'FAIL'
    if not ok:
        _ALL_PASS = False
    CHECKS.append((name, status, detail))


def run_startup_integrity_check():
    """
    启动时检查主链配置完整性。
    任何一项 FAIL 直接 sys.exit(1)。
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # 1. project_runtime_state.json
    path = os.path.join(project_root, 'project_runtime_state.json')
    ok = os.path.exists(path)
    _record('project_runtime_state.json', ok,
            path if ok else f'文件不存在: {path}')

    # 2. 00_READ_THIS_FIRST.md
    path = os.path.join(project_root, '00_READ_THIS_FIRST.md')
    ok = os.path.exists(path)
    _record('00_READ_THIS_FIRST.md', ok,
            path if ok else f'文件不存在: {path}')

    # 3. prompts/video_news/ 目录
    path = os.path.join(project_root, 'prompts', 'video_news')
    ok = os.path.isdir(path)
    _record('prompts/video_news/', ok,
            path if ok else f'目录不存在: {path}')

    # 4. l2_three_tier_review_prompt_v1.txt
    path = os.path.join(project_root, 'prompts', 'video_news', 'l2_three_tier_review_prompt_v1.txt')
    ok = os.path.exists(path)
    size = os.path.getsize(path) if ok else 0
    _record('l2_three_tier_review_prompt_v1.txt', ok and size > 10000,
            f'{size} bytes' if ok else f'文件不存在: {path}')

    # 5. L3 导演 prompt（主链实际引用）
    for fname, min_size in [('l3_director_prompt_v7.txt', 5000),
                             ('l3_music_montage_prompt_v1.txt', 5000)]:
        path = os.path.join(project_root, 'prompts', 'video_news', fname)
        ok = os.path.exists(path)
        size = os.path.getsize(path) if ok else 0
        _record(fname, ok and size >= min_size,
                f'{size} bytes' if ok else f'文件不存在: {path}')

    # 6. DOUBAO_API_KEY
    key = os.environ.get('DOUBAO_API_KEY', '')
    ok = len(key) >= 30
    masked = f'{key[:6]}****{key[-4:]}' if len(key) >= 10 else '(空)'
    _record('DOUBAO_API_KEY', ok, f'长度={len(key)} ({masked})')

    # 7. DOUBAO_ENDPOINT
    ep = os.environ.get('DOUBAO_ENDPOINT', '')
    ok = ep.startswith('https://')
    _record('DOUBAO_ENDPOINT', ok, f'{ep or "(空)"}')

    # 8. DOUBAO_MODEL
    model = os.environ.get('DOUBAO_MODEL', '')
    ok = len(model) >= 5
    _record('DOUBAO_MODEL', ok, f'{model or "(空)"}')

    # 输出检查结果
    print('\n' + '=' * 60)
    print('  启动时配置完整性检查 (startup integrity check)')
    print('=' * 60)
    for name, status, detail in CHECKS:
        icon = '✅' if status == 'PASS' else '❌'
        print(f'  {icon} {status}  {name:45s} {detail}')
    print('-' * 60)
    pass_count = sum(1 for _, s, _ in CHECKS if s == 'PASS')
    fail_count = sum(1 for _, s, _ in CHECKS if s == 'FAIL')
    print(f'  总计: {pass_count} PASS / {fail_count} FAIL')
    print('=' * 60)

    if not _ALL_PASS:
        print('\n[启动拦截] 配置完整性检查失败，禁止启动 worker。')
        print('请检查上方 FAIL 项，修复后重启。')
        sys.exit(1)
    
    print('[启动检查] 全部通过 ✅\n')
