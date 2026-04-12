#!/bin/bash
# Start the video tool server

cd /home/admin/.openclaw/workspace/video-tool

# Use project Python
PYTHON=/home/admin/.openclaw/workspace/.venv/bin/python

# Start server
exec $PYTHON -m app.main
