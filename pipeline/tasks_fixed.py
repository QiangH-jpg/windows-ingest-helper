import os
import json
import uuid
import asyncio
import subprocess
from datetime import datetime
import sys

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from core.config import config
from pipeline import processor
from pipeline.tts_provider import generate_tts, create_subtitle_srt_from_meta
from pipeline.video_analyzer import create_video_provider, extract_frames_for_task
from pipeline.video_cache import get_or_create_processed, extract_dynamic_clip
from pipeline.processor import get_video_duration
from core.storage import storage
from pipeline.project_state import load_project_state, validate_script, validate_task, get_state_constraints, clear_cache

VIDEO = config['video']
