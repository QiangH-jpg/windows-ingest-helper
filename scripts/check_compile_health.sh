#!/bin/bash
# compile health 检查脚本
# 用途：release 前必须执行，确保 generate_video.py 可编译
# 用法：bash scripts/check_compile_health.sh

set -e

cd "$(dirname "$0")/.." || exit 1

echo "=== generate_video.py compile health check ==="

if python3 -m py_compile pipeline/generate_video.py; then
    echo "✅ py_compile PASS"
    exit 0
else
    echo "❌ py_compile FAIL"
    echo ""
    echo "release 前必须修复所有 compile error"
    exit 1
fi
