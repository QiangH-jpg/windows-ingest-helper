#!/bin/bash
# 视频项目独立部署最小演练脚本
# 用途：验证项目是否真正具备独立部署能力

set -e

echo "============================================================"
echo "视频项目独立部署最小演练"
echo "============================================================"
echo ""

# 1. 创建独立临时目录
TEMP_DIR="/tmp/video-tool-independent-test-$$"
echo "[1/6] 创建独立测试目录：$TEMP_DIR"
mkdir -p "$TEMP_DIR"

# 2. 复制项目文件（排除大文件）
echo "[2/6] 复制项目文件..."
cp -r /home/admin/.openclaw/workspace/video-tool/* "$TEMP_DIR/" 2>/dev/null || true
# 清理大目录
rm -rf "$TEMP_DIR/outputs" "$TEMP_DIR/workdir" "$TEMP_DIR/uploads" "$TEMP_DIR/processed_videos" "$TEMP_DIR/.git" 2>/dev/null || true

# 3. 创建独立虚拟环境
echo "[3/6] 创建独立 Python 虚拟环境..."
cd "$TEMP_DIR"
python3.11 -m venv .venv
source .venv/bin/activate

# 4. 安装依赖
echo "[4/6] 安装依赖..."
pip install --upgrade pip -q
pip install -r requirements.txt -q

# 5. 配置环境变量
echo "[5/6] 配置环境变量..."
cp config/.env.example config/.env
cat > config/.env << EOF
# 独立部署测试配置
VIDEO_TOOL_HOST=127.0.0.1
VIDEO_TOOL_PORT=8765
FFMPEG_PATH=/usr/local/bin/ffmpeg
FFPROBE_PATH=/usr/local/bin/ffprobe
LOG_LEVEL=INFO
DEPLOY_ENV=test
EOF

# 6. 验证启动
echo "[6/6] 验证启动..."
timeout 10 python run.py > /dev/null 2>&1 &
PID=$!
sleep 3

# 健康检查
if curl -sf http://127.0.0.1:8765/api/health > /dev/null 2>&1; then
    echo ""
    echo "============================================================"
    echo "✅ 独立部署演练成功！"
    echo "============================================================"
    echo ""
    echo "验证结果:"
    echo "  - 独立目录：$TEMP_DIR"
    echo "  - 独立 venv: $TEMP_DIR/.venv"
    echo "  - 独立配置：$TEMP_DIR/config/.env"
    echo "  - 独立端口：8765"
    echo "  - 健康检查：通过"
    echo ""
    
    # 清理
    echo "清理测试环境..."
    kill $PID 2>/dev/null || true
    rm -rf "$TEMP_DIR"
    echo "清理完成"
    
    exit 0
else
    echo ""
    echo "============================================================"
    echo "❌ 独立部署演练失败！"
    echo "============================================================"
    echo ""
    echo "服务未能正常启动，请检查日志"
    
    # 清理
    kill $PID 2>/dev/null || true
    rm -rf "$TEMP_DIR"
    
    exit 1
fi
