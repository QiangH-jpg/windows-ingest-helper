import os
import uuid
from .config import config

class Storage:
    def __init__(self):
        self.uploads_dir = config['storage']['uploads_dir']
        self.workdir = config['storage']['workdir']
        self.outputs_dir = config['storage']['outputs_dir']
        self._ensure_dirs()
    
    def _ensure_dirs(self):
        for d in [self.uploads_dir, self.workdir, self.outputs_dir]:
            os.makedirs(d, exist_ok=True)
    
    def save_upload(self, file_content, original_filename):
        """Save uploaded file, return file_id and path"""
        file_id = str(uuid.uuid4())
        ext = os.path.splitext(original_filename)[1] or '.mp4'
        filename = f"{file_id}{ext}"
        path = os.path.join(self.uploads_dir, filename)
        with open(path, 'wb') as f:
            f.write(file_content)
        return file_id, path
    
    def get_upload_path(self, file_id):
        """Get upload path by file_id"""
        for f in os.listdir(self.uploads_dir):
            if f.startswith(file_id):
                return os.path.join(self.uploads_dir, f)
        return None
    
    def get_output_path(self, task_id):
        return os.path.join(self.outputs_dir, f"{task_id}.mp4")

storage = Storage()
