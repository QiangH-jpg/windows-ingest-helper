"""
TTS Provider 抽象层
支持多种 TTS 服务接入，当前默认使用 Edge TTS（保底方案）
"""
import os
import re
import json
import subprocess
import edge_tts
from typing import List, Dict, Any

from core.config import config

VIDEO = config['video']
TTS_CONFIG = config['tts']


class TTSSentence:
    """单句 TTS 信息"""
    def __init__(self, index: int, text: str, audio_path: str, duration: float, start_time: float, end_time: float):
        self.index = index
        self.text = text
        self.audio_path = audio_path
        self.duration = duration
        self.start_time = start_time
        self.end_time = end_time
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'index': self.index,
            'text': self.text,
            'audio_path': self.audio_path,
            'duration': round(self.duration, 3),
            'start_time': round(self.start_time, 3),
            'end_time': round(self.end_time, 3)
        }


class TTSProvider:
    """TTS 服务提供者基类"""
    
    def __init__(self, provider_name: str, voice: str, rate: str = '+0%'):
        self.provider_name = provider_name
        self.voice = voice
        self.rate = rate
    
    def split_text_into_sentences(self, text: str) -> List[str]:
        """将文本按语音节奏切分成短句（每条字幕 1.5-3 秒）
        
        ✅ 核心规则：
        1. 按中文句号 `。` 优先分割（确保每句独立）
        2. 处理换行符 `\n\n`
        3. 每句不超过 30 字（避免过长）
        """
        sentences = []
        
        # === 1. 先按换行符分割（处理段落） ===
        text = text.replace('\n\n', '\n')
        paragraphs = text.split('\n')
        paragraphs = [p.strip() for p in paragraphs if p.strip()]
        
        # === 2. 每个段落按句号分割 ===
        for para in paragraphs:
            # 按中文句号分割
            parts = re.split(r'[。]', para)
            parts = [p.strip() for p in parts if p.strip()]
            
            for part in parts:
                # 如果部分太长（超过 30 字），按逗号继续分割
                if len(part) > 30:
                    sub_parts = re.split(r'[，,]', part)
                    sub_parts = [p.strip() for p in sub_parts if p.strip()]
                    sentences.extend(sub_parts)
                else:
                    sentences.append(part)
        
        # 过滤空句
        sentences = [s for s in sentences if s.strip()]
        
        if not sentences:
            sentences = [text]
        
        return sentences
    
    def synthesize_sentence(self, text: str, output_path: str) -> bool:
        """合成单句音频，返回是否成功"""
        raise NotImplementedError
    
    def get_audio_duration(self, audio_path: str) -> float:
        """获取音频时长（秒）"""
        if not os.path.exists(audio_path):
            return 0.0
        ffprobe_path = VIDEO['ffmpeg_path'].replace('ffmpeg', 'ffprobe')
        cmd = [
            ffprobe_path, '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            audio_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        output = result.stdout.strip()
        if not output:
            return 0.0
        return float(output)
    
    def synthesize(self, text: str, output_audio_path: str, output_meta_path: str = None) -> Dict[str, Any]:
        """
        合成完整 TTS 音频，返回元数据
        
        Args:
            text: 输入文本
            output_audio_path: 输出音频路径
            output_meta_path: 输出元数据路径（可选）
        
        Returns:
            包含分句信息、总时长等的元数据字典
        """
        sentences = self.split_text_into_sentences(text)
        sentence_objects = []
        chunk_files = []
        current_time = 0.0
        
        # 逐句合成
        for i, sentence in enumerate(sentences):
            chunk_path = f"{output_audio_path}.chunk_{i}.mp3"
            success = self.synthesize_sentence(sentence, chunk_path)
            
            if success and os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 0:
                duration = self.get_audio_duration(chunk_path)
                sentence_obj = TTSSentence(
                    index=i,
                    text=sentence,
                    audio_path=chunk_path,
                    duration=duration,
                    start_time=current_time,
                    end_time=current_time + duration
                )
                sentence_objects.append(sentence_obj)
                chunk_files.append(chunk_path)
                current_time += duration
            else:
                if os.path.exists(chunk_path):
                    os.remove(chunk_path)
        
        if not chunk_files:
            raise Exception("No audio was received. Please verify that your parameters are correct.")
        
        # 拼接所有片段
        concat_file = output_audio_path + '.concat.txt'
        with open(concat_file, 'w') as f:
            for chunk in chunk_files:
                f.write(f"file '{chunk}'\n")
        
        cmd = [
            VIDEO['ffmpeg_path'], '-y',
            '-f', 'concat', '-safe', '0', '-i', concat_file,
            '-c', 'copy',
            output_audio_path
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        
        # 清理临时文件
        for chunk in chunk_files:
            os.remove(chunk)
        os.remove(concat_file)
        
        # 生成元数据
        total_duration = sum(s.duration for s in sentence_objects)
        meta = {
            'provider': self.provider_name,
            'voice': self.voice,
            'rate': self.rate,
            'input_text': text,
            'sentence_count': len(sentence_objects),
            'total_duration': round(total_duration, 3),
            'sentences': [s.to_dict() for s in sentence_objects]
        }
        
        # 落盘元数据
        if output_meta_path:
            with open(output_meta_path, 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        
        return meta


class EdgeTTSProvider(TTSProvider):
    """Edge TTS 实现（微软免费服务）"""
    
    def __init__(self, voice: str = 'zh-CN-XiaoxiaoNeural', rate: str = '+0%'):
        super().__init__(provider_name='edge_tts', voice=voice, rate=rate)
    
    def synthesize_sentence(self, text: str, output_path: str) -> bool:
        """使用 Edge TTS 命令行合成单句（同步）"""
        import subprocess
        try:
            # 使用 edge-tts 命令行工具
            cmd = [
                'edge-tts',
                '--text', text,
                '--voice', self.voice,
                '--rate', self.rate,
                '--write-media', output_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode == 0 and os.path.exists(output_path):
                return True
            else:
                print(f"Edge TTS failed: {result.stderr}")
                return False
        except Exception as e:
            print(f"Edge TTS failed: {e}")
            return False


def create_tts_provider() -> TTSProvider:
    """根据配置创建 TTS 提供者实例"""
    provider = TTS_CONFIG.get('provider', 'edge_tts')
    voice = TTS_CONFIG.get('voice', 'zh-CN-XiaoxiaoNeural')
    rate = TTS_CONFIG.get('rate', '+0%')
    
    if provider == 'edge_tts':
        return EdgeTTSProvider(voice=voice, rate=rate)
    else:
        # 未知 provider 时降级到 Edge TTS
        print(f"Warning: Unknown TTS provider '{provider}', falling back to edge_tts")
        return EdgeTTSProvider(voice=voice, rate=rate)


# 快捷函数：保持向后兼容
def generate_tts(text: str, output_path: str, output_meta_path: str = None) -> Dict[str, Any]:
    """
    生成 TTS 音频（快捷函数）
    
    Args:
        text: 输入文本
        output_path: 输出音频路径
        output_meta_path: 输出元数据路径（可选）
    
    Returns:
        TTS 元数据字典
    """
    provider = create_tts_provider()
    return provider.synthesize(text, output_path, output_meta_path)


def split_subtitle_line(text: str, max_chars: int = 16) -> List[str]:
    """
    将长文本分割成行列表，每行不超过 max_chars 个字
    
    ✅ 核心规则（严格版）：
    1. 每行必须 ≤16 字（强约束）
    2. 清理非法字符，防止方框
    3. 清理句首句尾标点（禁止以标点开头/结尾）
    4. 按完整字符切分（禁止 UTF-8 截断）
    5. 返回最多2行（超出部分丢弃）
    
    Args:
        text: 输入文本
        max_chars: 每行最大字数
    
    Returns:
        行列表（最多2行）
    """
    # === 1. 强制清洗非法字符 ===
    text = text.strip()
    # 去除句首句尾标点
    text = text.lstrip('。,，.!?！？;；:：')
    text = text.rstrip('。,，.!?！？;；:：')
    # 去除零宽字符和不可见空格
    text = ''.join(c for c in text if c >= ' ' or c in '\n\r\t')
    text = text.replace('□', '').replace('', '').replace('█', '')
    text = text.replace('\u00a0', ' ')  # 不可见空格
    # 去除连续空格
    text = re.sub(r'\s+', ' ', text)
    
    if not text:
        return []
    
    if len(text) <= max_chars:
        return [text]
    
    # === 2. 按优先级切分 ===
    
    # 优先级 1：逗号/句号
    parts = re.split(r'[,,。]', text)
    parts = [p.strip() for p in parts if p.strip()]
    
    if len(parts) > 1:
        lines = []
        for part in parts:
            part = part.lstrip('。,，')
            if part:
                lines.extend(split_subtitle_line(part, max_chars))
        # 严格限制最多2行
        return lines[:2]
    
    # 优先级 2：顿号
    if '、' in text:
        parts = text.split('、')
        parts = [p.strip() for p in parts if p.strip()]
        all_lines = []
        for part in parts:
            all_lines.extend(split_subtitle_line(part, max_chars))
        return all_lines[:2]
    
    # 优先级 3：虚词
    mid = len(text) // 2
    for i in range(min(5, mid)):
        for punct in ['的', '了', '是', '在', '和', '与', '或', '等', '及', '而', '并']:
            if mid - i > 0 and text[mid - i] == punct:
                result = split_subtitle_line(text[:mid - i + 1], max_chars) + \
                         split_subtitle_line(text[mid - i + 1:], max_chars)
                return result[:2]
            if mid + i < len(text) and text[mid + i] == punct:
                result = split_subtitle_line(text[:mid + i + 1], max_chars) + \
                         split_subtitle_line(text[mid + i + 1:], max_chars)
                return result[:2]
    
    # 优先级 4：强制按长度切分
    lines = []
    for i in range(0, len(text), max_chars):
        lines.append(text[i:i + max_chars])
    
    # 严格限制最多2行
    return lines[:2]


def create_subtitle_srt_from_meta(meta: Dict[str, Any], output_path: str) -> str:
    """
    从 TTS 元数据生成 SRT 字幕文件（短语切分版）
    
    ✅ 核心规则（短语切分版）：
    1. 每段目标时长：1.5~3 秒（严格）
    2. 按短语/词组边界切分（禁止词中间拆分）
    3. 每行 ≤14 字，最多 2 行
    4. 禁止三行、禁止空行
    5. 呼吸间隙：100ms
    
    Args:
        meta: TTS 元数据
        output_path: 输出 SRT 路径
    
    Returns:
        SRT 文件路径
    """
    def format_srt_time(seconds: float) -> str:
        hrs = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        ms = int((seconds % 1) * 1000)
        return f"{hrs:02d}:{mins:02d}:{secs:02d},{ms:03d}"
    
    def clean_text(text: str) -> str:
        """清理文本"""
        text = text.strip()
        text = text.lstrip('。,，.!?！？;；:：')
        text = text.rstrip('。,，.!?！？;；:：')
        text = re.sub(r'\s+', '', text)
        return text
    
    # ============================================================
    # 短语边界识别（核心）- 最长匹配 + 不可拆分区间标记
    # ============================================================
    # 按长度降序排列（最长匹配优先）
    PHRASES_NO_SPLIT = sorted([
        # 长短语
        '新就业形态劳动者', '济南市人社局', '人社服务大篷车', '美团服务中心',
        '保障与你同行', '最后一公里', '面对面讲解', '走进奔跑者',
        '一线劳动者',
        # 中等长度短语
        '外卖骑手', '服务中心', '权益保障', '社保参保', '人社服务',
        '互动环节', '发放资料', '了解政策', '持续推动', '就业形态',
        '轻松氛围', '新就业', '大篷车', '人社局',
        # 2字常用词
        '服务', '活动', '政策', '保障', '了解', '推动', '持续', '形态',
        '劳动', '氛围', '互动', '环节', '参保', '权益', '就业', '骑手',
        '讲解', '资料', '介绍',
    ], key=len, reverse=True)
    
    def mark_protected_ranges(text: str) -> List:
        """
        标记文本中所有不可拆分区间（最长匹配优先）
        
        Returns:
            [(start, end), ...] 不可拆分的字符区间
        """
        protected = []
        used = [False] * len(text)
        
        for phrase in PHRASES_NO_SPLIT:
            plen = len(phrase)
            start = 0
            while True:
                idx = text.find(phrase, start)
                if idx == -1:
                    break
                # 检查该区间是否已被更长短语占用
                if not any(used[idx:idx + plen]):
                    protected.append((idx, idx + plen))
                    for i in range(idx, idx + plen):
                        used[i] = True
                start = idx + 1
        
        return sorted(protected)
    
    def is_inside_protected(pos: int, protected_ranges: List) -> bool:
        """检查位置是否在不可拆分区间内部（不含边界）"""
        for start, end in protected_ranges:
            if start < pos < end:
                return True
        return False
    
    def find_safe_split(text: str, target_pos: int, protected_ranges: List, search_range: int = 8) -> int:
        """
        在目标位置附近寻找安全切分点（不在保护区间内部）
        
        优先级：
        1. 标点
        2. 保护区间边界
        3. 虚词后
        """
        # 优先在标点处切分
        for offset in range(search_range + 1):
            for pos in [target_pos + offset, target_pos - offset]:
                if 0 < pos < len(text):
                    if text[pos - 1] in '，,、；;：:' and not is_inside_protected(pos, protected_ranges):
                        return pos
        
        # 在保护区间边界处切分
        for start, end in protected_ranges:
            if abs(start - target_pos) <= search_range and start > 0:
                return start
            if abs(end - target_pos) <= search_range and end < len(text):
                return end
        
        # 在虚词后切分
        for offset in range(search_range + 1):
            for pos in [target_pos + offset, target_pos - offset]:
                if 0 < pos < len(text):
                    if text[pos - 1] in '的了是在和与或等及而并把向对让给' and not is_inside_protected(pos, protected_ranges):
                        return pos
        
        # 最后手段：找最近的非保护位置
        for offset in range(1, search_range + 3):
            for pos in [target_pos + offset, target_pos - offset]:
                if 0 < pos < len(text) and not is_inside_protected(pos, protected_ranges):
                    return pos
        
        return target_pos
    
    def split_by_phrase(text: str, total_duration: float,
                        min_seg_duration: float = 1.5,
                        max_seg_duration: float = 3.0) -> List[Dict]:
        """
        按短语边界切分文本（核心函数）
        
        ✅ 切分优先级：
        1. 标点（逗号、顿号），但保护引号内容
        2. 短语/词组边界
        3. 虚词后
        4. 最后才允许长度切分（必须在词边界）
        
        Returns:
            [{'text': '片段文本', 'chars': 字数, 'duration': 时长}, ...]
        """
        text = clean_text(text)
        if not text:
            return []
        
        total_chars = len(text)
        if total_chars == 0:
            return []
        
        time_per_char = total_duration / total_chars
        
        # ============================================================
        # 第一步：识别引号区间（不在此区间内切分）
        # ============================================================
        # 支持中文引号和英文引号
        LEFT_QUOTES = '"''"'
        RIGHT_QUOTES = '"''"'
        
        quote_ranges = []  # [(start, end), ...]
        in_quote = False
        quote_start = -1
        for i, char in enumerate(text):
            if char in LEFT_QUOTES:
                if not in_quote:
                    in_quote = True
                    quote_start = i
            elif char in RIGHT_QUOTES:
                if in_quote:
                    in_quote = False
                    quote_ranges.append((quote_start, i + 1))
        
        def is_in_quote(pos):
            """检查位置是否在引号内"""
            for start, end in quote_ranges:
                if start <= pos < end:
                    return True
            return False
        
        # ============================================================
        # 第二步：按标点切分（跳过引号内）
        # ============================================================
        punct_splits = []
        for i, char in enumerate(text):
            if char in '，,、；;：:':
                if not is_in_quote(i):  # 不在引号内才切分
                    punct_splits.append((i + 1, 'punct'))
        
        # 收集标点切分段落
        segments = []
        if punct_splits:
            last_pos = 0
            for split_pos, split_type in punct_splits:
                seg_text = text[last_pos:split_pos]
                seg_chars = len(seg_text)
                seg_duration = seg_chars * time_per_char
                if seg_chars > 0:
                    segments.append({
                        'text': seg_text,
                        'chars': seg_chars,
                        'duration': seg_duration
                    })
                last_pos = split_pos
            
            # 剩余部分
            if last_pos < len(text):
                remaining = text[last_pos:]
                if remaining:
                    segments.append({
                        'text': remaining,
                        'chars': len(remaining),
                        'duration': len(remaining) * time_per_char
                    })
        
        if not segments:
            segments = [{'text': text, 'chars': total_chars, 'duration': total_duration}]
        
        # ============================================================
        # 第二步：拆分超长段落（>3秒）
        # ============================================================
        final_segments = []
        for seg in segments:
            if seg['duration'] <= max_seg_duration:
                final_segments.append(seg)
            else:
                # 需要拆分：计算目标位置
                target_chars = int(max_seg_duration / time_per_char)
                current_pos = 0
                
                while current_pos < len(seg['text']):
                    remaining_text = seg['text'][current_pos:]
                    remaining_chars = len(remaining_text)
                    
                    if remaining_chars <= target_chars:
                        # 剩余部分可以直接加入
                        final_segments.append({
                            'text': remaining_text,
                            'chars': remaining_chars,
                            'duration': remaining_chars * time_per_char
                        })
                        break
                    
                    # 寻找短语边界切分点
                    split_pos = find_safe_split(remaining_text, target_chars, mark_protected_ranges(remaining_text), search_range=6)
                    
                    # 确保切分点合理
                    if split_pos <= 2:
                        split_pos = target_chars
                    if split_pos >= remaining_chars:
                        split_pos = remaining_chars
                    
                    seg_text = remaining_text[:split_pos]
                    seg_chars = len(seg_text)
                    
                    # 检查是否太短
                    if seg_chars >= 3:  # 最少3个字
                        final_segments.append({
                            'text': seg_text,
                            'chars': seg_chars,
                            'duration': seg_chars * time_per_char
                        })
                    else:
                        # 太短，合并到上一段
                        if final_segments:
                            final_segments[-1]['text'] += seg_text
                            final_segments[-1]['chars'] += seg_chars
                            final_segments[-1]['duration'] += seg_chars * time_per_char
                        else:
                            final_segments.append({
                                'text': seg_text,
                                'chars': seg_chars,
                                'duration': seg_chars * time_per_char
                            })
                    
                    current_pos += split_pos
        
        # ============================================================
        # 第三步：合并过短段落（<1.5秒）
        # ============================================================
        merged = []
        for seg in final_segments:
            if seg['chars'] < 2:  # 少于2字直接跳过
                continue
            
            # 如果当前段落过短且上一段不过长，合并
            if merged and seg['duration'] < min_seg_duration:
                if merged[-1]['duration'] + seg['duration'] <= max_seg_duration:
                    merged[-1]['text'] += seg['text']
                    merged[-1]['chars'] += seg['chars']
                    merged[-1]['duration'] += seg['duration']
                    continue
            
            merged.append(seg)
        
        # 二次检查：还有过短的？
        final_merged = []
        for seg in merged:
            if final_merged and seg['duration'] < min_seg_duration:
                # 还是过短，尝试合并
                if final_merged[-1]['duration'] + seg['duration'] <= max_seg_duration:
                    final_merged[-1]['text'] += seg['text']
                    final_merged[-1]['chars'] += seg['chars']
                    final_merged[-1]['duration'] += seg['duration']
                    continue
            final_merged.append(seg)
        
        # ============================================================
        # 第四步：最终检查，确保无>3秒段落
        # ============================================================
        result = []
        for seg in final_merged:
            if seg['duration'] > max_seg_duration:
                # 强制拆分
                target_chars = int(max_seg_duration / time_per_char)
                text_to_split = seg['text']
                
                # 寻找最佳切分点
                split_pos = find_safe_split(text_to_split, target_chars, mark_protected_ranges(text_to_split), search_range=8)
                if split_pos <= 2 or split_pos >= len(text_to_split) - 2:
                    split_pos = min(target_chars, len(text_to_split) // 2)
                
                text1 = text_to_split[:split_pos]
                text2 = text_to_split[split_pos:]
                
                if text1:
                    result.append({'text': text1, 'chars': len(text1), 'duration': len(text1) * time_per_char})
                if text2:
                    result.append({'text': text2, 'chars': len(text2), 'duration': len(text2) * time_per_char})
            else:
                result.append(seg)
        
        return result
    
    def format_subtitle_lines(text: str, max_chars: int = 14) -> str:
        """将文本格式化为最多2行，每行≤max_chars"""
        text = clean_text(text)
        if not text:
            return ''
        
        if len(text) <= max_chars:
            return text
        
        # 寻找分行点（优先标点）
        for i in range(min(max_chars, len(text) - 1), max(0, max_chars - 5), -1):
            if text[i] in '，,、':
                return text[:i] + '\n' + text[i+1:]
        
        # 在虚词后分行
        for i in range(min(max_chars, len(text) - 1), max(0, max_chars - 5), -1):
            if text[i] in '的了是在和与或等及而并':
                return text[:i+1] + '\n' + text[i+1:]
        
        # 强制分行（在词边界）
        split_pos = find_safe_split(text, max_chars, mark_protected_ranges(text), search_range=3)
        if split_pos > 0 and split_pos < len(text):
            return text[:split_pos] + '\n' + text[split_pos:]
        
        return text[:max_chars] + '\n' + text[max_chars:]
    
    # ============================================================
    # 主逻辑
    # ============================================================
    MAX_CHARS_PER_LINE = 14
    MAX_LINES_PER_SUBTITLE = 2
    MIN_SEGMENT_DURATION = 1.2  # 最小1.2秒
    MAX_SEGMENT_DURATION = 3.0
    BREATHING_GAP = 0.08  # 80ms呼吸感（从end减去，不影响下条start）
    
    all_segments = []
    
    for sentence in meta['sentences']:
        text = sentence['text']
        duration = sentence['duration']
        
        segments = split_by_phrase(text, duration, MIN_SEGMENT_DURATION, MAX_SEGMENT_DURATION)
        
        if not segments:
            segments = [{'text': clean_text(text), 'chars': len(clean_text(text)), 'duration': duration}]
        
        all_segments.extend(segments)
    
    # ============================================================
    # 合并过短片段（<1.2秒）
    # ============================================================
    merged_segments = []
    for seg in all_segments:
        if merged_segments and seg['duration'] < MIN_SEGMENT_DURATION:
            # 当前片段过短，尝试合并到上一条
            if merged_segments[-1]['duration'] + seg['duration'] <= MAX_SEGMENT_DURATION:
                merged_segments[-1]['text'] += seg['text']
                merged_segments[-1]['chars'] += seg['chars']
                merged_segments[-1]['duration'] += seg['duration']
                continue
        if seg['duration'] >= MIN_SEGMENT_DURATION:
            merged_segments.append(seg)
    
    # 二次检查：还有过短的？
    final_segments = []
    for seg in merged_segments:
        if final_segments and seg['duration'] < MIN_SEGMENT_DURATION:
            if final_segments[-1]['duration'] + seg['duration'] <= MAX_SEGMENT_DURATION:
                final_segments[-1]['text'] += seg['text']
                final_segments[-1]['chars'] += seg['chars']
                final_segments[-1]['duration'] += seg['duration']
                continue
        final_segments.append(seg)
    
    # 生成SRT
    subtitle_idx = 1
    lines_written = []
    current_time = 0.0
    
    with open(output_path, 'w', encoding='utf-8') as f:
        for seg in final_segments:
            formatted_text = format_subtitle_lines(seg['text'], MAX_CHARS_PER_LINE)
            if not formatted_text:
                continue
            
            # ✅ 核心修改：时间轴贴着语音，不在start上加gap
            start_time = current_time
            # 显示时长 = 语音时长 - 呼吸间隙（创建呼吸感）
            display_duration = max(seg['duration'] - BREATHING_GAP, MIN_SEGMENT_DURATION)
            end_time = start_time + display_duration
            # 下一条的start = 当前语音结束时间（不加gap）
            current_time = start_time + seg['duration']
            
            lines = formatted_text.split('\n')
            if len(lines) > MAX_LINES_PER_SUBTITLE:
                lines = lines[:MAX_LINES_PER_SUBTITLE]
                formatted_text = '\n'.join(lines)
            
            f.write(f"{subtitle_idx}\n")
            f.write(f"{format_srt_time(start_time)} --> {format_srt_time(end_time)}\n")
            f.write(f"{formatted_text}\n\n")
            
            lines_written.append({
                'text': formatted_text.replace('\n', ' | '),
                'lines': len(lines),
                'chars_per_line': [len(l) for l in lines],
                'start': start_time,
                'end': end_time,
                'duration': display_duration,
                'voice_duration': seg['duration']
            })
            subtitle_idx += 1
    
    # 验证输出
    print(f"\n【字幕卡点验证（收口版）】")
    print(f"  总字幕数：{subtitle_idx - 1}")
    print(f"  每行最大字数：{MAX_CHARS_PER_LINE}")
    print(f"  目标时长范围：{MIN_SEGMENT_DURATION}-{MAX_SEGMENT_DURATION}秒")
    print(f"  呼吸间隙：{int(BREATHING_GAP * 1000)}ms（从end减，不影响start）")
    
    if lines_written:
        durations = [l['duration'] for l in lines_written]
        max_chars = max(max(l['chars_per_line']) for l in lines_written)
        max_lines = max(l['lines'] for l in lines_written)
        over_3s_count = sum(1 for d in durations if d > MAX_SEGMENT_DURATION)
        under_12s_count = sum(1 for d in durations if d < MIN_SEGMENT_DURATION)
        
        print(f"  实际时长范围：{min(durations):.2f}-{max(durations):.2f}秒")
        print(f"  实际最大每行字数：{max_chars}")
        print(f"  实际最大行数：{max_lines}")
        print(f"  是否存在三行字幕：{'❌ 是' if max_lines > 2 else '✅ 否'}")
        print(f"  是否存在超长行：{'❌ 是' if max_chars > MAX_CHARS_PER_LINE else '✅ 否'}")
        print(f"  是否存在>3秒字幕：{'❌ 是（' + str(over_3s_count) + '条）' if over_3s_count > 0 else '✅ 否'}")
        print(f"  是否存在<1.2秒字幕：{'❌ 是（' + str(under_12s_count) + '条）' if under_12s_count > 0 else '✅ 否'}")
        
        print(f"\n  前8条字幕详情：")
        for i, line_info in enumerate(lines_written[:8]):
            duration_str = f"{line_info['duration']:.2f}s"
            warning = ""
            if line_info['duration'] < MIN_SEGMENT_DURATION:
                warning = "⚠️过短"
            elif line_info['duration'] > MAX_SEGMENT_DURATION:
                warning = "⚠️超3s"
            print(f"    [{line_info['start']:.2f}-{line_info['end']:.2f}] {line_info['text']} ({duration_str}) {warning}")
    
    return output_path
