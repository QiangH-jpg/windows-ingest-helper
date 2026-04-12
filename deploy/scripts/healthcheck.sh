#!/bin/bash
# 视频工具健康检查脚本
# 用于 systemd 或负载均衡器的健康检查

set -e

BASE_DIR="$(dirname "$(dirname "$(readlink -f "$0")")")"
PORT="${VIDEO_TOOL_PORT:-8088}"
TIMEOUT=5

# 检查服务端口
if curl -sf --max-time "$TIMEOUT" "http://localhost:$PORT/api/health" > /dev/null; then
    echo "OK - video-tool is healthy"
    exit 0
else
    echo "CRITICAL - video-tool health check failed"
    exit 2
fi
