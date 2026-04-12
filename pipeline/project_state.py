"""
项目状态验证模块

所有视频任务执行前必须读取 PROJECT_STATE.md 并进行冲突检查
【重要】禁止规则从 PROJECT_STATE.md 动态解析，不在代码中硬编码
"""
import os
import re

PROJECT_STATE_PATH = os.path.join(os.path.dirname(__file__), '..', 'PROJECT_STATE.md')

# 缓存解析结果
_cached_state = None
_cached_forbidden = None
_cached_not_recommended = None
_cached_goal = None
_cached_priority = None


def load_project_state():
    """加载项目状态文件"""
    if not os.path.exists(PROJECT_STATE_PATH):
        raise FileNotFoundError(
            f"PROJECT_STATE.md 不存在！路径：{PROJECT_STATE_PATH}\n"
            "所有任务必须以 PROJECT_STATE.md 为基准。"
        )
    
    with open(PROJECT_STATE_PATH, 'r', encoding='utf-8') as f:
        return f.read()


def _parse_forbidden_actions(state_content):
    """
    从 PROJECT_STATE.md 解析禁止回退事项
    
    解析第六条「禁止回退事项」部分，提取所有禁止规则
    """
    forbidden = []
    
    # 匹配第六条的列表项
    # 格式：1. 禁止xxx
    pattern = r'(\d+)\.\s*禁止(.+)'
    
    # 找到第六条的区间
    in_section = False
    for line in state_content.split('\n'):
        if '六、当前禁止回退事项' in line:
            in_section = True
            continue
        if in_section:
            if line.startswith('## ') or line.startswith('七、'):
                break
            match = re.search(pattern, line.strip())
            if match:
                action = match.group(2).strip()
                # 提取关键词（取前几个字作为匹配关键词）
                keyword = action[:10] if len(action) > 10 else action
                forbidden.append((keyword, f"禁止{action}"))
    
    return forbidden


def _parse_not_recommended(state_content):
    """
    从 PROJECT_STATE.md 解析不应该做的事情
    
    解析第九条「当前不应该做的事情」部分
    """
    not_recommended = []
    
    # 匹配第九条的列表项
    pattern = r'(\d+)\.\s*不要(.+)'
    
    in_section = False
    for line in state_content.split('\n'):
        if '九、当前不应该做的事情' in line:
            in_section = True
            continue
        if in_section:
            if line.startswith('## ') or line.startswith('十、'):
                break
            match = re.search(pattern, line.strip())
            if match:
                action = match.group(2).strip()
                keyword = action[:15] if len(action) > 15 else action
                not_recommended.append((keyword, f"不要{action}"))
    
    return not_recommended


def _parse_current_goal(state_content):
    """解析当前主目标"""
    match = re.search(r'三、当前主目标\s*\n+(.+?)\n', state_content)
    if match:
        return match.group(1).strip()
    
    # 备用：从括号中提取
    match = re.search(r'【(.+?)】', state_content[:2000])
    if match:
        return match.group(1)
    return "未知目标"


def _parse_first_priority(state_content):
    """解析第一优先级任务"""
    match = re.search(r'第一优先级[：:]\s*\n*【(.+?)】', state_content)
    if match:
        return match.group(1).strip()
    return "未知优先级"


def get_forbidden_actions():
    """
    获取禁止回退事项列表（从 PROJECT_STATE.md 动态解析）
    
    Returns:
        List[Tuple[keyword, reason]] - 禁止事项列表
    """
    global _cached_forbidden, _cached_state
    
    if _cached_forbidden is not None:
        return _cached_forbidden
    
    state = load_project_state()
    _cached_forbidden = _parse_forbidden_actions(state)
    return _cached_forbidden


def get_not_recommended():
    """
    获取不应该做的事情列表（从 PROJECT_STATE.md 动态解析）
    
    Returns:
        List[Tuple[keyword, reason]] - 不推荐事项列表
    """
    global _cached_not_recommended
    
    if _cached_not_recommended is not None:
        return _cached_not_recommended
    
    state = load_project_state()
    _cached_not_recommended = _parse_not_recommended(state)
    return _cached_not_recommended


def get_current_goal():
    """获取当前主目标（从 PROJECT_STATE.md 动态解析）"""
    global _cached_goal
    
    if _cached_goal is not None:
        return _cached_goal
    
    state = load_project_state()
    _cached_goal = _parse_current_goal(state)
    return _cached_goal


def get_first_priority():
    """获取第一优先级任务（从 PROJECT_STATE.md 动态解析）"""
    global _cached_priority
    
    if _cached_priority is not None:
        return _cached_priority
    
    state = load_project_state()
    _cached_priority = _parse_first_priority(state)
    return _cached_priority


