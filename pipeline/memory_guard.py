"""
项目记忆守护模块

三层记忆体系：
1. PROJECT_STATE.md - 当前状态（精简版）
2. memory/YYYY-MM-DD.md - 每日日志
3. MILESTONES.md - 里程碑（只追加）

强制规则：
- 执行前必须读取 PROJECT_STATE.md + 最近1天 memory
- 执行后必须更新相应文件
- 阶段完成必须追加到 MILESTONES.md

⚠️ 紧急修复：
- PROJECT_STATE 摘要不超过 800 字
- memory 摘要不超过 500 字
- MILESTONES 默认不注入
- 总长度超过 120000 字时截断告警
"""

import os
import sys
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_STATE_PATH = os.path.join(PROJECT_ROOT, 'PROJECT_STATE.md')
MEMORY_DIR = os.path.join(PROJECT_ROOT, 'memory')
MILESTONES_PATH = os.path.join(PROJECT_ROOT, 'MILESTONES.md')

# 长度限制
MAX_PROJECT_STATE_CHARS = 800
MAX_MEMORY_CHARS = 500
MAX_TOTAL_CHARS = 120000


class MemoryGuard:
    """项目记忆守护者"""
    
    def __init__(self):
        self.project_state = None
        self.today_memory_path = None
        self.yesterday_memory_path = None
        
    def check_project_state(self):
        """检查 PROJECT_STATE.md 是否存在"""
        if not os.path.exists(PROJECT_STATE_PATH):
            raise FileNotFoundError(
                f"❌ PROJECT_STATE.md 不存在！\n"
                f"   路径: {PROJECT_STATE_PATH}\n"
                f"   所有任务必须以 PROJECT_STATE.md 为基准。"
            )
        return True
    
    def check_memory_dir(self):
        """检查 memory 目录是否存在"""
        if not os.path.exists(MEMORY_DIR):
            os.makedirs(MEMORY_DIR, exist_ok=True)
            print(f"[MemoryGuard] 创建 memory 目录: {MEMORY_DIR}")
        return True
    
    def get_today_memory_path(self):
        """获取今天的 memory 文件路径"""
        today = datetime.now().strftime('%Y-%m-%d')
        return os.path.join(MEMORY_DIR, f"{today}.md")
    
    def get_yesterday_memory_path(self):
        """获取昨天的 memory 文件路径"""
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        return os.path.join(MEMORY_DIR, f"{yesterday}.md")
    
    def _extract_summary(self, content, max_chars, title=""):
        """提取摘要（保留关键部分）"""
        if len(content) <= max_chars:
            return content
        
        # 提取关键部分
        lines = content.split('\n')
        summary_lines = []
        current_len = 0
        
        for line in lines:
            # 优先保留标题和列表项
            if line.startswith('#') or line.startswith('-') or line.startswith('|') or line.startswith('1.'):
                summary_lines.append(line)
                current_len += len(line)
                if current_len >= max_chars:
                    break
        
        summary = '\n'.join(summary_lines)
        if len(summary) > max_chars:
            summary = summary[:max_chars]
        
        print(f"[MemoryGuard] ⚠ {title} 已截断: {len(content)} → {len(summary)} 字符")
        return summary
    
    def load_project_state(self):
        """加载项目状态（摘要版，不超过 MAX_PROJECT_STATE_CHARS 字符）"""
        self.check_project_state()
        
        with open(PROJECT_STATE_PATH, 'r', encoding='utf-8') as f:
            full_content = f.read()
        
        # 提取摘要
        self.project_state = self._extract_summary(
            full_content, 
            MAX_PROJECT_STATE_CHARS,
            "PROJECT_STATE.md"
        )
        print(f"[MemoryGuard] ✓ 已加载 PROJECT_STATE.md ({len(self.project_state)} 字符)")
        return self.project_state
    
    def load_recent_memory(self):
        """加载最近的 memory 文件（摘要版，不超过 MAX_MEMORY_CHARS 字符）"""
        self.check_memory_dir()
        
        # 优先加载今天的
        today_path = self.get_today_memory_path()
        if os.path.exists(today_path):
            with open(today_path, 'r', encoding='utf-8') as f:
                full_content = f.read()
            # 提取摘要
            content = self._extract_summary(
                full_content,
                MAX_MEMORY_CHARS,
                "memory"
            )
            print(f"[MemoryGuard] ✓ 已加载今日 memory ({len(content)} 字符)")
            return content, today_path
        
        # 其次加载昨天的
        yesterday_path = self.get_yesterday_memory_path()
        if os.path.exists(yesterday_path):
            with open(yesterday_path, 'r', encoding='utf-8') as f:
                full_content = f.read()
            content = self._extract_summary(
                full_content,
                MAX_MEMORY_CHARS,
                "memory"
            )
            print(f"[MemoryGuard] ✓ 已加载昨日 memory ({len(content)} 字符)")
            return content, yesterday_path
        
        # 都不存在，返回空
        print(f"[MemoryGuard] ⚠ 无最近 memory 文件，将创建新的")
        return None, today_path
    
    def enforce_pre_check(self):
        """
        强制前置检查
        
        必须在所有 run_*.py / 主执行入口开头调用
        如果检查失败，直接报错退出
        """
        print("\n" + "=" * 60)
        print("[MemoryGuard] 强制前置检查")
        print("=" * 60)
        
        # 1. 检查并加载 PROJECT_STATE.md
        self.load_project_state()
        
        # 2. 检查并加载最近 memory
        memory_content, memory_path = self.load_recent_memory()
        self.today_memory_path = memory_path
        
        print("=" * 60)
        print("[MemoryGuard] ✓ 前置检查通过，允许执行")
        print("=" * 60 + "\n")
        
        return True
    
    def append_to_memory(self, content):
        """追加到今天的 memory 文件"""
        self.check_memory_dir()
        
        path = self.get_today_memory_path()
        
        if os.path.exists(path):
            with open(path, 'a', encoding='utf-8') as f:
                f.write('\n' + content)
        else:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(f"# {datetime.now().strftime('%Y-%m-%d')} 工作日志\n\n")
                f.write(content)
        
        print(f"[MemoryGuard] ✓ 已追加到 memory: {path}")
    
    def append_milestone(self, milestone_id, title, content):
        """追加里程碑到 MILESTONES.md"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')
        
        entry = f"""
### [{milestone_id}] {title}
- **时间**: {timestamp}
- **内容**: {content}

"""
        
        with open(MILESTONES_PATH, 'a', encoding='utf-8') as f:
            f.write(entry)
        
        print(f"[MemoryGuard] ✓ 已追加里程碑: {milestone_id}")
    
    def git_backup(self, message=None):
        """Git 备份当前状态"""
        if message is None:
            message = f"auto backup: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        
        import subprocess
        
        try:
            # git add
            subprocess.run(['git', 'add', '-A'], cwd=PROJECT_ROOT, check=True)
            # git commit
            result = subprocess.run(
                ['git', 'commit', '-m', message],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                print(f"[MemoryGuard] ✓ Git 备份完成: {message}")
                return True
            else:
                print(f"[MemoryGuard] ⚠ Git 备份跳过（无变更）")
                return False
        except Exception as e:
            print(f"[MemoryGuard] ⚠ Git 备份失败: {e}")
            return False


# 便捷函数
def enforce_pre_check():
    """强制前置检查（便捷入口）"""
    guard = MemoryGuard()
    return guard.enforce_pre_check()


def get_guard():
    """获取 MemoryGuard 实例"""
    return MemoryGuard()


if __name__ == '__main__':
    # 测试
    enforce_pre_check()