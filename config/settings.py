#!/usr/bin/env python3
"""
配置管理模块

从 config/.env 加载配置，支持环境变量覆盖
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# 确定应用根目录
APP_ROOT = Path(__file__).parent.parent

# 加载 .env 配置
env_path = APP_ROOT / 'config' / '.env'
if env_path.exists():
    load_dotenv(env_path)

# ============================================
# 基础路径配置
# ============================================
APP_ROOT = Path(os.getenv('APP_ROOT', str(APP_ROOT)))
CONFIG_DIR = Path(os.getenv('CONFIG_DIR', APP_ROOT / 'config'))
DATA_DIR = Path(os.getenv('DATA_DIR', APP_ROOT / 'data'))
CACHE_DIR = Path(os.getenv('CACHE_DIR', APP_ROOT / 'cache'))
OUTPUT_DIR = Path(os.getenv('OUTPUT_DIR', APP_ROOT / 'outputs'))
TASK_DIR = Path(os.getenv('TASK_DIR', APP_ROOT / 'tasks'))

# ============================================
# 分层目录配置
# ============================================
V1_MATERIALS_DIR = Path(os.getenv('V1_MATERIALS_DIR', APP_ROOT / 'v1_materials'))
V2_SEMANTIC_DIR = Path(os.getenv('V2_SEMANTIC_DIR', APP_ROOT / 'v2_semantic'))
V3_TIMELINE_DIR = Path(os.getenv('V3_TIMELINE_DIR', APP_ROOT / 'v3_timeline'))
V4_RENDER_DIR = Path(os.getenv('V4_RENDER_DIR', APP_ROOT / 'v4_render'))
V5_GATE_DIR = Path(os.getenv('V5_GATE_DIR', APP_ROOT / 'v5_gate'))

# ============================================
# 输出目录
# ============================================
OUTPUTS_RAW_DIR = OUTPUT_DIR / 'raw'
OUTPUTS_APPROVED_DIR = OUTPUT_DIR / 'approved'

# ============================================
# 素材配置
# ============================================
UPLOADS_DIR = Path(os.getenv('UPLOADS_DIR', APP_ROOT / 'uploads'))
FIXED_MATERIALS = int(os.getenv('FIXED_MATERIALS', '12'))

# ============================================
# 视频参数
# ============================================
TARGET_WIDTH = int(os.getenv('TARGET_WIDTH', '1280'))
TARGET_HEIGHT = int(os.getenv('TARGET_HEIGHT', '720'))
TARGET_FPS = int(os.getenv('TARGET_FPS', '25'))
TARGET_CODEC = os.getenv('TARGET_CODEC', 'h264')

# ============================================
# 校验规则（统一标准）
# ============================================
MIN_CLIP_DURATION = float(os.getenv('MIN_CLIP_DURATION', '1.5'))
VIDEO_AUDIO_BUFFER = float(os.getenv('VIDEO_AUDIO_BUFFER', '-0.5'))
SUBTITLE_MATCH_RATE = float(os.getenv('SUBTITLE_MATCH_RATE', '0.95'))

# ============================================
# TTS 配置
# ============================================
TTS_PROVIDER = os.getenv('TTS_PROVIDER', 'edge_tts')
TTS_VOICE = os.getenv('TTS_VOICE', 'zh-CN-XiaoxiaoNeural')
TTS_RATE = os.getenv('TTS_RATE', '+0%')

# ============================================
# 字幕样式
# ============================================
SUBTITLE_FONT_SIZE = int(os.getenv('SUBTITLE_FONT_SIZE', '26'))
SUBTITLE_MARGIN_V = int(os.getenv('SUBTITLE_MARGIN_V', '20'))
SUBTITLE_MARGIN_L = int(os.getenv('SUBTITLE_MARGIN_L', '40'))
SUBTITLE_MARGIN_R = int(os.getenv('SUBTITLE_MARGIN_R', '40'))

# ============================================
# 初始化目录（确保所有目录存在）
# ============================================
def init_directories():
    """初始化所有必要目录"""
    dirs = [
        CONFIG_DIR, DATA_DIR, CACHE_DIR, OUTPUT_DIR, TASK_DIR,
        V1_MATERIALS_DIR, V2_SEMANTIC_DIR, V3_TIMELINE_DIR,
        V4_RENDER_DIR, V5_GATE_DIR,
        OUTPUTS_RAW_DIR, OUTPUTS_APPROVED_DIR,
        UPLOADS_DIR
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
