#!/bin/bash
# 视频工具启动脚本
# 用法：./start.sh [start|stop|restart|status]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$BASE_DIR/.venv"
PYTHON="$VENV_DIR/bin/python"
RUN_SCRIPT="$BASE_DIR/run.py"
PID_FILE="$BASE_DIR/logs/app.pid"
LOG_DIR="$BASE_DIR/logs/app"

# 加载环境变量
if [ -f "$BASE_DIR/config/.env" ]; then
    export $(cat "$BASE_DIR/config/.env" | grep -v '^#' | xargs)
fi

# 设置默认值
export VIDEO_TOOL_BASE_DIR="${VIDEO_TOOL_BASE_DIR:-$BASE_DIR}"
export VIDEO_TOOL_HOST="${VIDEO_TOOL_HOST:-0.0.0.0}"
export VIDEO_TOOL_PORT="${VIDEO_TOOL_PORT:-8088}"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"

# 创建日志目录
mkdir -p "$LOG_DIR"

case "${1:-start}" in
    start)
        echo "Starting video-tool..."
        if [ -f "$PID_FILE" ]; then
            PID=$(cat "$PID_FILE")
            if kill -0 "$PID" 2>/dev/null; then
                echo "video-tool is already running (PID: $PID)"
                exit 0
            fi
            rm -f "$PID_FILE"
        fi
        
        cd "$BASE_DIR"
        nohup "$PYTHON" "$RUN_SCRIPT" > "$LOG_DIR/startup.log" 2>&1 &
        echo $! > "$PID_FILE"
        echo "video-tool started (PID: $(cat $PID_FILE))"
        ;;
    
    stop)
        echo "Stopping video-tool..."
        if [ -f "$PID_FILE" ]; then
            PID=$(cat "$PID_FILE")
            if kill -0 "$PID" 2>/dev/null; then
                kill "$PID"
                sleep 2
                if kill -0 "$PID" 2>/dev/null; then
                    kill -9 "$PID"
                fi
                echo "video-tool stopped"
            else
                echo "video-tool is not running"
            fi
            rm -f "$PID_FILE"
        else
            echo "PID file not found"
        fi
        ;;
    
    restart)
        "$0" stop
        sleep 2
        "$0" start
        ;;
    
    status)
        if [ -f "$PID_FILE" ]; then
            PID=$(cat "$PID_FILE")
            if kill -0 "$PID" 2>/dev/null; then
                echo "video-tool is running (PID: $PID)"
                exit 0
            else
                echo "video-tool is not running (stale PID file)"
                exit 1
            fi
        else
            echo "video-tool is not running (no PID file)"
            exit 1
        fi
        ;;
    
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac

exit 0