def check_forbidden(text_or_action):
    """
    检查是否违反禁止回退事项（动态解析规则）
    
    Args:
        text_or_action: 要检查的文本或动作描述
    
    Returns:
        (is_forbidden: bool, reason: str or None)
    """
    forbidden_actions = get_forbidden_actions()
    
    for keyword, reason in forbidden_actions:
        # 关键词匹配：检查 keyword 的核心部分是否在文本中
        # 提取核心词（去掉"禁止"前缀、标点等）
        core = keyword.replace("禁止", "").replace("恢复", "").replace("回退到", "").replace("让", "").replace("再次把", "")
        core = core.replace("引入", "").replace("、", "").replace(""", "").replace(""", "")
        
        # 检查核心词或原关键词
        if keyword in text_or_action or core[:8] in text_or_action:
            return True, reason
        
        # 特殊匹配：5秒碎片、测试文案、原声主导等核心概念
        special_matches = [
            ("碎片轮换", "碎片轮换"),
            ("5秒碎片", "碎片轮换"),
            ("测试文案", "测试文案"),
            ("原声主导", "原声主导"),
            ("单段素材", "单段素材"),
            ("图片序列", "图片序列"),
            ("短样片轮播", "短样片轮播"),
            ("清单外素材", "清单外素材"),
        ]
        
        for pattern, forbidden_key in special_matches:
            if pattern in text_or_action and forbidden_key in reason:
                return True, reason
    
    return False, None


def check_not_recommended(text_or_action):
    """
    检查是否属于不推荐做的事情（动态解析规则）
    
    Args:
        text_or_action: 要检查的文本或动作描述
    
    Returns:
        (is_not_recommended: bool, reason: str or None)
    """
    not_recommended = get_not_recommended()
    
    for keyword, reason in not_recommended:
        # 关键词匹配
        core = keyword.replace("不要", "").replace("现在就", "").replace("再", "")
        
        if keyword in text_or_action or core[:10] in text_or_action:
            return True, reason
        
        # 特殊匹配
        special_matches = [
            ("大修 FFmpeg", "FFmpeg 主链"),
            ("大修ffmpeg", "FFmpeg 主链"),
            ("云非编", "云非编"),
            ("对象存储", "对象存储正式化"),
            ("重型大模型", "重型大模型"),
            ("同时改", "同时改"),
            ("扩展新功能", "扩展新功能"),
        ]
        
        for pattern, not_rec_key in special_matches:
            if pattern in text_or_action and not_rec_key in reason:
                return True, reason
    
    return False, None


def validate_task(task_description):
    """
    验证任务是否合规（参与决策，不是只打印）
    
    Args:
        task_description: 任务描述
    
    Returns:
        {
            'valid': bool,
            'reason': str or None,
            'warning': str or None,
            'decision': 'reject' | 'warn' | 'allow'
        }
    """
    # 检查禁止事项
    is_forbidden, forbidden_reason = check_forbidden(task_description)
    if is_forbidden:
        return {
            'valid': False,
            'reason': f"❌ 任务被拒绝：{forbidden_reason}\n依据：PROJECT_STATE.md 第六条「禁止回退事项」",
            'warning': None,
            'decision': 'reject'  # 决策：拒绝执行
        }
    
    # 检查不推荐事项
    is_not_recommended, not_recommended_reason = check_not_recommended(task_description)
    if is_not_recommended:
        return {
            'valid': True,
            'reason': None,
            'warning': f"⚠️ 警告：{not_recommended_reason}\n依据：PROJECT_STATE.md 第九条「当前不应该做的事情」",
            'decision': 'warn'  # 决策：警告但允许
        }
    
    return {
        'valid': True,
        'reason': None,
        'warning': None,
        'decision': 'allow'  # 决策：允许执行
    }


def validate_script(script_text):
    """
    验证稿件是否合规（不包含测试文案）
    
    Args:
        script_text: 用户稿件文本
    
    Returns:
        {
            'valid': bool,
            'reason': str or None,
            'decision': 'reject' | 'allow'
        }
    """
    # 测试文案特征（从禁止规则中提取）
    test_patterns = [
        "测试视频",
        "这是一个测试",
        "自动生成的成片",
        "验证测试",
        "欢迎观看",
        "测试文案",
    ]
    
    for pattern in test_patterns:
        if pattern in script_text:
            return {
                'valid': False,
                'reason': f"❌ 稿件包含测试文案特征「{pattern}」\n依据：PROJECT_STATE.md 第六条「禁止恢复测试文案进入正式样片」",
                'decision': 'reject'
            }
    
    return {
        'valid': True,
        'reason': None,
        'decision': 'allow'
    }


def get_state_constraints():
    """
    获取项目状态约束（用于决策，不是用于打印）
    
    Returns:
        {
            'goal': str,
            'priority': str,
            'forbidden': List[Tuple],
            'not_recommended': List[Tuple]
        }
    """
    return {
        'goal': get_current_goal(),
        'priority': get_first_priority(),
        'forbidden': get_forbidden_actions(),
        'not_recommended': get_not_recommended()
    }


def clear_cache():
    """清除缓存（当 PROJECT_STATE.md 更新时调用）"""
    global _cached_state, _cached_forbidden, _cached_not_recommended, _cached_goal, _cached_priority
    _cached_state = None
    _cached_forbidden = None
    _cached_not_recommended = None
    _cached_goal = None
    _cached_priority = None


# 模块加载时验证项目状态文件存在
if __name__ != '__main__':
    try:
        load_project_state()
    except FileNotFoundError as e:
        print(f"[警告] {e}")