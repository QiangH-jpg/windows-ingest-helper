#!/home/admin/.openclaw/workspace/.venv/bin/python
"""
轻量新闻短视频自动成片工具 - 主入口
阿里云公网测试版 v0.1.0
"""
import sys
import os

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from app.main import app, start_background_worker

if __name__ == '__main__':
    print("=" * 50)
    print("轻量新闻短视频自动成片工具")
    print("阿里云公网测试版 v0.1.0")
    print("=" * 50)
    
    # Start background worker
    start_background_worker()
    
    # Run server
    app.run(host='0.0.0.0', port=8088, debug=False, threaded=True)
