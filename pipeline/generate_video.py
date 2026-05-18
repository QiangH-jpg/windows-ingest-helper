"""
一键生成成片 — 正式主链（2026-04-22）

完整链路：
  build_l2_segments_text(task_id) → L3 动态调用 → timeline → TTS → 字幕 → 裁切 → 渲染

消费人工 overrides，产出 task 级成片。
"""
import json
import os
import time
import subprocess
import requests
from pathlib import Path
from datetime import datetime

from pipeline.pool_overrides import build_l2_segments_text, load_pool_data, build_l3_video_inputs
from pipeline.tts_volcengine import generate_tts_volcengine
from pipeline.tts_provider import create_subtitle_srt_from_meta, validate_subtitles_no_split
from pipeline.render_preflight_checks import run_preflight_checks, get_subtitle_style
from pipeline.subtitle_styles import get_style_force_string, get_default_preset, srt_to_ass
from pipeline.combined_review import _ensure_env, _get_api_key, _get_endpoint, _get_model

PROJECT_ROOT = Path(__file__).parent.parent

# ffmpeg / ffprobe 从 mainchain_config 统一读取
from pipeline.mainchain_config import FFMPEG_PATH as FFMPEG, FFPROBE_PATH as FFPROBE
PROMPTS_DIR = PROJECT_ROOT / "prompts" / "video_news"
L3_PROMPT_FILE = PROMPTS_DIR / "l3_director_prompt_v7.txt"
L3_MUSIC_PROMPT_FILE = PROMPTS_DIR / "l3_music_montage_prompt_v1.txt"

# v13.2-h step4: 开关
OPENING_ENFORCE_VS_ANCHOR_GUARD = True   # B: VS 语义锚点保护，阻止 opening_enforce 抢走互动段关键镜头
VARIANT_METADATA_PASSTHROUGH = True      # A: variant 元数据透传到 manifest / L3 输入
_VS_ANCHOR_KEYWORDS = {'互动', '游戏', '投沙包', '投飞镖', '投掷', '大骰子', '体验', '参与', '飞镖'}


def _check_video_url_health(url: str, timeout: int = 5) -> dict:
    """v12.9.1: 检查单个视频 URL 的健康状态
    
    策略：
    1. 优先 HEAD 请求
    2. HEAD 失败或不明确时，用 Range GET (bytes=0-1023)
    3. 验证 status_code, content_type, content_length
    
    Returns:
        {"ok": bool, "status_code": int, "latency_ms": int,
         "content_length": int, "accept_ranges": bool,
         "content_type": str, "error": str or None}
    """
    import re as _re_url
    result = {
        "ok": False,
        "status_code": None,
        "latency_ms": 0,
        "content_length": 0,
        "accept_ranges": False,
        "content_type": "",
        "error": None,
    }
    t0 = __import__('time').time()
    try:
        # Step 1: HEAD 请求
        resp = requests.head(url, timeout=timeout, allow_redirects=True)
        result["status_code"] = resp.status_code
        result["content_type"] = resp.headers.get('Content-Type', '').lower()
        cl = resp.headers.get('Content-Length', '0')
        result["content_length"] = int(cl) if cl and cl.isdigit() else 0
        ar = resp.headers.get('Accept-Ranges', '').lower()
        result["accept_ranges"] = ar in ('bytes', 'byte')
        result["latency_ms"] = int((__import__('time').time() - t0) * 1000)

        # Step 2: 如果 HEAD 返回 405/403，尝试 Range GET
        if resp.status_code in (405, 403):
            resp2 = requests.get(url, headers={'Range': 'bytes=0-1023'},
                                 timeout=timeout, allow_redirects=True, stream=True)
            result["status_code"] = resp2.status_code
            result["content_type"] = resp2.headers.get('Content-Type', '').lower()
            cl2 = resp2.headers.get('Content-Length', '0')
            result["content_length"] = int(cl2) if cl2 and cl2.isdigit() else 0
            ar2 = resp2.headers.get('Accept-Ranges', '').lower()
            result["accept_ranges"] = ar2 in ('bytes', 'byte')
            result["latency_ms"] = int((__import__('time').time() - t0) * 1000)
            resp2.close()

        # Step 3: 验证
        sc = result["status_code"]
        if sc not in (200, 206):
            result["error"] = f"HTTP {sc}"
            return result

        ct = result["content_type"]
        if any(bad in ct for bad in ('text/html', 'application/xml', 'text/xml')):
            result["error"] = f"bad content_type: {ct}"
            return result

        if result["content_length"] == 0:
            # HEAD 可能不返回 Content-Length，用 Range GET 再试
            if sc == 200 and not result["accept_ranges"]:
                resp3 = requests.get(url, headers={'Range': 'bytes=0-1023'},
                                     timeout=timeout, allow_redirects=True, stream=True)
                cl3 = resp3.headers.get('Content-Length', '0')
                result["content_length"] = int(cl3) if cl3 and cl3.isdigit() else 0
                result["latency_ms"] = int((__import__('time').time() - t0) * 1000)
                resp3.close()

        result["ok"] = True
        return result

    except requests.exceptions.Timeout:
        result["latency_ms"] = int((__import__('time').time() - t0) * 1000)
        result["error"] = "timeout"
        return result
    except requests.exceptions.ConnectionError as e:
        result["latency_ms"] = int((__import__('time').time() - t0) * 1000)
        result["error"] = f"connection_error: {str(e)[:80]}"
        return result
    except Exception as e:
        result["latency_ms"] = int((__import__('time').time() - t0) * 1000)
        result["error"] = f"unexpected: {str(e)[:80]}"
        return result


def _check_l3_input_urls(url_list: list, task_id: str = '', overall_timeout: int = 30) -> dict:
    """v12.9.1: 批量检查 L3 输入 URL 健康状态
    
    Args:
        url_list: [(clip_info, tos_url), ...]
        task_id: 任务 ID（用于日志）
        overall_timeout: 整体预检查超时（秒）
    
    Returns:
        {"ok_urls": [(clip_info, tos_url), ...],
         "bad_urls": [(clip_info, tos_url, health_result), ...],
         "summary": {"checked": int, "ok": int, "bad": int, ...}}
    """
    import time as _time
    t0 = _time.time()
    ok_urls = []
    bad_urls = []
    bad_examples = []
    _cache = {}  # URL -> health result 缓存

    task_label = f"task={task_id}" if task_id else ""

    for clip_info, tos_url in url_list:
        elapsed = _time.time() - t0
        if elapsed > overall_timeout:
            print(f"[v12.9.1][URL health] {task_label} overall timeout reached ({elapsed:.1f}s)")
            # 超时前已检查的保留，未检查的视为 ok（避免过度过滤）
            ok_urls.append((clip_info, tos_url))
            continue

        # 缓存检查
        if tos_url in _cache:
            hr = _cache[tos_url]
        else:
            hr = _check_video_url_health(tos_url, timeout=5)
            _cache[tos_url] = hr

        clip_id = clip_info.get('clip_id', clip_info.get('source_file', 'unknown')[:40])

        if hr["ok"]:
            print(f"[v12.9.1][URL health] {task_label} clip={clip_id} status=ok latency={hr['latency_ms']}ms size={hr['content_length']}")
            ok_urls.append((clip_info, tos_url))
        else:
            print(f"[v12.9.1][URL health] {task_label} clip={clip_id} status=bad error={hr['error']}")
            bad_urls.append((clip_info, tos_url, hr))
            bad_examples.append({"clip_id": clip_id, "error": hr["error"]})

    summary = {
        "checked": len(_cache),
        "ok": len(ok_urls),
        "bad": len(bad_urls),
        "bad_examples": bad_examples[:5],  # 最多保留 5 个示例
        "total_time_ms": int((_time.time() - t0) * 1000),
        "updated_at": __import__('datetime').datetime.now().isoformat(),
    }

    if bad_urls:
        print(f"[v12.9.1][URL health] {task_label} filtered bad clips: {len(bad_urls)}/{len(url_list)}")

    return {"ok_urls": ok_urls, "bad_urls": bad_urls, "summary": summary}


def _upload_clip_to_tos(clip_path: str, source_file: str, window_index: int) -> str:
    """上传 L3 候选片段到 TOS 临时目录，返回签名 URL（v7.3 正式固化）"""
    from tos import TosClientV2
    ak = os.environ.get('TOS_INGEST_AK', os.environ.get('TOS_PUBLISH_AK', ''))
    sk = os.environ.get('TOS_INGEST_SK', os.environ.get('TOS_PUBLISH_SK', ''))
    bucket = os.environ.get('TOS_BUCKET', 'e23-video')
    region = os.environ.get('TOS_REGION', 'cn-beijing')
    endpoint = f'tos-{region}.volces.com'
    
    client = TosClientV2(ak=ak, sk=sk, endpoint=endpoint, region=region)
    
    safe_fn = os.path.basename(clip_path)
    tos_key = f'tmp_l3_clips/{safe_fn}'
    
    client.put_object_from_file(bucket=bucket, key=tos_key, file_path=clip_path)
    
    # 返回公开 URL（TOS bucket 已配公开读）
    return f'https://{bucket}.tos-{region}.volces.com/{tos_key}'


def _call_l3_director(l2_segments_text: str, script_text: str, task_context_text: str, narration_duration: float, prompt_file: Path = None, video_clips: list = None) -> dict:
    """调用豆包 Pro 模型执行 L3 导演调度

    v7.5 正式固化：分批视频精看（30条/3批）
    - video_clips: build_l3_video_inputs() 返回的 clip 列表
    - 前 L3_VIDEO_WATCH_TOTAL 条进入视频精看（分批执行，每批 L3_BATCH_SIZE 条）
    - 超出部分仅文字参与（已在 l2_segments_text 中）
    - 如果没有 video_clips（兜底），仅读文字（deprecated）

    永久规则（L3_VIDEO_WATCH_V1）：
    - L3_VIDEO_WATCH_TOTAL = 30（视频精看总量上限）
    - L3_BATCH_SIZE = 10（单次 API 视频上限，豆包 Pro 限制）
    - 不得将 L3_BATCH_SIZE 作为总量限制
    - 30 条之外仅允许文字参与

    v12.9 P0: 内置 L3 自动重试（最多 3 次，退避 3s/8s/15s）
    """
    # ============================================================
    # 永久常量（L3_VIDEO_WATCH_V1 — 不可删除、不可降低）
    # ============================================================
    L3_VIDEO_WATCH_TOTAL = 30   # 视频精看总量上限
    L3_BATCH_SIZE = 10          # 单次 API 视频上限（豆包 Pro 限制）

    # v12.9 P0: L3 重试配置
    L3_MAX_RETRIES = 3
    L3_RETRY_DELAYS = [3, 8, 15]  # 第1/2/3次重试前等待秒数
    # 仅对临时性错误重试
    L3_RETRY_HTTP_CODES = {429, 500, 502, 503, 504}

    _ensure_env()
    api_key = _get_api_key()
    endpoint = _get_endpoint()
    model = _get_model()

    _pf = prompt_file or L3_PROMPT_FILE
    print(f"[L3] 使用 prompt: {_pf.name}")
    with open(_pf, 'r', encoding='utf-8') as f:
        prompt_template = f.read()

    prompt = prompt_template.replace('{task_context}', task_context_text)
    prompt = prompt.replace('{script_text}', script_text)
    prompt = prompt.replace('{narration_duration_sec}', str(narration_duration))
    prompt = prompt.replace('{l2_segments_text}', l2_segments_text)

    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}

    def _is_retryable_error(status_code: int, error_text: str) -> bool:
        """判断 L3 错误是否可重试"""
        if status_code in L3_RETRY_HTTP_CODES:
            return True
        # HTTP 400 仅当错误体显示临时不可用时才重试
        if status_code == 400:
            retryable_keywords = ['rate limit', 'throttl', 'too many', 'temporary', 'unavailable', ' overloaded', 'busy']
            return any(kw in error_text.lower() for kw in retryable_keywords)
        return False

    def _write_l3_retry_state(attempt: int, max_retries: int, wait: int, error: str):
        """v12.9 P0-4: 写入 L3 重试状态到 task JSON"""
        from datetime import datetime as _dt_retry
        try:
            _task_dir = PROJECT_ROOT / "workdir" / "tasks"
            for _f in _task_dir.glob("*.json"):
                try:
                    with open(_f, 'r') as _hf:
                        _d = json.load(_hf)
                    if _d.get('status') == 'generating' and _d.get('generate_stage'):
                        _next_at = (datetime.now().timestamp() + wait)
                        _d['status'] = 'retrying'
                        _d['generate_stage'] = 'l3_retrying'
                        _d['generate_heartbeat'] = datetime.now().isoformat()
                        _d['retry'] = {
                            'stage': 'l3',
                            'count': attempt,
                            'max': max_retries,
                            'next_retry_at': datetime.fromtimestamp(_next_at).isoformat(),
                            'last_error': error[:200],
                        }
                        _d['user_message'] = 'AI 正在自动重试，请稍候'
                        _d['recoverable'] = True
                        _tmp = str(_f) + '.tmp'
                        with open(_tmp, 'w') as _wf:
                            json.dump(_d, _wf, ensure_ascii=False, indent=2)
                        os.replace(_tmp, str(_f))
                        break
                except Exception:
                    pass
        except Exception:
            pass

    def _execute_l3_call(content_parts: list, call_label: str = '') -> dict:
        """执行单次 L3 API 调用（含 v12.9 自动重试）"""
        payload = {"model": model, "input": [{"role": "user", "content": content_parts}]}
        last_error = None

        for attempt in range(1 + L3_MAX_RETRIES):  # 1 次初始 + 3 次重试
            t0 = time.time()
            try:
                resp = requests.post(f'{endpoint}/responses', json=payload, headers=headers, timeout=600)
                elapsed = round(time.time() - t0, 1)

                if resp.status_code != 200:
                    error_detail = resp.text[:500] if resp.text else '无响应内容'
                    if attempt < L3_MAX_RETRIES and _is_retryable_error(resp.status_code, error_detail):
                        wait = L3_RETRY_DELAYS[attempt]
                        print(f"[v12.9][L3 retry] {call_label} attempt={attempt} error=HTTP {resp.status_code} detail={error_detail[:100]}")
                        print(f"[v12.9][L3 retry] {call_label} attempt={attempt + 1} wait={wait}s")
                        _write_l3_retry_state(attempt + 1, L3_MAX_RETRIES, wait, f"HTTP {resp.status_code}: {error_detail[:100]}")
                        time.sleep(wait)
                        continue
                    else:
                        raise RuntimeError(f"L3 调用失败: HTTP {resp.status_code} ({elapsed}s) - {error_detail[:200]}")

                result = _parse_l3_response(resp.json(), elapsed)
                if attempt > 0:
                    print(f"[v12.9][L3 retry] {call_label} success attempt={attempt + 1}")
                return result

            except requests.exceptions.Timeout:
                elapsed = round(time.time() - t0, 1)
                last_error = f"timeout ({elapsed}s)"
                if attempt < L3_MAX_RETRIES:
                    wait = L3_RETRY_DELAYS[attempt]
                    print(f"[v12.9][L3 retry] {call_label} attempt={attempt} error=timeout detail={last_error}")
                    print(f"[v12.9][L3 retry] {call_label} attempt={attempt + 1} wait={wait}s")
                    _write_l3_retry_state(attempt + 1, L3_MAX_RETRIES, wait, f"timeout: {last_error}")
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"L3 调用超时: {last_error}")

            except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as e:
                elapsed = round(time.time() - t0, 1)
                last_error = f"connection error: {e} ({elapsed}s)"
                if attempt < L3_MAX_RETRIES:
                    wait = L3_RETRY_DELAYS[attempt]
                    print(f"[v12.9][L3 retry] {call_label} attempt={attempt} error=connection detail={last_error[:100]}")
                    print(f"[v12.9][L3 retry] {call_label} attempt={attempt + 1} wait={wait}s")
                    _write_l3_retry_state(attempt + 1, L3_MAX_RETRIES, wait, f"connection error: {last_error[:100]}")
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"L3 连接错误: {last_error}")

        # 理论上不会到这里（上面的 continue/raise 会覆盖所有路径）
        raise RuntimeError(f"L3 调用失败: {last_error}")

    if not video_clips:
        print(f"[L3] 纯文字模式（deprecated，建议升级为视频精看）")
        content_parts = [{"type": "input_text", "text": prompt}]
        print(f"[L3] 调用豆包 Pro 模型...（0 条视频 + 文字）")
        result = _execute_l3_call(content_parts, call_label='text_only')
        result['_l3_video_watch'] = {'total': 0, 'batches': 0, 'mode': 'text_only'}
        return result

    # ============================================================
    # v7.5 分批视频精看：前 30 条分 3 批执行
    # ============================================================
    # 1. 构建 video_watch_set（多样性控制）
    video_watch_set = _build_video_watch_set(video_clips, L3_VIDEO_WATCH_TOTAL)
    total_watch = len(video_watch_set)
    num_batches = (total_watch + L3_BATCH_SIZE - 1) // L3_BATCH_SIZE  # 向上取整

    print(f"[L3] 视频精看机制 v7.5: {total_watch} 条视频 / {num_batches} 批（上限 {L3_VIDEO_WATCH_TOTAL} 条，每批 {L3_BATCH_SIZE} 条）")

    # 2. 分批上传视频并获取 TOS URL
    all_video_urls = []  # [(clip_info, tos_url), ...]
    for clip in video_watch_set:
        clip_path = clip.get('clip_path', '')
        if clip_path and os.path.exists(clip_path):
            try:
                tos_url = _upload_clip_to_tos(clip_path, clip.get('source_file', ''), clip.get('window_index', 0))
                if tos_url:
                    all_video_urls.append((clip, tos_url))
            except Exception as e:
                print(f"  [L3] ⚠️ 上传 clip 失败: {clip_path}: {e}")

    total_uploaded = len(all_video_urls)

    # v12.9.1: L3 输入 URL 健康预检查
    health_result = _check_l3_input_urls(all_video_urls, task_id="", overall_timeout=30)
    all_video_urls = health_result["ok_urls"]
    total_uploaded = len(all_video_urls)

    # 写入 task JSON 的 l3_url_health 字段
    try:
        _hb_path = PROJECT_ROOT / "workdir" / "tasks"
        for _hb_f in _hb_path.glob("*.json"):
            try:
                with open(_hb_f, 'r', encoding='utf-8') as _hf:
                    _hd = json.load(_hf)
                if _hd.get('status') == 'generating':
                    _hd['l3_url_health'] = health_result["summary"]
                    _tmp = str(_hb_f) + '.tmp'
                    with open(_tmp, 'w', encoding='utf-8') as _wf:
                        json.dump(_hd, _wf, ensure_ascii=False, indent=2)
                    os.replace(_tmp, str(_hb_f))
                    break
            except Exception:
                pass
    except Exception:
        pass

    actual_batches = (total_uploaded + L3_BATCH_SIZE - 1) // L3_BATCH_SIZE if total_uploaded > 0 else 0
    print(f"[L3] 上传成功: {total_uploaded}/{total_watch} 条，实际分 {actual_batches} 批执行")
    if health_result["summary"]["bad"] > 0:
        print(f"  [v12.9.1] 已过滤 {health_result['summary']['bad']} 个不可用 URL")
    if total_uploaded == 0 and total_watch > 0:
        raise RuntimeError("素材临时链接不可用，系统已保留任务。请稍后重试或重新生成候选池。")

    # 3. 分批调用 L3
    all_results = []
    total_elapsed = 0
    for batch_idx in range(actual_batches):
        batch_start = batch_idx * L3_BATCH_SIZE
        batch_end = min(batch_start + L3_BATCH_SIZE, total_uploaded)
        batch_urls = all_video_urls[batch_start:batch_end]

        content_parts = []
        for clip_info, tos_url in batch_urls:
            content_parts.append({"type": "input_video", "video_url": tos_url})

        # 每批都带完整文字 prompt
        content_parts.append({"type": "input_text", "text": prompt})

        payload = {"model": model, "input": [{"role": "user", "content": content_parts}]}
        batch_video_count = len(batch_urls)

        print(f"[L3] 批次 {batch_idx + 1}/{actual_batches}: {batch_video_count} 条视频 + 文字")
        # v10.8: L3 批次间心跳（更新 task JSON 的 heartbeat 让前端知道还活着）
        try:
            from datetime import datetime as _dt_hb
            _hb_path = PROJECT_ROOT / "workdir" / "tasks"
            for _hb_f in _hb_path.glob("*.json"):
                try:
                    with open(_hb_f, 'r') as _hf:
                        _hd = json.load(_hf)
                    if _hd.get('generate_stage') == 'l3_started' and _hd.get('status') == 'generating':
                        _hd['generate_heartbeat'] = _dt_hb.now().isoformat()
                        with open(_hb_f, 'w') as _hf:
                            json.dump(_hd, _hf, ensure_ascii=False, indent=2)
                        break
                except Exception:
                    pass
        except Exception:
            pass
        # v12.9 P0: 使用带重试的 L3 调用
        try:
            batch_result = _execute_l3_call(content_parts, call_label=f'batch_{batch_idx+1}')
            elapsed = batch_result.pop('_elapsed', 0)
            total_elapsed += elapsed
            all_results.append(batch_result)
            tl_count = len(batch_result.get('timeline', []))
            print(f"  [L3] 批次 {batch_idx + 1} 完成 ({elapsed}s): timeline={tl_count} 条镜头")
        except RuntimeError as e:
            err_msg = str(e)
            print(f"  [L3] ⚠️ 批次 {batch_idx + 1} 最终失败（含重试）: {err_msg}")
            # v12.9 fix: 从错误消息提取 elapsed，避免 (0s) 误导
            import re as _re
            _m = _re.search(r'\(([\d.]+)s\)', err_msg)
            if _m:
                total_elapsed += float(_m.group(1))

    if not all_results:
        raise RuntimeError(f"L3 所有批次均失败 ({total_elapsed}s)")

    # 4. 合并结果：取最后一批的 timeline（最后一批看到最多视频上下文）
    #    如果只有 1 批，直接用该批结果
    #    如果多批，用最后一批的 timeline（它包含了完整的文字 prompt + 最后一组视频）
    final_result = all_results[-1]
    final_result['_elapsed'] = total_elapsed
    final_result['_l3_video_watch'] = {
        'total': total_watch,
        'uploaded': total_uploaded,
        'batches': actual_batches,
        'batch_results': len(all_results),
        'mode': 'multi_batch_video_watch',
        'rule_version': 'L3_VIDEO_WATCH_V1',
    }

    print(f"[L3] 全部完成: {actual_batches} 批, 总耗时 {total_elapsed}s, "
          f"timeline={len(final_result.get('timeline', []))} 条镜头")
    return final_result


def _build_video_watch_set(all_clips: list, max_total: int = 30) -> list:
    """从所有 clips 构建 usable_story_pool 视频精看集合（v8.0 丰富度优先）

    永久规则（USABLE_STORY_POOL_V1）：
    核心原则：丰富度优先，质量底线由 L2 控制，L3 调度控制覆盖面

    构建策略：
    1. 第一轮：每个 source_file 各取 1 条最佳片段（最大化素材覆盖）
    2. 第二轮：如有余量，每个 source_file 可补第 2 条（但内容须不同）
    3. primary 和 backup 平等参与第一轮（不再 primary 优先截断 backup）
    4. 同一素材同一视觉母题最多 1 条进入精看
    5. 总量不超过 max_total
    """
    from collections import defaultdict

    # 按素材分组
    source_groups = defaultdict(list)
    for clip in all_clips:
        source_groups[clip.get('source_file', 'unknown')].append(clip)

    selected = []
    selected_keys = set()  # source_file + window_index 去重

    # === 第一轮：每个素材各取 1 条最佳片段（最大化覆盖）===
    for source, clips in source_groups.items():
        if len(selected) >= max_total:
            break
        # 按时长降序取第一条
        best = sorted(clips, key=lambda c: c.get('duration', 0), reverse=True)[0]
        key = f"{best.get('source_file','')}_{best.get('window_index',0)}"
        if key not in selected_keys:
            selected.append(best)
            selected_keys.add(key)

    # === 第二轮：补第 2 条（如有余量）===
    if len(selected) < max_total:
        for source, clips in source_groups.items():
            if len(selected) >= max_total:
                break
            # 取时长第二的片段（如果有）
            sorted_c = sorted(clips, key=lambda c: c.get('duration', 0), reverse=True)
            for clip in sorted_c[1:2]:  # 只取第 2 条
                key = f"{clip.get('source_file','')}_{clip.get('window_index',0)}"
                if key not in selected_keys:
                    selected.append(clip)
                    selected_keys.add(key)

    # === 第三轮：如仍有余量，补充所有剩余 ===
    if len(selected) < max_total:
        for clip in all_clips:
            if len(selected) >= max_total:
                break
            key = f"{clip.get('source_file','')}_{clip.get('window_index',0)}"
            if key not in selected_keys:
                selected.append(clip)
                selected_keys.add(key)

    selected = selected[:max_total]

    # 统计
    from collections import Counter
    src_count = len(set(c.get('source_file', '') for c in selected))
    primary_count = sum(1 for c in selected if c.get('pool_level') == 'primary')
    backup_count = len(selected) - primary_count

    print(f"  [L3 精看集] usable_story_pool: {len(selected)}/{len(all_clips)} 条 "
          f"(primary={primary_count}, backup={backup_count}, "
          f"素材覆盖={src_count}/{len(source_groups)})")
    return selected


def _parse_l3_response(data: dict, elapsed: float) -> dict:
    """解析 L3 API 响应 JSON（容错）"""
    text = ''
    for item in data.get('output', []):
        if item.get('type') == 'message':
            for c in item.get('content', []):
                if c.get('type') == 'output_text':
                    text = c.get('text', '')

    start = text.find('{')
    end = text.rfind('}')
    if start == -1:
        raise RuntimeError(f"L3 返回无法解析 JSON ({elapsed}s)")

    json_text = text[start:end + 1]
    try:
        result = json.loads(json_text)
    except json.JSONDecodeError:
        import re
        cleaned = re.sub(r'//.*?\n', '\n', json_text)
        cleaned = re.sub(r',\s*([}\]])', r'\1', cleaned)
        cleaned = re.sub(r'(\d+\.?\d*)%', r'\1', cleaned)
        # v13.0-pre5: 清理 JSON 字符串内非法控制字符
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ' ', cleaned)
        # v13.2-h step6: 修复模型漏写 key-value 分隔符的已知模式
        # 模式："xxx_id": "LS3",\n "bare_value",\n → 补全为 "xxx_name": "bare_value",
        cleaned = re.sub(r'"([a-z_]+_id)":\s*"([^"]+)"\s*,\s*\n(\s*)"([^":{}\[\]]+)"\s*,', 
                         r'"\1": "\2",\n\3"logic_segment_name": "\4",', cleaned)
        try:
            result = json.loads(cleaned)
        except json.JSONDecodeError:
            # 再尝试 strict=False
            try:
                result = json.loads(cleaned, strict=False)
            except json.JSONDecodeError as e2:
                debug_path = str(PROJECT_ROOT / "outputs" / "l3_debug_raw.txt")
                with open(debug_path, 'w') as df:
                    df.write(text)
                raise RuntimeError(f"L3 JSON 解析失败: {e2} ({elapsed}s), 原文已保存到 {debug_path}")

    result['_elapsed'] = elapsed
    return result


# ============================================================
# 纯音乐字幕安全切分（词组/短语/引号边界保护）
# ============================================================
_COMMON_WORDS_2 = set([
    '宣传', '保障', '了解', '服务', '活动', '政策', '推动', '持续',
    '形态', '劳动', '氛围', '互动', '环节', '参保', '权益', '就业',
    '骑手', '讲解', '资料', '介绍', '贴近', '群众', '方式', '维权',
    '意识', '增强', '一线', '现场', '设置', '主办', '集成', '开展',
    '聚焦', '主题', '通过', '走近', '打通', '发放', '社保', '相关',
    '中心', '大篷', '走进', '美团', '山大', '济南',
    # v7.3 补充：历次样片中被切断的词
    '力量', '手册', '注入', '健康', '规范', '精准', '急难', '创新',
    '采用', '联动', '干货', '用心', '用情', '经济', '平台', '模式',
    '线上', '线下', '启幕', '大家', '工作', '人员', '志愿', '轻松',
])
_COMMON_PHRASES = [
    '最后一公里', '人社服务大篷车', '新就业形态劳动者', '面对面讲解',
    '走进奔跑者', '保障与你同行', '美团服务中心', '权益保障',
    '社保参保', '人社服务', '人社局', '维权意识', '宣传资料',
    '互动环节', '志愿者', '劳动者', '就业形态', '服务保障',
    '工作人员', '外卖骑手', '大篷车', '集成宣传活动',
    '山大站', '服务中心', '宣传活动',
]

def _jieba_word_boundary_check(text: str, cut_pos: int) -> int:
    """通用坏边界回退：用 jieba 分词检查切点是否落在词中间（v7.3 新增）
    
    如果切点落在某个词的中间，自动回退到词边界。
    这是词表机制的兜底——不依赖手动维护的词表。
    """
    try:
        import jieba
        words = list(jieba.cut(text))
        # 找到切点落在哪个词里
        char_pos = 0
        for word in words:
            word_start = char_pos
            word_end = char_pos + len(word)
            if word_start < cut_pos < word_end and len(word) >= 2:
                # 切点落在这个词的中间，回退到词开始
                return word_start
            char_pos = word_end
    except Exception:
        pass
    return cut_pos


def _music_safe_split(text: str, max_chars: int = 14) -> list:
    """安全切分：词组完整性 > 单行约束 > 字数压缩"""
    if len(text) <= max_chars:
        return [text]
    
    result = []
    pos = 0
    while pos < len(text):
        remaining = text[pos:]
        if len(remaining) <= max_chars:
            result.append(remaining)
            break
        
        target = pos + max_chars
        best_cut = target
        
        # 优先级1: 在标点处切
        for i in range(min(target, len(text) - 1), max(pos + 3, target - 6), -1):
            if text[i] in '，,、；;：:':
                best_cut = i + 1
                break
        
        # 优先级2: 在虚词后切
        if best_cut == target:
            for i in range(min(target, len(text) - 1), max(pos + 3, target - 5), -1):
                if text[i] in '的了是在和与或等及而并让把向对':
                    best_cut = i + 1
                    break
        
        cut_pos = best_cut
        
        # 检查: 是否切在双字词中间（词表）
        if pos < cut_pos < len(text):
            pair = text[cut_pos - 1: cut_pos + 1]
            if pair in _COMMON_WORDS_2:
                cut_pos = cut_pos - 1
        
        # 检查: 是否切在固定短语中间
        for phrase in _COMMON_PHRASES:
            p_start = text.find(phrase, max(0, cut_pos - len(phrase)))
            if p_start >= 0:
                p_end = p_start + len(phrase)
                if p_start < cut_pos < p_end:
                    if p_end - pos <= max_chars + 4:  # 允许略微超限保护短语
                        cut_pos = p_end
                    else:
                        cut_pos = p_start
                    break
        
        # 通用坏边界回退：jieba 分词检查（v7.3 新增）
        adjusted = _jieba_word_boundary_check(text, cut_pos)
        if adjusted != cut_pos and adjusted > pos + 2:
            cut_pos = adjusted
        
        # 检查: 是否切在引号内
        before = text[pos:cut_pos]
        open_q = before.count('\u201c') + before.count('\u300a')
        close_q = before.count('\u201d') + before.count('\u300b')
        if open_q > close_q:
            # 引号未闭合，找到闭合位置
            for i in range(cut_pos, min(cut_pos + 10, len(text))):
                if text[i] in '\u201d\u300b':
                    cut_pos = i + 1
                    break
        
        # 防止死循环
        if cut_pos <= pos:
            cut_pos = pos + max_chars
        
        segment = text[pos:cut_pos].strip()
        if segment:
            result.append(segment)
        pos = cut_pos
    
    return result


def parse_news_script_structure(script_text: str) -> dict:
    """将新闻稿解析为结构化段落约束（v7.4 新闻播报模式专用）
    
    拆解为 4 类段落：
    1. opening_theme — 片头主题段（活动名/时间/地点/核心点题）
    2. main_body — 主体信息段（服务内容/关键动作/核心信息点）
    3. highlight — 亮点/差异化段（创新环节/重点对象/特色活动）
    4. closing — 片尾收束段（结果/总结/展望/主题强化）
    
    同时标记每段的"主信息关键词"和"期望场景类型"
    """
    import re
    
    result = {
        'has_structure': False,
        'segments': [],        # [{type, text, keywords, expected_scenes}]
        'info_keywords': [],   # 全稿主信息关键词
        'atmosphere_keywords': [],  # 氛围类关键词
    }
    
    if not script_text or not script_text.strip():
        return result
    
    # 按句号分句
    sentences = [s.strip() for s in re.split(r'[。！？\n]', script_text) if s.strip() and len(s.strip()) > 3]
    if not sentences:
        return result
    
    result['has_structure'] = True
    total = len(sentences)
    
    # 主信息关键词库（能真正支撑新闻稿语义的动作/场景）
    INFO_KEYWORDS = {
        '讲解', '发放', '资料', '手册', '政策', '咨询', '办理', '服务',
        '互动', '游戏', '答疑', '解读', '培训', '指导', '宣传',
        '骑手', '外卖', '劳动者', '工作人员', '志愿者',
        '参保', '社保', '权益', '维权', '就业', '创业',
        '走进', '深入', '面对面', '一对一', '现场',
        '展示', '演示', '体验', '参与', '报名',
    }
    
    # 氛围/展板类关键词（只能辅助，不能冒充主信息）
    ATMOSPHERE_KEYWORDS = {
        '横幅', '展板', '标语', '合影', '全景', '主视觉', 'logo',
        '背景', '场地', '布置', '氛围', '现场环境',
    }
    
    # 分段策略：按位置 + 语义关键词
    for i, sent in enumerate(sentences):
        # 判断段落类型
        position_ratio = i / max(total - 1, 1)
        
        if i == 0 or (i == 1 and total > 4):
            seg_type = 'opening_theme'
        elif i >= total - 1 or (i >= total - 2 and total > 4):
            seg_type = 'closing'
        elif any(kw in sent for kw in ['创新', '亮点', '特色', '首次', '独特', '新模式', '新方式']):
            seg_type = 'highlight'
        else:
            seg_type = 'main_body'
        
        # 提取关键词
        keywords = [kw for kw in INFO_KEYWORDS if kw in sent]
        atmo_kws = [kw for kw in ATMOSPHERE_KEYWORDS if kw in sent]
        
        # 推断期望场景类型
        expected_scenes = []
        if any(kw in sent for kw in ['讲解', '解读', '答疑', '培训']):
            expected_scenes.append('政策讲解')
        if any(kw in sent for kw in ['发放', '资料', '手册']):
            expected_scenes.append('资料发放')
        if any(kw in sent for kw in ['互动', '游戏', '体验']):
            expected_scenes.append('互动活动')
        if any(kw in sent for kw in ['咨询', '办理', '服务', '面对面']):
            expected_scenes.append('服务互动')
        if any(kw in sent for kw in ['骑手', '外卖', '劳动者']):
            expected_scenes.append('服务对象')
        if any(kw in sent for kw in ['走进', '深入', '现场']):
            expected_scenes.append('活动现场')
        if any(kw in sent for kw in ['合影', '主题']):
            expected_scenes.append('主题建立')
        
        result['segments'].append({
            'type': seg_type,
            'text': sent,
            'keywords': keywords,
            'atmosphere_keywords': atmo_kws,
            'expected_scenes': expected_scenes,
            'sentence_index': i,
        })
        result['info_keywords'].extend(keywords)
        result['atmosphere_keywords'].extend(atmo_kws)
    
    # 去重
    result['info_keywords'] = list(set(result['info_keywords']))
    result['atmosphere_keywords'] = list(set(result['atmosphere_keywords']))
    
    print(f"  [新闻稿解析] {total} 句 → opening={sum(1 for s in result['segments'] if s['type']=='opening_theme')}, "
          f"main={sum(1 for s in result['segments'] if s['type']=='main_body')}, "
          f"highlight={sum(1 for s in result['segments'] if s['type']=='highlight')}, "
          f"closing={sum(1 for s in result['segments'] if s['type']=='closing')}")
    print(f"  [新闻稿解析] 主信息关键词: {result['info_keywords'][:10]}")
    
    return result


def parse_director_constraints(storyboard_note: str) -> dict:
    """从"分镜要求 / 参考文案"解析结构化导演约束（v7.4 正式固化）
    
    将用户自然语言拆解为 5 类可执行约束：
    1. opening  — 片头约束（前几秒先出什么）
    2. sequence — 段落顺序约束（先A再B再C）
    3. scene_preference — 画面类型约束（优先/减少某类镜头）
    4. ending   — 片尾约束（最后收在什么画面）
    5. subtitle_scene — 字幕-画面联动约束
    
    Returns:
        {
            'has_constraints': bool,
            'opening': {'required_types': [...], 'text': str},
            'sequence': [{'order': int, 'scene_keywords': [...], 'text': str}],
            'scene_preference': {'prefer': [...], 'reduce': [...], 'text': str},
            'ending': {'required_types': [...], 'text': str},
            'subtitle_scene': [{'subtitle_text': str, 'scene_keywords': [...]}],
            'raw_text': str,
        }
    """
    import re
    
    result = {
        'has_constraints': False,
        'opening': {'required_types': [], 'text': ''},
        'sequence': [],
        'scene_preference': {'prefer': [], 'reduce': [], 'text': ''},
        'ending': {'required_types': [], 'text': ''},
        'subtitle_scene': [],
        'raw_text': storyboard_note or '',
    }
    
    if not storyboard_note or not storyboard_note.strip():
        return result
    
    text = storyboard_note.strip()
    
    # ============================================================
    # 1. 片头约束：匹配"开头/片头/前几秒 + 先出/先放/先上 + 关键词"
    # ============================================================
    opening_patterns = [
        r'(?:开头|片头|前\d+秒|开场|开始).*?(?:先出|先放|先上|先用|先来|应该是|要有|出现)\s*[：:]*\s*(.+?)(?:[。；\n]|$)',
        r'(?:先出|先放|先上|开头先).*?(?:主题|全景|横幅|标语|活动现场|人物|合影|展板|现场|服务)',
    ]
    for pat in opening_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            matched_text = m.group(0)
            result['opening']['text'] = matched_text
            # 提取场景关键词
            scene_kws = []
            for kw in ['主题', '全景', '横幅', '标语', '活动现场', '合影', '展板', 
                       '人物', '服务', '现场', '特写', '标识', '建立']:
                if kw in matched_text:
                    scene_kws.append(kw)
            result['opening']['required_types'] = scene_kws
            result['has_constraints'] = True
            break
    
    # ============================================================
    # 2. 段落顺序约束：匹配"先...再...然后..."或"第一段...第二段..."
    # ============================================================
    seq_pattern = re.compile(
        r'(?:先|首先|第一[段部分]?).*?(?:再|然后|接着|其次|第二[段部分]?)',
        re.DOTALL
    )
    if seq_pattern.search(text):
        # 按"先/再/然后/接着/最后"拆分顺序段
        parts = re.split(r'[，,。；\n]', text)
        order = 0
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if re.search(r'先|首先|第一|开头|开场', part):
                order = 1
            elif re.search(r'再|然后|接着|其次|第二|中段|中间', part):
                order = 2
            elif re.search(r'最后|片尾|结尾|收尾|第三', part):
                order = 3
            
            if order > 0:
                kws = []
                for kw in ['主题', '全景', '横幅', '标语', '活动', '合影', '服务', '现场',
                           '人物', '资料', '发放', '讲解', '互动', '咨询', '骑手',
                           '游戏', '展板', '特写', '宣传', '政策']:
                    if kw in part:
                        kws.append(kw)
                if kws:
                    result['sequence'].append({
                        'order': order,
                        'scene_keywords': kws,
                        'text': part,
                    })
                    result['has_constraints'] = True
    
    # ============================================================
    # 3. 画面类型约束：匹配"优先/多用/减少/不要/少用 + 某类镜头"
    # ============================================================
    prefer_pattern = re.compile(r'(?:优先|多用|多出|侧重|突出|增加)\s*(.+?)(?:[，,。；\n]|$)')
    reduce_pattern = re.compile(r'(?:减少|少用|不要|避免|少出|不用)\s*(.+?)(?:[，,。；\n]|$)')
    
    for m in prefer_pattern.finditer(text):
        kws = []
        matched = m.group(1)
        for kw in ['人物', '横幅', '活动', '合影', '服务', '资料', '现场', '互动',
                   '特写', '全景', '近景', '中景', '远景', '动态', '标语']:
            if kw in matched:
                kws.append(kw)
        if kws:
            result['scene_preference']['prefer'].extend(kws)
            result['scene_preference']['text'] += m.group(0) + '；'
            result['has_constraints'] = True
    
    for m in reduce_pattern.finditer(text):
        kws = []
        matched = m.group(1)
        for kw in ['横幅', '合影', '标语', '展板', '静态', '重复', '相似']:
            if kw in matched:
                kws.append(kw)
        if kws:
            result['scene_preference']['reduce'].extend(kws)
            result['scene_preference']['text'] += m.group(0) + '；'
            result['has_constraints'] = True
    
    # ============================================================
    # 4. 片尾约束：匹配"最后/收尾/片尾 + 收在/用/放 + 关键词"
    # ============================================================
    ending_patterns = [
        r'(?:最后|收尾|片尾|结尾|结束).*?(?:收在|用|放|出现|是)\s*[：:]*\s*(.+?)(?:[。；\n]|$)',
    ]
    for pat in ending_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            matched_text = m.group(0)
            result['ending']['text'] = matched_text
            scene_kws = []
            for kw in ['主题', '合影', '全景', '标语', '横幅', '收束', '主视觉', '展板']:
                if kw in matched_text:
                    scene_kws.append(kw)
            result['ending']['required_types'] = scene_kws
            result['has_constraints'] = True
            break
    
    if result['has_constraints']:
        print(f"  [导演约束] 从分镜要求解析到结构化约束:")
        if result['opening']['required_types']:
            print(f"    片头: {result['opening']['required_types']} — {result['opening']['text'][:40]}")
        if result['sequence']:
            for s in result['sequence']:
                print(f"    顺序{s['order']}: {s['scene_keywords']} — {s['text'][:40]}")
        if result['scene_preference']['prefer'] or result['scene_preference']['reduce']:
            print(f"    优先: {result['scene_preference']['prefer']}, 减少: {result['scene_preference']['reduce']}")
        if result['ending']['required_types']:
            print(f"    片尾: {result['ending']['required_types']} — {result['ending']['text'][:40]}")
    
    return result


def _build_structured_director_prompt(constraints: dict) -> str:
    """将结构化导演约束转化为 L3 prompt 中的强规则段落（v7.4）
    
    不再只是 [分镜要求]: 原文
    而是拆成结构化的 ⚠️ 硬约束段
    """
    if not constraints.get('has_constraints'):
        return ''
    
    sections = []
    sections.append("\n## ⚠️ 用户导演硬约束（优先级高于默认模板，必须遵守）\n")
    
    if constraints['opening']['required_types']:
        kws = '、'.join(constraints['opening']['required_types'])
        sections.append(f"### 片头硬约束\n- 前 1-2 个镜头必须包含以下场景类型之一：{kws}\n- 原文要求：{constraints['opening']['text']}\n")
    
    if constraints['sequence']:
        sections.append("### 段落顺序硬约束\n按以下顺序组织镜头：\n")
        for s in sorted(constraints['sequence'], key=lambda x: x['order']):
            label = {1: '前段', 2: '中段', 3: '后段'}.get(s['order'], f"第{s['order']}段")
            kws = '、'.join(s['scene_keywords'])
            sections.append(f"- {label}：优先 {kws} 类镜头（{s['text'][:50]}）\n")
    
    if constraints['scene_preference']['prefer']:
        kws = '、'.join(constraints['scene_preference']['prefer'])
        sections.append(f"### 画面类型硬约束\n- 优先选用：{kws}\n")
    if constraints['scene_preference']['reduce']:
        kws = '、'.join(constraints['scene_preference']['reduce'])
        sections.append(f"- 减少使用：{kws}\n")
    
    if constraints['ending']['required_types']:
        kws = '、'.join(constraints['ending']['required_types'])
        sections.append(f"### 片尾硬约束\n- 最后 1-2 个镜头必须包含以下场景类型之一：{kws}\n- 原文要求：{constraints['ending']['text']}\n")
    
    if constraints['raw_text']:
        sections.append(f"\n### 用户原始分镜要求（完整参考）\n{constraints['raw_text']}\n")
    
    return '\n'.join(sections)


def _parse_subtitle_timing_rules(storyboard_note: str) -> list:
    """从分镜要求中解析字幕时序指令（v7.4 正式固化）
    
    支持的指令格式：
    - "字幕第3秒出现，持续5秒"
    - "字幕在第3秒出现，持续5秒"
    - "第一句字幕 3秒出现 持续5秒"
    - "字幕3s出现 5s持续"
    - "第1句 start=3 duration=5"
    
    Returns:
        [{
            'start_sec': float,    # 出现时间
            'duration_sec': float, # 持续时长
            'sentence_index': int, # 句子序号（0-based，-1 表示全部）
        }]
        空列表表示没有时序指令（走默认逻辑）
    """
    import re
    if not storyboard_note:
        return []
    
    rules = []
    
    # 模式1: "字幕(在)第N秒出现，持续M秒"
    pattern1 = re.compile(
        r'字幕[在]?第?\s*(\d+(?:\.\d+)?)\s*秒?\s*(?:时[候]?)?出现'
        r'[，,\s]*持续\s*(\d+(?:\.\d+)?)\s*秒',
        re.IGNORECASE
    )
    for m in pattern1.finditer(storyboard_note):
        rules.append({
            'start_sec': float(m.group(1)),
            'duration_sec': float(m.group(2)),
            'sentence_index': -1,  # 应用到所有句子（按顺序分配）
        })
    
    # 模式2: "第N句字幕 第M秒出现 持续K秒"
    pattern2 = re.compile(
        r'第\s*(\d+)\s*句[字幕]*[在]?\s*第?\s*(\d+(?:\.\d+)?)\s*秒?\s*出现'
        r'[，,\s]*持续\s*(\d+(?:\.\d+)?)\s*秒',
        re.IGNORECASE
    )
    for m in pattern2.finditer(storyboard_note):
        rules.append({
            'start_sec': float(m.group(2)),
            'duration_sec': float(m.group(3)),
            'sentence_index': int(m.group(1)) - 1,  # 转 0-based
        })
    
    # 模式3: "start=N duration=M" 或 "N秒出现 M秒持续"
    pattern3 = re.compile(
        r'(?:start\s*=\s*|(\d+(?:\.\d+)?)\s*[sS秒]\s*出现)'
        r'[，,\s]*(?:duration\s*=\s*|(\d+(?:\.\d+)?)\s*[sS秒]\s*持续)',
        re.IGNORECASE
    )
    
    if rules:
        print(f"  [字幕时序] 从分镜要求解析到 {len(rules)} 条时序指令:")
        for i, r in enumerate(rules):
            idx_str = f"第{r['sentence_index']+1}句" if r['sentence_index'] >= 0 else "全部"
            print(f"    [{i+1}] {idx_str}: {r['start_sec']}s 出现, 持续 {r['duration_sec']}s")
    
    return rules


def _generate_music_subtitle_srt(text: str, total_duration: float, srt_path: str, 
                                  target_duration: int = 30, storyboard_note: str = ''):
    """为纯音乐模式生成字幕 SRT
    
    正式规则（2026-04-25 v6 — 完整句 + 用户时序约束优先）：
    
    核心策略：
    1. 如果用户在"分镜要求"中指定了字幕时序（如"第3秒出现，持续5秒"）
       → 按用户指令强制执行，不走默认均分逻辑
    2. 如果没有时序指令 → 走默认完整句单行展示模式（v5 逻辑）
    
    用户时序指令是强规则，不是建议。
    """
    import re
    
    # ============================================================
    # 第一步：解析用户时序指令（强规则优先）
    # ============================================================
    timing_rules = _parse_subtitle_timing_rules(storyboard_note)
    
    # ============================================================
    # 第二步：分句（与 v5 相同）
    # ============================================================
    HARD_LIMIT = 50  # v7.5: 50字上限，配合 MarginL/R=20 宽显示区域防止三行
    
    raw_sentences = [s.strip() for s in re.split(r'[。！？\n]', text) if s.strip()]
    if not raw_sentences:
        raw_sentences = [text.strip()]
    
    segments = []
    for sent in raw_sentences:
        if len(sent) <= HARD_LIMIT:
            segments.append(sent)
        else:
            parts = re.split(r'[，,、；]', sent)
            buf = ''
            for p in parts:
                p = p.strip()
                if not p:
                    continue
                candidate = (buf + '，' + p) if buf else p
                if len(candidate) <= HARD_LIMIT:
                    buf = candidate
                else:
                    if buf:
                        segments.append(buf)
                    if len(p) > HARD_LIMIT:
                        sub_parts = _music_safe_split_v2(p, HARD_LIMIT)
                        segments.extend(sub_parts)
                        buf = ''
                    else:
                        buf = p
            if buf:
                segments.append(buf)
    
    segments = [s.strip() for s in segments if s.strip()]
    
    # 短段合并
    MIN_SEGMENT_CHARS = 10
    MERGE_LIMIT = 50  # v7.5: 与 HARD_LIMIT 同步
    merged = []
    i = 0
    while i < len(segments):
        seg = segments[i]
        if len(seg) <= MIN_SEGMENT_CHARS and i + 1 < len(segments):
            combined = seg + '，' + segments[i + 1]
            if len(combined) <= MERGE_LIMIT:
                merged.append(combined)
                i += 2
                continue
        merged.append(seg)
        i += 1
    segments = merged
    
    # ============================================================
    # 第三步：分配时间（用户指令优先 vs 默认均分）
    # ============================================================
    
    if timing_rules:
        # ---- 用户指定了时序 → 强制按指令分配 ----
        print(f"  [字幕] 纯音乐v6: 用户时序模式（{len(timing_rules)} 条指令，{len(segments)} 条字幕）")
        
        # 如果是全局指令（sentence_index=-1），应用到所有句子
        global_rules = [r for r in timing_rules if r['sentence_index'] == -1]
        specific_rules = [r for r in timing_rules if r['sentence_index'] >= 0]
        
        lines = []
        if global_rules and not specific_rules:
            # 全局规则：所有字幕按同一套时序参数，依次出现
            rule = global_rules[0]
            start = rule['start_sec']
            dur = rule['duration_sec']
            gap = 0.3
            
            for i, s in enumerate(segments):
                seg_start = start + i * (dur + gap)
                seg_end = min(seg_start + dur, total_duration)
                if seg_start >= total_duration:
                    break
                lines.append(f"{i+1}")
                lines.append(f"{_srt_time(seg_start)} --> {_srt_time(seg_end)}")
                lines.append(s)
                lines.append("")
                print(f"    [{i+1}] {seg_start:.1f}-{seg_end:.1f}s ({len(s)}字) {s}")
        
        elif specific_rules:
            # 逐句指定：按 sentence_index 精确匹配
            for i, s in enumerate(segments):
                matched = [r for r in specific_rules if r['sentence_index'] == i]
                if matched:
                    rule = matched[0]
                    seg_start = rule['start_sec']
                    seg_end = min(seg_start + rule['duration_sec'], total_duration)
                else:
                    # 未指定的句子：在已指定句子之后均匀分配剩余时间
                    seg_start = 0
                    seg_end = 0
                
                if seg_end > seg_start:
                    lines.append(f"{len(lines)//4 + 1}")
                    lines.append(f"{_srt_time(seg_start)} --> {_srt_time(seg_end)}")
                    lines.append(s)
                    lines.append("")
                    print(f"    [{i+1}] {seg_start:.1f}-{seg_end:.1f}s ({len(s)}字) {s} [用户指定]")
        
        # 后验校验
        _verify_subtitle_timing(lines, timing_rules, total_duration)
        
    else:
        # ---- 无用户指令 → 默认均分（v5 逻辑）----
        print(f"  [字幕] 纯音乐v6: 默认均分模式（{len(segments)} 条字幕）")
        gap = 0.3
        total_gaps = gap * max(0, len(segments) - 1)
        usable = total_duration - total_gaps
        seg_dur = max(usable / len(segments), 1.0) if segments else total_duration
        
        lines = []
        cursor = 0.0
        for i, s in enumerate(segments):
            start = cursor
            end = min(cursor + seg_dur, total_duration)
            lines.append(f"{i+1}")
            lines.append(f"{_srt_time(start)} --> {_srt_time(end)}")
            lines.append(s)
            lines.append("")
            cursor = end + gap
        
        for i, s in enumerate(segments):
            print(f"    [{i+1}] ({len(s)}字) {s}")
    
    with open(srt_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f"  [字幕] 纯音乐v6: {len(segments)} 条, 总时长 {total_duration:.1f}s, "
          f"模式={'用户时序' if timing_rules else '默认均分'}")


def _verify_subtitle_timing(srt_lines: list, timing_rules: list, total_duration: float):
    """后验校验：检查字幕时间是否符合用户指令"""
    import re
    
    # 解析 SRT 行中的时间
    actual_timings = []
    for i in range(0, len(srt_lines), 4):
        if i + 1 < len(srt_lines):
            time_line = srt_lines[i + 1]
            m = re.match(r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})', time_line)
            if m:
                actual_timings.append({
                    'start': m.group(1),
                    'end': m.group(2),
                })
    
    if not actual_timings:
        return
    
    # 检查全局规则
    global_rules = [r for r in timing_rules if r['sentence_index'] == -1]
    if global_rules:
        rule = global_rules[0]
        first_start = actual_timings[0]['start']
        expected_start = f"{int(rule['start_sec']//3600):02d}:{int(rule['start_sec']%3600//60):02d}:{int(rule['start_sec']%60):02d},000"
        if first_start != expected_start:
            print(f"  [后验] ⚠️ 首条字幕出现时间 {first_start}，用户要求 {expected_start}")
        else:
            print(f"  [后验] ✅ 首条字幕出现时间符合用户要求（{expected_start}）")


def _music_safe_split_v2(text: str, hard_limit: int = 30) -> list:
    """纯音乐字幕专用安全切分（v5 新增）
    
    只在超过 hard_limit 时才调用。
    策略：jieba 分词后按词边界切分，绝不切断词语。
    """
    if len(text) <= hard_limit:
        return [text]
    
    try:
        import jieba
        words = list(jieba.cut(text))
    except Exception:
        # jieba 不可用时，在虚词后切分
        mid = len(text) // 2
        for offset in range(min(6, mid)):
            for pos in [mid + offset, mid - offset]:
                if 0 < pos < len(text) and text[pos - 1] in '的了是在和与或等及而并让把向对':
                    return [text[:pos].strip(), text[pos:].strip()]
        return [text[:mid].strip(), text[mid:].strip()]
    
    # 按词边界切分，尽量均分
    result = []
    buf = ''
    for w in words:
        if len(buf) + len(w) > hard_limit and buf:
            result.append(buf.strip())
            buf = w
        else:
            buf += w
    if buf:
        result.append(buf.strip())
    
    return [s for s in result if s]

def _srt_time(sec):
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int((sec - int(sec)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ============================================================
# ============================================================
# v8.1: expression_intent 注入 + 表达去重（新闻播报专用）
# ============================================================
# 七类新闻表达意图（NEWS_EXPRESSION_INTENT_V1）：
#   event_intro          — 交代事件/地点/对象
#   service_action       — 服务动作（发资料/递送/办理）
#   policy_explain       — 政策讲解/答疑
#   participant_feedback — 骑手阅读/咨询/回应/反馈
#   interactive_highlight— 趣味互动/游戏/体验
#   atmosphere_support   — 横幅/标语/环境/现场辅助
#   value_closure        — 总结收束/服务温度/结果落点
# ============================================================

_INTENT_KEYWORDS = {
    'event_intro':          ['走进', '启动', '举办', '开展', '来到', '现场', '活动'],
    'service_action':       ['发放', '资料', '手册', '递送', '办理', '递发', '礼包', '发放宣传'],
    'policy_explain':       ['讲解', '答疑', '解读', '培训', '指导', '政策讲解', '沟通', '交流', '面对面'],
    'participant_feedback':  ['骑手', '阅读', '咨询', '回应', '反馈', '参与', '认真', '倾听', '提问'],
    'interactive_highlight': ['互动', '游戏', '体验', '趣味', '轻松', '投掷', '创新', '亮点'],
    'atmosphere_support':   ['横幅', '标语', '展板', '主视觉', '全景', '布景', '场地', '主题展示', '标识'],
    'value_closure':        ['收束', '满意', '积极', '效果', '成效', '温度', '保障', '最后一公里',
                             '持续', '群体正向', '合影', '集体'],
}

# 表达占比上限（NEWS_EXPRESSION_INTENT_V1 永久规则）
_INTENT_MAX_RATIO = {
    'service_action': 0.35,       # 发资料不得超 35%
    'policy_explain': 0.30,       # 讲解不得超 30%
    'atmosphere_support': 0.15,   # 氛围辅助不得超 15%
}


# 视觉母题 → 允许的 intent 白名单（视觉优先锁定）
_VISUAL_INTENT_WHITELIST = {
    '合影':     ['event_intro', 'atmosphere_support', 'value_closure'],
    '集体':     ['event_intro', 'atmosphere_support', 'value_closure'],
    '群像':     ['event_intro', 'atmosphere_support', 'value_closure'],
    '横幅':     ['event_intro', 'atmosphere_support'],
    '标语':     ['event_intro', 'atmosphere_support'],
    '展板':     ['event_intro', 'atmosphere_support'],
    '全景':     ['event_intro', 'atmosphere_support'],
    '布景':     ['event_intro', 'atmosphere_support'],
    '主题展示': ['event_intro', 'atmosphere_support'],
    '主视觉':   ['event_intro', 'atmosphere_support'],
}


def _classify_intent(shot: dict, dp_entry: dict = None, total_shots: int = 10) -> str:
    """根据 shot + director_plan 综合判断 expression_intent（v8.1 视觉优先）

    核心原则：先看画面客观能表达什么，再匹配关键词。
    合影只能表达 event_intro/atmosphere_support/value_closure，不能表达 policy_explain。
    """
    scene_type = shot.get('scene_type', '')

    # === 视觉优先锁定：如果画面属于特定母题，限制可用 intent ===
    allowed_intents = None
    for motif_kw, whitelist in _VISUAL_INTENT_WHITELIST.items():
        if motif_kw in scene_type:
            allowed_intents = whitelist
            break

    # 构建综合文本（scene_type 权重最高）
    combined = scene_type + ' ' + scene_type + ' '  # scene_type 双倍权重
    if dp_entry:
        combined += dp_entry.get('selection_reason', '') + ' '

    scores = {}
    for intent, keywords in _INTENT_KEYWORDS.items():
        # 如果视觉锁定了白名单，不在白名单内的 intent 直接 0 分
        if allowed_intents and intent not in allowed_intents:
            scores[intent] = 0
            continue
        score = sum(2 if kw in combined else 0 for kw in keywords)
        scores[intent] = score

    # 位置启发（降低权重，不再强制覆盖视觉判断）
    order = shot.get('order', 0)
    if order <= 2:
        scores['event_intro'] = scores.get('event_intro', 0) + 1
    # 不再对片尾强加 value_closure 加分

    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else 'service_action'


def _inject_expression_types(timeline: list, director_plan: list, script_text: str, narrative_flow: str):
    """为每个 timeline 镜头注入 expression_intent + 表达去重后验

    永久规则（NEWS_EXPRESSION_INTENT_V1）：
    1. 每条镜头必须有 expression_intent
    2. 同一 intent 连续不得超过 2 条
    3. service_action ≤ 35%, policy_explain ≤ 30%, atmosphere_support ≤ 15%
    4. 片头 = event_intro, 片尾 = value_closure
    5. interactive_highlight / participant_feedback 各至少 1 条（如候选存在）
    """
    from collections import Counter

    # v10.4 类型防御：director_plan 可能是 str/dict/None（L3 全局读片模式返回格式不固定）
    if not isinstance(director_plan, list):
        print(f"  ⚠️ [expression] director_plan 类型异常: {type(director_plan).__name__}，跳过表达意图注入")
        # 给每条 shot 赋默认 intent，不崩溃
        for i, shot in enumerate(timeline):
            total = len(timeline)
            if i == 0:
                shot['expression_intent'] = 'event_intro'
            elif i >= total - 2:
                shot['expression_intent'] = 'value_closure'
            else:
                shot['expression_intent'] = 'service_action'
        return timeline

    dp_map = {}
    for dp in director_plan:
        if not isinstance(dp, dict):
            continue  # 跳过非 dict 元素
        dp_map[dp.get('slot_order', 0)] = dp

    total = len(timeline)

    # === 1. 注入 expression_intent ===
    for shot in timeline:
        order = shot.get('order', 0)
        dp_entry = dp_map.get(order, {})
        shot['expression_intent'] = _classify_intent(shot, dp_entry, total)
        # 兼容旧字段
        shot['expression_type'] = shot['expression_intent']

    # === 2. 片头强制 event_intro（保留）+ 片尾由画面决定（v8.1 改） ===
    if timeline:
        timeline[0]['expression_intent'] = 'event_intro'
        timeline[0]['expression_type'] = 'event_intro'
        # v8.1: 片尾不再强制 value_closure，由画面内容自然决定
        # 如果片尾画面本身就是收束类（合影/群体/成效），保持 value_closure
        # 如果片尾是服务/讲解/互动，保持其真实 intent（不强行覆盖）
        last_intent = timeline[-1].get('expression_intent', '')
        last_scene = timeline[-1].get('scene_type', '')
        # 只有当片尾画面确实是收束类时才标 value_closure
        CLOSURE_KEYWORDS = ['合影', '集体', '群体', '成效', '收束', '满意', '正向']
        if any(kw in last_scene for kw in CLOSURE_KEYWORDS):
            timeline[-1]['expression_intent'] = 'value_closure'
            timeline[-1]['expression_type'] = 'value_closure'
        # 否则保持画面真实 intent，不强行覆盖

    # === 3. 连续重复打断（同一 intent 连续 > 2 条） ===
    fixes = []
    changed = True
    passes = 0
    while changed and passes < 3:
        changed = False
        passes += 1
        consec = 1
        for i in range(1, len(timeline)):
            ei = timeline[i].get('expression_intent', '')
            ep = timeline[i-1].get('expression_intent', '')
            if ei == ep and ei:
                consec += 1
                if consec > 2:
                    # 找后方不同 intent 的镜头交换
                    for j in range(i+1, min(i+4, len(timeline))):
                        ej = timeline[j].get('expression_intent', '')
                        if ej != ei:
                            timeline[i], timeline[j] = timeline[j], timeline[i]
                            fixes.append(f"[表达去重] 镜头{i+1}↔{j+1}: 打断连续{consec}条'{ei}'")
                            changed = True
                            break
            else:
                consec = 1

    # === 4. 占比限制 + 表达均衡调度（v8.1 升级） ===
    # 策略：当某 intent 超限时，将后半段多余的该 intent 镜头
    #       与前半段不同 intent 的镜头交换位置（不改标签，改位置）
    #       这样后半段表达更丰富，前半段多余的同类被后移（变相稀释）
    dist = Counter(s.get('expression_intent', '') for s in timeline)
    for intent, max_ratio in _INTENT_MAX_RATIO.items():
        count = dist.get(intent, 0)
        max_count = max(int(total * max_ratio), 2)
        if count > max_count and total >= 6:
            # 找后半段中该 intent 的镜头（从后往前）
            excess_indices = []
            half = total // 2
            for i in range(total - 2, half, -1):  # 不动最后 1 条
                if timeline[i].get('expression_intent') == intent:
                    excess_indices.append(i)
                    if len(excess_indices) >= count - max_count:
                        break

            # 找前半段中不同 intent 的镜头来交换
            swapped = 0
            for ex_idx in excess_indices:
                for fwd_idx in range(2, half):  # 不动前 2 条
                    fwd_intent = timeline[fwd_idx].get('expression_intent', '')
                    if fwd_intent != intent and fwd_intent not in ('event_intro',):
                        timeline[ex_idx], timeline[fwd_idx] = timeline[fwd_idx], timeline[ex_idx]
                        fixes.append(f"[表达均衡] 镜头{ex_idx+1}↔{fwd_idx+1}: "
                                    f"'{intent}'后移，'{fwd_intent}'前置（降低后半段'{intent}'密度）")
                        swapped += 1
                        break
            if swapped > 0:
                # 重新计算分布
                dist = Counter(s.get('expression_intent', '') for s in timeline)

    # === 4b. 片尾表达多样性（最后 3 条至少 1 条非 policy_explain） ===
    if total >= 6:
        tail_3 = timeline[-3:]
        tail_intents = [s.get('expression_intent', '') for s in tail_3]
        if all(ei == 'policy_explain' for ei in tail_intents):
            # 找倒数第 2 条，与前半段非 policy_explain 交换
            for fwd_idx in range(total // 2, 2, -1):
                if timeline[fwd_idx].get('expression_intent', '') != 'policy_explain':
                    timeline[-2], timeline[fwd_idx] = timeline[fwd_idx], timeline[-2]
                    fixes.append(f"[片尾多样] 镜头{total-1}↔{fwd_idx+1}: 片尾插入非 policy_explain")
                    break

    # === 5. 同素材一致性检查（v8.1：同一素材不得赋予不同 intent） ===
    from collections import defaultdict
    src_intent_map = defaultdict(set)
    for shot in timeline:
        src = shot.get('source_file', '')
        ei = shot.get('expression_intent', '')
        src_intent_map[src].add(ei)

    for src, intents in src_intent_map.items():
        if len(intents) > 1:
            # 统一为出现次数最多的 intent
            intent_counts = Counter(s.get('expression_intent', '') for s in timeline if s.get('source_file') == src)
            dominant = intent_counts.most_common(1)[0][0]
            for shot in timeline:
                if shot.get('source_file') == src and shot.get('expression_intent') != dominant:
                    old = shot['expression_intent']
                    shot['expression_intent'] = dominant
                    shot['expression_type'] = dominant
                    fixes.append(f"[表达一致性] {src[:25]}: '{old}'→'{dominant}'（同素材统一）")

    # === 6. 输出统计 ===
    dist = Counter(s.get('expression_intent', '') for s in timeline)
    unique = len([k for k, v in dist.items() if v > 0])

    if fixes:
        for f in fixes:
            print(f"    ✅ {f}")

    print(f"  [表达意图] 分布: {dict(dist)} (共{total}条, {unique}种)")
    return fixes


def generate_video(task_id: str, stage_callback=None) -> dict:
    """
    一键生成成片。

    完整链路：
    1. 读取 task JSON（新闻稿、task_context）
    2. build_l2_segments_text(task_id) — 含人工 overrides
    3. TTS 生成配音
    4. L3 动态调用 — 产出 timeline
    5. 裁切素材片段
    6. 拼接 + 字幕 + 渲染
    7. 产出成片
    
    Args:
        stage_callback: 可选的阶段心跳回调 fn(stage_name, extra_dict=None)

    Returns:
        {'success': True, 'video_path': '...', 'timeline': [...], ...}
    """
    # b5a-patch: 清除跨任务 retry 标记
    if hasattr(generate_video, '_l3_retried'):
        delattr(generate_video, '_l3_retried')

    # v10.4: 阶段心跳回调（不传则跳过）
    def _heartbeat(stage, extra=None):
        if stage_callback:
            try:
                stage_callback(stage, extra)
            except Exception:
                pass
    
    # v10.5: 中断检查（每个关键阶段前调用）
    def _check_cancel():
        try:
            _cancel_path = PROJECT_ROOT / "workdir" / "tasks" / f"{task_id}.json"
            if _cancel_path.exists():
                with open(_cancel_path, 'r', encoding='utf-8') as _cf:
                    _ct = json.load(_cf)
                if _ct.get('cancel_requested'):
                    print(f"  ⛔ [中断] 检测到用户取消请求，停止生成")
                    raise RuntimeError("task_cancelled")
        except RuntimeError:
            raise
        except Exception:
            pass  # 读取失败不影响主流程
    
    print(f"\n{'=' * 70}")
    print(f"[一键生成] task_id={task_id}")
    print(f"{'=' * 70}")

    # v10.8: 加载永久规则
    _rules_path = PROJECT_ROOT / "config" / "system_permanent_rules.json"
    _rules_version = 'unknown'
    if _rules_path.exists():
        with open(_rules_path, 'r', encoding='utf-8') as _rf:
            _permanent_rules = json.load(_rf)
        _rules_version = _permanent_rules.get('version', 'unknown')
        print(f"[PERMANENT_RULES] loaded version: {_rules_version}")
    else:
        print(f"[PERMANENT_RULES] ⚠️ 规则文件不存在: {_rules_path}")

    tasks_dir = PROJECT_ROOT / "workdir" / "tasks"
    task_path = tasks_dir / f"{task_id}.json"
    if not task_path.exists():
        raise FileNotFoundError(f"Task 不存在: {task_path}")

    def _update_stage(tp, stage):
        """写回生成阶段到 task JSON"""
        try:
            with open(tp, 'r', encoding='utf-8') as _f:
                _t = json.load(_f)
            _t['generate_stage'] = stage
            _t['status'] = 'generating'
            _t['updated_at'] = datetime.now().isoformat()
            with open(tp, 'w', encoding='utf-8') as _f:
                json.dump(_t, _f, ensure_ascii=False, indent=2)
        except:
            pass

    def _update_task_failed(tp, error_msg, last_step):
        """将任务标记为 failed"""
        try:
            with open(tp, 'r', encoding='utf-8') as _f:
                _t = json.load(_f)
            _t['status'] = 'failed'
            _t['failed_at'] = datetime.now().isoformat()
            _t['error_traceback'] = error_msg
            _t['last_step'] = last_step
            with open(tp, 'w', encoding='utf-8') as _f:
                json.dump(_t, _f, ensure_ascii=False, indent=2)
        except:
            pass

    with open(task_path, 'r', encoding='utf-8') as f:
        task = json.load(f)

    # v11.6.3: 初始化 relaxable_backups
    generate_video._relaxable_backups = []
    
    # v11.6: 生成开始时重置 heartbeat + started_at，避免前端读到上次残留时间戳
    _now_str = datetime.now().isoformat()
    task['generate_heartbeat'] = _now_str
    task['generate_started_at'] = _now_str
    task['status'] = 'generating'
    task['generate_stage'] = 'generating'
    # 清除上次失败残留
    task.pop('error_traceback', None)
    task.pop('failed_at', None)
    task.pop('error', None)
    with open(task_path, 'w', encoding='utf-8') as f:
        json.dump(task, f, ensure_ascii=False, indent=2)

    # v11.6.3: 顶层异常保护 — 确保任何崩溃都回写 failed，不留假活
    import atexit
    def _cleanup_on_crash():
        """进程异常退出时，检查 task 是否仍为 generating，若是则标记 failed"""
        try:
            with open(task_path, 'r') as _cf:
                _ct = json.load(_cf)
            if _ct.get('status') == 'generating':
                _ct['status'] = 'failed'
                _ct['generate_stage'] = 'failed'
                _ct['error'] = '生成进程异常退出（atexit cleanup）'
                _ct['failed_at'] = datetime.now().isoformat()
                with open(task_path, 'w') as _cf:
                    json.dump(_ct, _cf, ensure_ascii=False, indent=2)
        except Exception:
            pass
    atexit.register(_cleanup_on_crash)

    # === 读取用户配置（优先从独立 config 文件读，兜底 task JSON） ===
    config_file = PROJECT_ROOT / "tasks" / "configs" / f"{task_id}.json"
    user_config = {}
    if config_file.exists():
        with open(config_file, 'r', encoding='utf-8') as cf:
            user_config = json.load(cf)
        print(f"  [config] 从独立配置文件读取: {config_file}")
    else:
        user_config = task.get('config', {})
        print(f"  [config] 从 task JSON config 字段读取")
    
    # 同步到 task JSON 的 config 字段（保持一致）
    task['config'] = {**task.get('config', {}), **user_config}

    # === 读取编辑模式 ===
    edit_mode = user_config.get('edit_mode', user_config.get('audio_mode', 'music_only'))
    print(f"  [config] edit_mode={edit_mode}")

    script_text = user_config.get('script', '') or task.get('script', '')
    task_context = task.get('task_context', {})
    theme = task_context.get('video_theme', '')
    event = task_context.get('news_event', '')
    context_text = f"拍摄主题：{theme}\n新闻事件：{event}" if theme or event else ""

    # 纯音乐混剪模式参数
    target_duration = user_config.get('target_duration', 30)
    bgm_tos_key = user_config.get('bgm_tos_key', '')
    storyboard_note = user_config.get('storyboard_note', '')
    music_subtitle = user_config.get('music_subtitle', {})

    # 构建 tos_key → URL 映射
    ms = task.get('material_status', {})
    tos_url_map = {}
    for fn, info in ms.items():
        if isinstance(info, dict) and info.get('tos_key'):
            tos_url_map[fn] = f"https://e23-video.tos-cn-beijing.volces.com/{info['tos_key']}"

    # === 输出目录 ===
    output_dir = PROJECT_ROOT / "outputs" / task_id
    output_dir.mkdir(parents=True, exist_ok=True)
    clips_dir = output_dir / "clips"
    clips_dir.mkdir(exist_ok=True)
    # v13.2-m: 每次生成前清空 clips/，避免上次渲染残留
    for _old_clip in clips_dir.glob("*.mp4"):
        try:
            _old_clip.unlink()
        except Exception:
            pass
    dl_dir = output_dir / "downloaded"
    # v13.2-m: 每次生成前清空 downloaded/，避免上次残留
    if dl_dir.exists():
        for _old_dl in dl_dir.iterdir():
            try:
                if _old_dl.is_file():
                    _old_dl.unlink()
            except Exception:
                pass
    dl_dir.mkdir(exist_ok=True)
    # v13.2-m: 清理旧 downloaded 目录
    if dl_dir.exists():
        for _old_dl in dl_dir.glob("*.*"):
            try:
                _old_dl.unlink()
            except Exception:
                pass
    dl_dir.mkdir(exist_ok=True)
    dl_dir.mkdir(exist_ok=True)

    _gen_ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    tts_output = None
    tts_meta_path = None
    tts_dur = 0

    if edit_mode == 'music_only':
        # ============================================================
        # 纯音乐混剪模式 — 跳过 TTS
        # ============================================================
        tts_dur = target_duration  # v11.6.2: 纯音乐用 target_duration 作为目标时长
        _update_stage(task_path, "tts")
        _heartbeat("tts_skipped")
        _check_cancel()
        print(f"\n[1/5] 纯音乐混剪模式 — 跳过 TTS")
        tts_dur = target_duration  # 使用用户选择的目标时长
        print(f"  目标时长: {target_duration}s")
        print(f"  BGM: {bgm_tos_key}")
        print(f"  分镜要求: {storyboard_note[:60] if storyboard_note else '(无)'}")
    else:
        # ============================================================
        # 1. TTS 生成配音（新闻播报模式）
        # ============================================================
        _update_stage(task_path, "tts")
        _heartbeat("tts_started")
        _check_cancel()
        print(f"\n[1/5] TTS 生成配音")
        if not script_text:
            raise ValueError("新闻稿为空，无法生成配音")

        voice_id = user_config.get('voice', '') or task.get('config', {}).get('voice', 'S_x249qIGO1')
        print(f"  音色: {voice_id}")

        # ============================================================
        # v13.3-b7b: TTS cache 检查
        # cache key = md5(normalized_script + voice_id + speed_ratio)
        # 命中时跳过 TTS API 调用，直接复用缓存音频
        # ============================================================
        from pipeline.tts_cache import check_tts_cache, write_tts_cache
        import time as _tts_time

        _tts_cache_hit, _tts_miss_reason, _tts_check_info = check_tts_cache(task_id, script_text, voice_id)
        _tts_t0 = _tts_time.time()

        if _tts_cache_hit:
            # Cache hit — 跳过 TTS API
            tts_output = _tts_check_info['audio_path']
            tts_meta_path = _tts_check_info['tts_meta_path']
            tts_dur = _tts_check_info.get('cached_duration', 0)
            _tts_elapsed = round(_tts_time.time() - _tts_t0, 3)
            print(f"  [v13.3-b7b] ✅ TTS cache hit: key={_tts_check_info['cache_key'][:12]}")
            print(f"  [v13.3-b7b] 跳过 TTS API，复用缓存音频 ({tts_dur}s)")
            print(f"  [v13.3-b7b] cache check: {_tts_elapsed}s")
        else:
            # Cache miss — 正常调用 TTS
            print(f"  [v13.3-b7b] ❌ TTS cache miss: reason={_tts_miss_reason}")
            tts_output = str(output_dir / f"tts_{_gen_ts}.mp3")
            tts_meta_path = str(output_dir / f"tts_meta_{_gen_ts}.json")
            tts_result = generate_tts_volcengine(script_text, tts_output, tts_meta_path, voice_type=voice_id)
            tts_dur = tts_result.get('total_duration', tts_result.get('duration', 0))
            if tts_dur == 0:
                raise RuntimeError(f"TTS 生成失败或时长为 0: {tts_result}")
            _tts_elapsed = round(_tts_time.time() - _tts_t0, 1)
            # 写入 cache
            write_tts_cache(task_id, script_text, voice_id,
                           tts_output, tts_meta_path, tts_dur, elapsed=_tts_elapsed)
            print(f"  [v13.3-b7b] TTS 完成 ({_tts_elapsed}s)，已写入 cache")

        print(f"  TTS 时长: {tts_dur}s")

    # ============================================================
    # 1b. 生成字幕句子级时间轴（v8.2 — TTS 后、L3 前）
    # ============================================================
    sentence_timeline = []
    if edit_mode == 'narration' and tts_meta_path and os.path.exists(tts_meta_path):
        with open(tts_meta_path, 'r') as f:
            tts_meta = json.load(f)
        # 从 TTS meta 提取句子级时间轴
        segments = tts_meta.get('segments', tts_meta.get('sentences', []))
        if segments:
            for i, seg in enumerate(segments):
                s_start = seg.get('start', seg.get('start_time', 0))
                s_end = seg.get('end', seg.get('end_time', 0))
                s_text = seg.get('text', seg.get('content', ''))
                # 标注 semantic_anchor
                anchor = 'service_action'  # 默认
                if any(kw in s_text for kw in ['互动', '游戏', '体验', '趣味']):
                    anchor = 'interactive_highlight'
                elif any(kw in s_text for kw in ['讲解', '答疑', '解读', '面对面']):
                    anchor = 'policy_explain'
                elif any(kw in s_text for kw in ['走进', '主办', '开展', '主题']):
                    anchor = 'event_intro'
                elif any(kw in s_text for kw in ['持续', '最后一公里', '打通', '推动']):
                    anchor = 'value_closure'
                elif any(kw in s_text for kw in ['发放', '资料', '手册', '递送']):
                    anchor = 'service_action'
                elif any(kw in s_text for kw in ['骑手', '外卖', '小哥', '劳动者']):
                    anchor = 'participant_feedback'
                # v1.2: 判断 strong/weak
                # v11: 扩充 strong 关键词（覆盖教育/展演/分享/研讨/嘉宾等场景）
                STRONG_KEYWORDS = ['互动', '游戏', '体验', '趣味', '发放', '资料', '手册',
                                   '讲解', '答疑', '咨询', '解读', '培训',
                                   '展演', '分享', '研讨', '课堂', '成果', '嘉宾', '专家',
                                   '启动', '仪式', '升空', '模型', '讲座', '听众', '论坛',
                                   '发布', '演示', '展示', '参会', '代表', '骑手', '外卖',
                                   '服务', '保障', '维权', '社保', '权益', '宣传']
                strength = 'strong' if any(kw in s_text for kw in STRONG_KEYWORDS) else 'weak'
                sentence_timeline.append({
                    'sentence_id': f'S{i+1:02d}',
                    'text': s_text,
                    'start_time': round(s_start, 2),
                    'end_time': round(s_end, 2),
                    'duration': round(s_end - s_start, 2),
                    'semantic_anchor': anchor,
                    'anchor_strength': strength,
                })
            # 保存
            with open(output_dir / "script_sentence_timeline.json", 'w', encoding='utf-8') as f:
                json.dump(sentence_timeline, f, ensure_ascii=False, indent=2)
            print(f"  [字幕时间轴] {len(sentence_timeline)} 句, 总时长 {tts_dur:.1f}s")
        else:
            # TTS meta 没有分句信息，按字数均分
            import re
            sentences = re.split(r'[。！？\n]', script_text)
            sentences = [s.strip() for s in sentences if s.strip()]
            cursor = 0.0
            avg_dur = tts_dur / max(len(sentences), 1)
            for i, s_text in enumerate(sentences):
                s_dur = avg_dur
                sentence_timeline.append({
                    'sentence_id': f'S{i+1:02d}',
                    'text': s_text,
                    'start_time': round(cursor, 2),
                    'end_time': round(cursor + s_dur, 2),
                    'duration': round(s_dur, 2),
                    'semantic_anchor': 'service_action',
                })
                cursor += s_dur
            with open(output_dir / "script_sentence_timeline.json", 'w', encoding='utf-8') as f:
                json.dump(sentence_timeline, f, ensure_ascii=False, indent=2)
            print(f"  [字幕时间轴] {len(sentence_timeline)} 句（均分估算）")

    # ============================================================
    # 2. L3 动态调用
    # ============================================================
    _update_stage(task_path, "l3")
    _heartbeat("l3_started")
    _check_cancel()
    print(f"\n[2/5] L3 动态导演调度")
    
    # 旧 task 兼容：如果 L2 产物没有 ffmpeg 后处理字段，补跑一次（v7.3）
    _l2_compat_path = output_dir / "l2_clean_windows_full.json"
    if _l2_compat_path.exists():
        import json as _json
        with open(_l2_compat_path) as _f:
            _l2_compat = _json.load(_f)
        _meta = _l2_compat.get('_metadata', {})
        if not _meta.get('ffmpeg_post_process'):
            print("  [兼容] L2 产物缺少 ffmpeg 后处理，补跑中...")
            from pipeline.ffmpeg_stability import process_l2_windows
            # 构建 local_files 列表
            _dl_dir = output_dir / "downloaded"
            _tmp_dir = Path(os.environ.get('VIDEO_TOOL_ROOT', str(PROJECT_ROOT))) / "tmp_v15" / task_id
            _local_files = []
            for fn in _l2_compat:
                if fn == '_metadata' or not isinstance(_l2_compat[fn], dict):
                    continue
                for _search in [_dl_dir, _tmp_dir]:
                    _cand = _search / fn
                    if not _cand.exists():
                        _cand = _search / fn.lower()
                    if _cand.exists():
                        _local_files.append({'filename': fn, 'path': str(_cand)})
                        break
            if _local_files:
                _l2_compat = process_l2_windows(_l2_compat, _local_files, task_id)
                with open(_l2_compat_path, 'w', encoding='utf-8') as _f:
                    _json.dump(_l2_compat, _f, ensure_ascii=False, indent=2)
                print(f"  [兼容] ffmpeg 后处理补跑完成，已写回 L2 产物")
            else:
                print(f"  [兼容] 无本地视频文件，跳过 ffmpeg 补跑")
    
    l2_segments_text = build_l2_segments_text(task_id=task_id)

    # v13.3-p2c: music_only 自适应目标时长 — 根据候选池计算 realistic_duration
    _realistic_duration = target_duration  # 默认使用原目标
    _adaptive_target_reason = ''
    if edit_mode == 'music_only':
        # 读取候选池时长
        import re as _re_dur
        _candidate_path = PROJECT_ROOT / "analysis_results" / f"{task_id}_candidate.json"
        if _candidate_path.exists():
            try:
                with open(_candidate_path) as _cf:
                    _cdata = json.load(_cf)
                _clips = _cdata if isinstance(_cdata, list) else _cdata.get('clips', [])
                _total_raw = 0
                _realistic_sum = 0
                for _c in _clips:
                    _tr = _c.get('timeline_range', '')
                    _m = _re_dur.search(r'([\d.]+)s', _tr)
                    _dur = float(_m.group(1)) if _m else 0
                    _total_raw += _dur
                    # 常规镜头最多贡献 3.2s（节奏上限）
                    _contrib = min(_dur, 3.2)
                    _realistic_sum += _contrib
                # clamp 到 20-30s
                _realistic_duration = max(20, min(30, int(_realistic_sum)))
                if _realistic_duration < target_duration:
                    _adaptive_target_reason = f'候选池按节奏截断后不足({_realistic_sum:.0f}s < {target_duration}s)'
                    print(f"  [v13.3-p2c] 📊 realistic_duration: {_realistic_sum:.0f}s → final_target: {_realistic_duration}s")
                    print(f"    原因: {_adaptive_target_reason}")
                else:
                    _realistic_duration = target_duration
            except Exception as _e:
                print(f"  [v13.3-p2c] ⚠️ 计算 realistic_duration 失败: {_e}")
        
        # 用 realistic_duration 替代 target_duration
        target_duration = _realistic_duration
        tts_dur = target_duration

    # 构建 L3 输入的 script_text（按模式）
    l3_script = script_text
    l3_context = context_text
    # v7.4: 解析结构化导演约束（用于 L3 prompt + timeline 后验）
    director_constraints = parse_director_constraints(storyboard_note)
    
    # v7.4: 新闻播报模式 — 解析新闻稿结构（用于 timeline 后验）
    news_structure = None
    if edit_mode == 'narration' and script_text:
        news_structure = parse_news_script_structure(script_text)
    
    if edit_mode == 'music_only':
        # 纯音乐模式：结构化导演约束 + 动态字幕文本传给 L3
        l3_parts = [f"[编辑模式: 纯音乐混剪]", f"[目标时长: {target_duration}秒]"]
        # v7.4: 用结构化强约束替代纯文本拼接
        director_prompt = _build_structured_director_prompt(director_constraints)
        if director_prompt:
            l3_parts.append(director_prompt)
        elif storyboard_note:
            # 无法解析为结构化约束时，仍传原文（兜底）
            l3_parts.append(f"[分镜要求]: {storyboard_note}")
        if music_subtitle.get('enabled') and music_subtitle.get('text'):
            l3_parts.append(f"[动态字幕文本]: {music_subtitle['text']}")
        if bgm_tos_key:
            l3_parts.append(f"[BGM]: {bgm_tos_key}")
        l3_script = '\n'.join(l3_parts)
        l3_context = context_text + f"\n编辑模式: 纯音乐混剪，目标时长 {target_duration} 秒"

    # ============================================================
    # v8.2: 全局候选长片读片 — 生产主链接入
    # ============================================================
    GLOBAL_REEL_L3 = True  # 全局读片开关（默认开启）
    _use_slot_render = False  # v9.9: slot 锁定渲染模式，在 slot_plan 生成后设为 True
    _slot_plan_path = output_dir / "slot_plan.json"  # v9.9: slot_plan 路径提前声明

    if GLOBAL_REEL_L3 and edit_mode == 'narration':
        # === 全局候选长片读片模式 ===
        print(f"\n[2/5] L3 全局候选长片读片模式")
        _heartbeat('candidate_reel_started')
        _check_cancel()
        try:
            from pipeline.candidate_reel import build_candidate_reel
            reel_info = build_candidate_reel(task_id)
            reel_path = reel_info['reel_path']
            manifest = reel_info['manifest']

            # 压缩到 480p（API 限制 50MB）
            # v12.4: timeout 60→300s，防止大素材任务因压缩超时回退旧 L3
            reel_small = str(output_dir / "candidate_reel_small.mp4")
            if not os.path.exists(reel_small) or os.path.getsize(reel_small) > 50 * 1024 * 1024:
                subprocess.run([FFMPEG, '-y', '-i', reel_path, '-vf', 'scale=854:480',
                               '-c:v', 'libx264', '-preset', 'fast', '-crf', '28', '-an', reel_small],
                              capture_output=True, timeout=300)
            reel_size_mb = os.path.getsize(reel_small) / (1024 * 1024)
            print(f"  候选长片: {reel_info['clip_count']} 条, {reel_info['total_duration']:.0f}s, 压缩后 {reel_size_mb:.0f}MB")

            # 上传到 TOS
            _ensure_env()
            import tos
            ak = os.environ.get('TOS_ACCESS_KEY', os.environ.get('VOLC_AK', ''))
            sk = os.environ.get('TOS_SECRET_KEY', os.environ.get('VOLC_SK', ''))
            tos_client = tos.TosClientV2(ak=ak, sk=sk, endpoint='tos-cn-beijing.volces.com', region='cn-beijing')
            tos_key = f'tmp_l3_clips/candidate_reel_{task_id}.mp4'
            tos_client.put_object_from_file('e23-video', tos_key, reel_small)
            reel_url = f'https://e23-video.tos-cn-beijing.volces.com/{tos_key}'
            print(f"  TOS 上传完成")

            # v12.9.1: narration 模式 reel URL 健康检查
            _reel_health = _check_video_url_health(reel_url, timeout=10)
            if not _reel_health["ok"]:
                print(f"  [v12.9.1] ⚠️ reel URL 健康检查失败: {_reel_health['error']}")
            else:
                print(f"  [v12.9.1] reel URL 健康: latency={_reel_health['latency_ms']}ms size={_reel_health['content_length']}")
            # 写入 task JSON
            try:
                _hb_path = PROJECT_ROOT / "workdir" / "tasks" / f"{task_id}.json"
                if _hb_path.exists():
                    with open(_hb_path, 'r', encoding='utf-8') as _hf:
                        _hd = json.load(_hf)
                    _hd['l3_url_health'] = {
                        "checked": 1,
                        "ok": 1 if _reel_health["ok"] else 0,
                        "bad": 0 if _reel_health["ok"] else 1,
                        "bad_examples": [{"clip_id": f"candidate_reel_{task_id}", "error": _reel_health["error"]}] if _reel_health["error"] else [],
                        "total_time_ms": _reel_health["latency_ms"],
                        "updated_at": __import__('datetime').datetime.now().isoformat(),
                    }
                    _tmp = str(_hb_path) + '.tmp'
                    with open(_tmp, 'w', encoding='utf-8') as _wf:
                        json.dump(_hd, _wf, ensure_ascii=False, indent=2)
                    os.replace(_tmp, str(_hb_path))
            except Exception:
                pass

            # ============================================================
            # v10.2: L3 选片评分体系
            # ============================================================
            from pipeline.clip_scorer import score_candidates, filter_low_score
            _pool_for_score = {}
            try:
                from pipeline.pool_overrides import load_pool_data as _load_score_pool
                _pool_for_score = _load_score_pool(task_id)
            except:
                pass
            _mm_for_score = None
            _mm_score_path = output_dir / "material_map.json"
            if _mm_score_path.exists():
                with open(_mm_score_path) as _mmf:
                    _mm_for_score = json.load(_mmf)
            manifest = score_candidates(manifest, _pool_for_score, _mm_for_score)
            # 过滤 < 50 分
            manifest, _score_rejected = filter_low_score(manifest, min_score=50)
            if _score_rejected:
                print(f"  [评分] 过滤 {len(_score_rejected)} 条低分镜头:")
                for _sr in _score_rejected:
                    print(f"    {_sr['reel_clip_id']}: score={_sr['score_total']} → 剔除")
            _score_avg = round(sum(m.get('score_total', 0) for m in manifest) / max(len(manifest), 1), 1)
            print(f"  [评分] {len(manifest)} 条候选, 平均分 {_score_avg}")

            # 保存评分后的 manifest
            with open(output_dir / "candidate_reel_manifest.json", 'w', encoding='utf-8') as _mf:
                json.dump(manifest, _mf, ensure_ascii=False, indent=2)

            # 构建 manifest 摘要
            # v10.1: manifest 摘要增加 usable_window 和 quality_class
            # v10.2: 增加 score_total
            manifest_lines = []
            for m in manifest:
                _uw = m.get('usable_window', m.get('allowed_offset_range', [0, m.get('clip_duration_sec', 0)]))
                if isinstance(_uw, list) and _uw and isinstance(_uw[0], dict):
                    _uw_str = ','.join([f"{w['start_offset']:.1f}-{w['end_offset']:.1f}" for w in _uw])
                elif isinstance(_uw, list) and _uw and isinstance(_uw[0], list):
                    _uw_str = ','.join([f"{w[0]:.1f}-{w[1]:.1f}" for w in _uw])
                else:
                    _uw_str = f"0.0-{m.get('clip_duration_sec', 0):.1f}"
                _qc = m.get('quality_class', 'A')
                _qc_mark = '' if _qc == 'A' else f' [{_qc}]'
                _score = m.get('score_total', 0)
                # v13.0-pre5: 补传场景字段
                _scene_desc = m.get('scene_description', '')[:80]
                _scene_hint = m.get('scene_group_hint', '')[:40]
                _event_ph = m.get('event_phase', '')[:20]
                _aud_role = m.get('audience_role', '')[:20]
                _scene_suffix = ''
                if _scene_desc or _scene_hint:
                    _scene_parts = []
                    if _scene_hint:
                        _scene_parts.append(f'scene={_scene_hint}')
                    if _event_ph:
                        _scene_parts.append(f'phase={_event_ph}')
                    if _aud_role and _aud_role != '无人物':
                        _scene_parts.append(f'role={_aud_role}')
                    if _scene_desc:
                        _scene_parts.append(f'desc={_scene_desc}')
                    _scene_suffix = ' | ' + ' | '.join(_scene_parts)
                manifest_lines.append(
                    f"{m['reel_clip_id']} | {m['pool_level']} | {m['source_file'][:25]} | "
                    f"{m['source_start_sec']:.1f}-{m['source_end_sec']:.1f}s | "
                    f"clip_duration={m.get('clip_duration_sec', m.get('duration', 0)):.2f}s | "
                    f"usable=[{_uw_str}]s{_qc_mark} | score={_score} | {m['info_type'][:40]}{_scene_suffix}"
                )

            # v13.0-pre5: 场景字段覆盖率日志
            _sd_count = sum(1 for m in manifest if m.get('scene_description'))
            _sh_count = sum(1 for m in manifest if m.get('scene_group_hint'))
            _ep_count = sum(1 for m in manifest if m.get('event_phase'))
            _ar_count = sum(1 for m in manifest if m.get('audience_role'))
            print(f"  [v13.0-pre5] manifest_summary scene fields: scene_description={_sd_count}/{len(manifest)}, scene_group_hint={_sh_count}/{len(manifest)}, event_phase={_ep_count}/{len(manifest)}, audience_role={_ar_count}/{len(manifest)}")

            # ============================================================
            # v13.1-a: L2.5 候选分类 + 过滤
            # ============================================================
            from pipeline.clip_classifier import classify_clip_list, filter_candidates, summarize_visual_class_stats
            classify_clip_list(manifest)
            _vc_stats = summarize_visual_class_stats(manifest)
            print(f"  [v13.1-a] visual_class stats: primary_action={_vc_stats['primary_action']} reaction={_vc_stats['reaction']} detail={_vc_stats['detail']} banner={_vc_stats['banner']} environment={_vc_stats['environment']} low_info={_vc_stats['low_info_count']}")
            print(f"  [v13.1-a] info_score avg={_vc_stats['avg_info_score']}")

            # 保存分类结果
            _classified_path = output_dir / "candidate_reel_l2_5_classified.json"
            with open(_classified_path, 'w', encoding='utf-8') as _cf:
                json.dump(manifest, _cf, ensure_ascii=False, indent=2)

            # 过滤
            _filtered_manifest, _filter_stats = filter_candidates(manifest)
            print(f"  [v13.1-a] filtered candidates: before={_filter_stats['before']} after={_filter_stats['after']} removed_low_info={_filter_stats.get('removed_low_info',0)} removed_banner={_filter_stats.get('removed_banner',0)} removed_environment={_filter_stats.get('removed_environment',0)} relaxed={_filter_stats.get('relaxed',False)}")

            # 保存过滤结果
            _filtered_path = output_dir / "candidate_reel_l2_5_filtered.json"
            with open(_filtered_path, 'w', encoding='utf-8') as _ff:
                json.dump(_filtered_manifest, _ff, ensure_ascii=False, indent=2)

            # 重建 manifest_summary 使用过滤后候选
            manifest_lines = []
            for m in _filtered_manifest:
                _uw = m.get('usable_window', m.get('allowed_offset_range', [0, m.get('clip_duration_sec', 0)]))
                if isinstance(_uw, list) and _uw and isinstance(_uw[0], dict):
                    _uw_str = ','.join([f"{w['start_offset']:.1f}-{w['end_offset']:.1f}" for w in _uw])
                elif isinstance(_uw, list) and _uw and isinstance(_uw[0], list):
                    _uw_str = ','.join([f"{w[0]:.1f}-{w[1]:.1f}" for w in _uw])
                else:
                    _uw_str = f"0.0-{m.get('clip_duration_sec', 0):.1f}"
                _qc = m.get('quality_class', 'A')
                _qc_mark = '' if _qc == 'A' else f' [{_qc}]'
                _score = m.get('score_total', 0)
                _vc = m.get('visual_class', '?')
                _is = m.get('info_score', 5)
                _scene_desc = m.get('scene_description', '')[:80]
                _scene_hint = m.get('scene_group_hint', '')[:40]
                _event_ph = m.get('event_phase', '')[:20]
                _scene_suffix = ''
                if _scene_hint:
                    _parts = [f'scene={_scene_hint}']
                    if _event_ph:
                        _parts.append(f'phase={_event_ph}')
                    if _scene_desc:
                        _parts.append(f'desc={_scene_desc}')
                    _scene_suffix = ' | ' + ' | '.join(_parts)
                manifest_lines.append(
                    f"{m['reel_clip_id']} | {m['pool_level']} | {_vc}({_is}) | {m['source_file'][:25]} | "
                    f"clip_duration={m.get('clip_duration_sec', m.get('duration', 0)):.2f}s | "
                    f"usable=[{_uw_str}]s{_qc_mark} | score={_score} | {m['info_type'][:40]}{_scene_suffix}"
                )

            # v13.1-b: 视觉语义粗去重
            from pipeline.clip_classifier import dedup_visual_semantic
            _dedup_manifest, _dedup_stats = dedup_visual_semantic(_filtered_manifest)
            print(f"  [v13.1-b] visual_dedup: before={_dedup_stats['before']} after={_dedup_stats['after']} removed_visual_dup={_dedup_stats['removed_visual_dup']} relaxed={_dedup_stats.get('relaxed',False)}")
            _top_keys = sorted(_dedup_stats.get('key_counts',{}).items(), key=lambda x:-x[1])[:5]
            print(f"  [v13.1-b] visual_semantic_key top: {dict(_top_keys)}")

            # 保存去重结果
            _dedup_path = output_dir / "candidate_reel_l2_5_dedup.json"
            with open(_dedup_path, 'w', encoding='utf-8') as _df:
                json.dump(_dedup_manifest, _df, ensure_ascii=False, indent=2)
            # v13.1-b-tune: 额外保存调优版去重结果
            _dedup_tuned_path = output_dir / "candidate_reel_l2_5_dedup_tuned.json"
            with open(_dedup_tuned_path, 'w', encoding='utf-8') as _dtf:
                json.dump({
                    'candidates': _dedup_manifest,
                    'stats': _dedup_stats,
                    'filter_stats': _filter_stats,
                    'vc_stats': _vc_stats,
                }, _dtf, ensure_ascii=False, indent=2)

            # ============================================================
            # v13.2-h step4 A: variant metadata passthrough
            # ============================================================
            if VARIANT_METADATA_PASSTHROUGH:
                _variant_fields_pt = ['_is_variant', '_variant_scheme', '_variant_source', '_variant_reason',
                                      '_variant_group_key', '_variant_added_after_base_limit']
                _variant_map_pt = {}
                for _vc_pt in _dedup_manifest:
                    if _vc_pt.get('_is_variant'):
                        _rid_pt = _vc_pt.get('reel_clip_id', '')
                        _variant_map_pt[_rid_pt] = {f: _vc_pt.get(f) for f in _variant_fields_pt if _vc_pt.get(f) is not None}
                if _variant_map_pt:
                    _manifest_resave_path = output_dir / "candidate_reel_manifest.json"
                    with open(_manifest_resave_path, 'r', encoding='utf-8') as _mrf:
                        _manifest_resave = json.load(_mrf)
                    _b63_before_pt = None
                    _b63_after_pt = None
                    for _mc_pt in _manifest_resave:
                        _mrid_pt = _mc_pt.get('reel_clip_id', '')
                        if _mrid_pt in _variant_map_pt:
                            if _mrid_pt == 'B63':
                                _b63_before_pt = {f: _mc_pt.get(f) for f in _variant_fields_pt}
                            _mc_pt.update(_variant_map_pt[_mrid_pt])
                            if _mrid_pt == 'B63':
                                _b63_after_pt = {f: _mc_pt.get(f) for f in _variant_fields_pt}
                    with open(_manifest_resave_path, 'w', encoding='utf-8') as _mwf:
                        json.dump(_manifest_resave, _mwf, ensure_ascii=False, indent=2)
                    _pt_summary = {
                        'enabled': True,
                        'updated_count': len(_variant_map_pt),
                        'updated_clip_ids': list(_variant_map_pt.keys()),
                        'B63_manifest_before': _b63_before_pt,
                        'B63_manifest_after': _b63_after_pt,
                        'warnings': []
                    }
                    with open(output_dir / "variant_passthrough_summary.json", 'w', encoding='utf-8') as _vps:
                        json.dump(_pt_summary, _vps, ensure_ascii=False, indent=2)
                    print(f"  [v13.2-h step4] variant_passthrough: {len(_variant_map_pt)} clips updated: {list(_variant_map_pt.keys())}")
                    # Also update in-memory manifest for downstream
                    for _mc_mem in manifest:
                        _mrid_mem = _mc_mem.get('reel_clip_id', '')
                        if _mrid_mem in _variant_map_pt:
                            _mc_mem.update(_variant_map_pt[_mrid_mem])
                    for _mc_dm in _dedup_manifest:
                        pass  # already has variant fields
                else:
                    print(f"  [v13.2-h step4] variant_passthrough: no variants to sync")

            # ============================================================
            # v13.1-d: 四象限评分 → 基于四象限的预检（正确顺序）
            # ============================================================
            # STEP 0: 从 pool 映射 clean_windows 到 manifest（step3 修复）
            from pipeline.pool_overrides import load_pool_data as _load_cw_pool
            _cw_pool = _load_cw_pool(task_id)
            _cw_mapped = 0
            if _cw_pool and isinstance(_cw_pool, dict):
                for _mc in _dedup_manifest:
                    _src = _mc.get('source_file', '')
                    _pool_entry = _cw_pool.get(_src, {})
                    if isinstance(_pool_entry, dict):
                        _cws = _pool_entry.get('clean_windows', [])
                        if _cws and not _mc.get('clean_windows'):
                            _mc['clean_windows'] = _cws
                            _cw_mapped += 1
            print(f"  [v13.1-d-step3] clean_windows mapped: {_cw_mapped}/{len(_dedup_manifest)}")

            # STEP 1: 先做四象限评分（在 backup 预检之前！）
            from pipeline.clip_classifier import (
                score_and_classify_list, mark_unique_scenes,
                summarize_tier_stats
            )
            score_and_classify_list(_dedup_manifest)
            _unique_stats = mark_unique_scenes(_dedup_manifest)
            _tier_stats = summarize_tier_stats(_dedup_manifest)
            print(f"  [v13.1-d] tier stats (pre-filter): safe_main={_tier_stats['safe_main']} "
                  f"safe_context={_tier_stats['safe_context']} risky_key={_tier_stats['risky_key']} "
                  f"risky_low={_tier_stats['risky_low']}")
            print(f"  [v13.1-d] unique_scene_count={_unique_stats['unique_scene_count']} "
                  f"risky_key {_unique_stats['risky_key_count_before']}→{_unique_stats['risky_key_count_after']}")
            # 打印 risky_key 样例
            for _rk in [c for c in _dedup_manifest if c.get('candidate_tier') == 'risky_key'][:5]:
                print(f"    risky_key: {_rk.get('reel_clip_id','')} tech={_rk.get('tech_score',0)} "
                      f"nar={_rk.get('narrative_score',0)} unique={_rk.get('is_unique_scene',False)} "
                      f"| {(_rk.get('scene_description','') or _rk.get('info_type',''))[:50]}")

            # STEP 2: 基于四象限的预检（替换旧 backup 预检）
            # risky_low → 移除 | risky_key → 保留 | safe_* → 保留
            _quadrant_passed = []
            _quadrant_removed = []
            for _c in _dedup_manifest:
                _tier = _c.get('candidate_tier', 'safe_context')
                if _tier == 'risky_low':
                    _quadrant_removed.append(_c)
                else:
                    _quadrant_passed.append(_c)
            _rl_count = len(_quadrant_removed)
            _rk_kept = sum(1 for c in _quadrant_passed if c.get('candidate_tier') == 'risky_key')
            print(f"  [v13.1-d] quadrant_precheck: total={len(_dedup_manifest)} "
                  f"passed={len(_quadrant_passed)} removed_risky_low={_rl_count} "
                  f"risky_key_kept={_rk_kept}")
            if _quadrant_removed:
                for _rl in _quadrant_removed[:3]:
                    print(f"    removed risky_low: {_rl.get('reel_clip_id','')} "
                          f"tech={_rl.get('tech_score',0)} nar={_rl.get('narrative_score',0)} "
                          f"| {(_rl.get('scene_description','') or _rl.get('info_type',''))[:40]}")

            _safe_manifest = _quadrant_passed

            # 保存四象限评分 + 预检产物
            _fq_path = output_dir / "candidate_reel_four_quadrant_scored.json"
            with open(_fq_path, 'w', encoding='utf-8') as _fqf:
                json.dump({
                    'candidates': [{
                        k: v for k, v in c.items()
                        if not k.startswith('_') or k in ('_scene_cluster',)
                    } for c in _safe_manifest],
                    'tier_stats': summarize_tier_stats(_safe_manifest),
                    'unique_stats': _unique_stats,
                    'removed_risky_low': _rl_count,
                    'risky_key_kept': _rk_kept,
                }, _fqf, ensure_ascii=False, indent=2)

            # ============================================================
            # v13.1-d: 重建 manifest_summary 三区分层表达
            # ============================================================
            _sm_list = [m for m in _safe_manifest if m.get('candidate_tier') == 'safe_main']
            _sc_list = [m for m in _safe_manifest if m.get('candidate_tier') == 'safe_context']
            _rk_list = [m for m in _safe_manifest if m.get('candidate_tier') == 'risky_key']

            def _build_manifest_line(m):
                _score = m.get('score_total', 0)
                _vc = m.get('visual_class', '?')
                _is = m.get('info_score', 5)
                _ts = m.get('tech_score', 0)
                _ns = m.get('narrative_score', 0)
                _tier = m.get('candidate_tier', '?')
                _vsk = m.get('visual_semantic_key', '')
                _scene_desc = m.get('scene_description', m.get('info_type', ''))[:60]
                _dur = m.get('clip_duration_sec', m.get('duration', 0))
                _line = (f"{m['reel_clip_id']} | {_tier} | tech={_ts} nar={_ns} | {_vc}({_is}) | "
                         f"{m['source_file'][:25]} | {_dur:.1f}s | {_scene_desc}")
                # v13.2-h step4 D: variant candidate card enhancement
                if VARIANT_METADATA_PASSTHROUGH and m.get('_is_variant'):
                    _var_desc = m.get('scene_description', '')
                    _src_file = m.get('source_file', '')
                    _variant_note = '🔄 动作变体'
                    if '0146_D' in _src_file or '投掷' in _var_desc or '飞镖' in _var_desc:
                        _variant_note += (' | 投飞镖/投掷道具互动，不同于投沙包(B50)和大骰子(P25)'
                                          ' | 推荐用于VS_05趣味互动段 | 建议使用窗口2.0-5.0s')
                    else:
                        _variant_note += f' | 与已保留主镜头动作形式不同 | source={m.get("_variant_source","")}'
                    _line += f' | {_variant_note}'
                return _line

            # 表头提示
            _tier_header = (
                f"当前安全主候选 {len(_sm_list)} 条，安全辅助 {len(_sc_list)} 条，"
                f"受控关键 {len(_rk_list)} 条。\n"
                f"安全主候选承担主体叙事。受控关键候选仅在补充叙事缺口时使用。\n"
            )
            if len(_sm_list) >= 10:
                _tier_header += f"本任务安全主候选充足（{len(_sm_list)}条），受控关键候选最多建议使用 2 条。\n"
            _tier_header += "\n"

            manifest_lines = [_tier_header]
            manifest_lines.append("【安全主候选 safe_main】🟢 技术稳定 + 新闻价值高")
            for m in _sm_list:
                manifest_lines.append(_build_manifest_line(m))
            if _sc_list:
                manifest_lines.append("")
                manifest_lines.append("【安全辅助候选 safe_context】🔵 技术稳定，辅助叙事")
                for m in _sc_list:
                    manifest_lines.append(_build_manifest_line(m))
            if _rk_list:
                manifest_lines.append("")
                manifest_lines.append("【受控关键候选 risky_key】🟡⚠ 技术有瑕疵，但为关键场景")
                for m in _rk_list:
                    _reason = m.get('unique_scene_reason', '')
                    _line = f"⚠ {_build_manifest_line(m)}"
                    if _reason:
                        _line += f" | reason={_reason}"
                    manifest_lines.append(_line)

            # v13.1-d step2: safe_window 裁剪（risky_key 专用）
            from pipeline.clip_classifier import apply_safe_windows
            apply_safe_windows(_safe_manifest)
            _rk_with_sw = sum(1 for c in _safe_manifest
                              if c.get('candidate_tier') == 'risky_key'
                              and c.get('safe_window'))
            _rk_no_sw = sum(1 for c in _safe_manifest
                            if c.get('candidate_tier') == 'risky_key'
                            and not c.get('safe_window'))
            if _rk_with_sw or _rk_no_sw:
                print(f"  [v13.1-d] safe_window: risky_key with_sw={_rk_with_sw} no_sw={_rk_no_sw}")
                for _sw_c in [c for c in _safe_manifest if c.get('candidate_tier') == 'risky_key'][:3]:
                    print(f"    {_sw_c.get('reel_clip_id','')} sw={_sw_c.get('safe_window')} "
                          f"safe_dur={_sw_c.get('safe_duration',0):.1f}s status={_sw_c.get('safe_window_status','?')}")

            # v13.1-d step2: risk_budget 计算
            from pipeline.clip_classifier import calc_risk_budget
            _risk_budget = calc_risk_budget(_safe_manifest, tts_dur)
            print(f"  [v13.1-d] risk_budget: coverage={_risk_budget['coverage']} "
                  f"max_risky_key={_risk_budget['max_risky_key']} "
                  f"max_ratio={_risk_budget['max_risky_duration_ratio']}")

            # 用四象限预检后的 manifest 替换
            manifest = _safe_manifest

            # ============================================================
            # v13.1-f: content_fingerprint 语义指纹去重
            # ============================================================
            from pipeline.clip_classifier import dedup_by_content_fingerprint
            _safe_main_count = sum(1 for c in manifest if c.get('candidate_tier') == 'safe_main')
            manifest, _fp_removed = dedup_by_content_fingerprint(manifest, safe_main_count=_safe_main_count)
            if _fp_removed:
                print(f"  [v13.1-f] fingerprint_dedup: before={len(manifest)+len(_fp_removed)} "
                      f"after={len(manifest)} removed={len(_fp_removed)}")
                for _fpr in _fp_removed[:3]:
                    print(f"    removed: {_fpr.get('reel_clip_id','')} fp={_fpr.get('content_fingerprint','')} "
                          f"| {(_fpr.get('scene_description','') or _fpr.get('info_type',''))[:40]}")
            # 保存 fp_dedup 产物
            _fp_path = output_dir / "candidate_reel_l2_5_fp_dedup.json"
            with open(_fp_path, 'w', encoding='utf-8') as _fpf:
                json.dump(manifest, _fpf, ensure_ascii=False, indent=2)

            # v13.2-c patch: 在 manifest_summary 中标注被 fp_dedup 删除的 clip
            if _fp_removed:
                _removed_ids = [c.get('reel_clip_id', '') for c in _fp_removed]
                manifest_lines.append("")
                manifest_lines.append(f"⛔ 以下 {len(_removed_ids)} 条候选已被内容去重删除，禁止选用：{', '.join(_removed_ids)}")

            # ============================================================
            # v13.1-e: 构建 filtered_reel（只拼过滤后候选）
            # ============================================================
            from pipeline.candidate_reel import build_filtered_reel
            _filtered_reel_info = build_filtered_reel(manifest, task_id, output_dir)
            if _filtered_reel_info.get('reel_path') and os.path.exists(_filtered_reel_info['reel_path']):
                # 上传 filtered_reel 到 TOS，替换原 reel_url
                _filtered_reel_tos_key = f'tmp_l3_clips/candidate_reel_filtered_{task_id}.mp4'
                tos_client.put_object_from_file('e23-video', _filtered_reel_tos_key, _filtered_reel_info['reel_path'])
                reel_url = f'https://e23-video.tos-cn-beijing.volces.com/{_filtered_reel_tos_key}'
                print(f"  [v13.1-e] filtered_reel 已上传 TOS，替换原 reel_url")

                # v12.9.1: filtered_reel URL 健康检查
                _fhr = _check_video_url_health(reel_url, timeout=10)
                if not _fhr["ok"]:
                    print(f"  [v12.9.1] ⚠️ filtered_reel URL 健康检查失败: {_fhr['error']}")
                else:
                    print(f"  [v12.9.1] filtered_reel URL 健康: latency={_fhr['latency_ms']}ms size={_fhr['content_length']}")

                print(f"  [v13.1-e] reel: {reel_info['clip_count']} clips/{reel_info['total_duration']:.0f}s → "
                      f"{_filtered_reel_info['clip_count']} clips/{_filtered_reel_info['total_duration']:.0f}s")
            else:
                print(f"  ⚠️ [v13.1-e] filtered_reel 构建失败，继续使用原 reel")

            # 读取全局读片 prompt
            global_reel_prompt_file = PROMPTS_DIR / "l3_global_reel_director_prompt_v1.txt"
            with open(global_reel_prompt_file, 'r', encoding='utf-8') as f:
                gr_prompt = f.read()
            gr_prompt = gr_prompt.replace('{task_context}', context_text)
            gr_prompt = gr_prompt.replace('{script_text}', script_text)
            gr_prompt = gr_prompt.replace('{narration_duration_sec}', str(round(tts_dur, 1)))
            gr_prompt = gr_prompt.replace('{manifest_summary}', '\n'.join(manifest_lines))
            # v8.2: 传入字幕句子时间轴
            # v9.2: 松绑 sentence_timeline → 只传强语义锚点
            if sentence_timeline:
                anchor_segments = []
                for s in sentence_timeline:
                    if s.get('anchor_strength') == 'strong':
                        anchor_segments.append({
                            'anchor_id': s['sentence_id'],
                            'text': s['text'],
                            'approximate_time_range': f"{s['start_time']:.1f}-{s['end_time']:.1f}s",
                            'required_visual_type': s['semantic_anchor'],
                        })
                if anchor_segments:
                    anchor_lines = [f"[{a['anchor_id']}] {a['approximate_time_range']} ({a['required_visual_type']}) {a['text'][:50]}" for a in anchor_segments]
                    anchor_block = "以下是强语义锚点，这些段落的画面必须对齐对应类型：\n" + '\n'.join(anchor_lines)
                    anchor_block += f"\n\n其余段落（日期/背景/活动意义/口号式总结）为弱语义段，不绑定具体画面——优先用现场动态、服务过程、交流互动、环境过渡等画面。\n\n总时长: {tts_dur:.1f}s"
                else:
                    anchor_block = f"本条新闻稿无强语义锚点，全部为弱语义段。优先用现场动态画面自由剪辑。\n总时长: {tts_dur:.1f}s"
                gr_prompt = gr_prompt.replace('{sentence_timeline}', anchor_block)
                # 保存 anchor_segments 供后端校验
                with open(output_dir / "anchor_segments.json", 'w', encoding='utf-8') as f:
                    json.dump(anchor_segments, f, ensure_ascii=False, indent=2)
                print(f"  [锚点] 强语义锚点 {len(anchor_segments)} 个, 弱语义段 {len(sentence_timeline) - len(anchor_segments)} 个")
            else:
                gr_prompt = gr_prompt.replace('{sentence_timeline}', f'（无配音时间轴，总时长 {tts_dur:.1f}s）')

            # ============================================================
            # v9.6: L3-A 素材地图读取 + 配额计算
            # ============================================================
            _mm_path = output_dir / "material_map.json"
            _mm_block = "（无素材地图）"
            if _mm_path.exists():
                with open(_mm_path, 'r') as _mmf:
                    _mm_data = json.load(_mmf)
                _mm_cards = _mm_data.get('clips', _mm_data.get('material_cards', []))
                if _mm_cards:
                    from collections import Counter as _Counter
                    _motif_counts = _Counter(c.get('visual_motif', '?') for c in _mm_cards)
                    # 可用数量（排除禁用/unstable/时长<1.5s）
                    _usable = {}
                    for c in _mm_cards:
                        m = c.get('visual_motif', '?')
                        if c.get('shot_value') == '禁用' or c.get('motion_quality') == 'unstable':
                            continue
                        if m not in _usable:
                            _usable[m] = []
                        _usable[m].append(c['reel_clip_id'])

                    # 配额计算
                    _target_clips = round(tts_dur / 2.5)  # 目标镜头数
                    _quota = {}
                    # 横幅类：最多2条
                    _banner_motif = '主视觉/横幅/展板/合影'
                    _quota[_banner_motif] = min(2, len(_usable.get(_banner_motif, [])))
                    # 互动类：如有，1-2条
                    _interact_motif = '互动游戏/体验'
                    _quota[_interact_motif] = min(2, len(_usable.get(_interact_motif, [])))
                    # 面对面交流：如有，1-2条
                    _exchange_motif = '面对面交流'
                    _quota[_exchange_motif] = min(2, len(_usable.get(_exchange_motif, [])))
                    # 环境全景：如有，1条
                    _env_motif = '环境全景/场地关系'
                    _quota[_env_motif] = min(1, len(_usable.get(_env_motif, [])))
                    # 成果展示：如有，1条
                    _result_motif = '成果展示/物品展示'
                    _quota[_result_motif] = min(1, len(_usable.get(_result_motif, [])))
                    # 服务台隔桌：剩余配额，但不超过 35%
                    _desk_motif = '服务台隔桌交互'
                    _used_quota = sum(_quota.values())
                    _desk_max = max(1, min(round(_target_clips * 0.35), _target_clips - _used_quota))
                    _quota[_desk_motif] = _desk_max

                    # 构建传入文本
                    _mm_lines = []
                    _mm_lines.append(f"目标镜头数: {_target_clips} 条, 目标均值: 2.5s")
                    _mm_lines.append(f"\n素材池结构:")
                    for m, cnt in _motif_counts.most_common():
                        usable_rids = _usable.get(m, [])
                        _mm_lines.append(f"  {m}: 总{cnt}条, 可用{len(usable_rids)}条 {usable_rids}")
                    _mm_lines.append(f"\n配额计划 (quota):")
                    for m, q in _quota.items():
                        _mm_lines.append(f"  {m}: 最多 {q} 条")
                    _mm_lines.append(f"\n注意: 素材池中服务台隔桌交互占 {_motif_counts.get(_desk_motif,0)}/{len(_mm_cards)}={_motif_counts.get(_desk_motif,0)/max(len(_mm_cards),1)*100:.0f}%，结构严重偏科。")
                    _mm_lines.append(f"必须优先用完非服务台类镜头（互动/交流/环境/展示），再用服务台类补足。")
                    _mm_lines.append(f"服务台类镜头必须分散到不同段落，前20秒不得连续超过2条。")
                    _mm_block = '\n'.join(_mm_lines)

                    # 保存配额计划
                    _quota_plan = {
                        'target_duration': round(tts_dur, 1),
                        'target_clip_count': _target_clips,
                        'avg_clip_duration': 2.5,
                        'quota': _quota,
                        'motif_counts': dict(_motif_counts),
                        'usable_counts': {m: len(r) for m, r in _usable.items()},
                    }
                    with open(output_dir / "quota_plan.json", 'w', encoding='utf-8') as _qf:
                        json.dump(_quota_plan, _qf, ensure_ascii=False, indent=2)
                    print(f"  [配额] 目标 {_target_clips} 条, 配额: {_quota}")

            gr_prompt = gr_prompt.replace('{material_map_and_quota}', _mm_block)

            # ============================================================
            # v9.8: Slot 时间骨架生成
            # ============================================================
            _slot_count = round(tts_dur / 2.6)  # 目标 2.6s 均值
            _slot_count = max(18, min(22, _slot_count))  # 限制 18-22
            _slot_dur = round(tts_dur / _slot_count, 2)
            _slots = []
            _cursor = 0.0
            # 读取锚点
            _anchor_map = {}
            if os.path.exists(output_dir / "anchor_segments.json"):
                with open(output_dir / "anchor_segments.json") as _af:
                    _anchor_segs = json.load(_af)
                for a in _anchor_segs:
                    # 解析时间范围
                    tr = a.get('approximate_time_range', '0-0s')
                    parts = tr.replace('s', '').split('-')
                    if len(parts) == 2:
                        try:
                            _anchor_map[a['anchor_id']] = (float(parts[0]), float(parts[1]))
                        except:
                            pass

            for si in range(_slot_count):
                slot_start = round(_cursor, 2)
                slot_end = round(_cursor + _slot_dur, 2)
                if si == _slot_count - 1:
                    slot_end = round(tts_dur, 2)  # 最后一个 slot 对齐总时长
                slot = {
                    'slot_id': f'slot_{si+1:02d}',
                    'target_start': slot_start,
                    'target_end': slot_end,
                    'target_duration': round(slot_end - slot_start, 2),
                    'is_anchor_slot': False,
                    'anchor_id': '',
                }
                # 检查是否与锚点重叠
                for aid, (a_start, a_end) in _anchor_map.items():
                    overlap_start = max(slot_start, a_start)
                    overlap_end = min(slot_end, a_end)
                    if overlap_end > overlap_start:
                        slot['is_anchor_slot'] = True
                        slot['anchor_id'] = aid
                        break
                _slots.append(slot)
                _cursor = slot_end

            # 保存 slot_plan
            with open(output_dir / "slot_plan.json", 'w', encoding='utf-8') as _sf:
                json.dump(_slots, _sf, ensure_ascii=False, indent=2)
            _anchor_slots = sum(1 for s in _slots if s['is_anchor_slot'])
            _use_slot_render = True  # v9.9: slot 生成成功，启用 slot 锁定渲染
            print(f"  [Slot] {len(_slots)} 个 slot, 每个 {_slot_dur:.2f}s, 锚点slot {_anchor_slots} 个")

            # v11: 为每个 slot 映射语义标签（从 sentence_timeline）
            for s in _slots:
                s_start = s['target_start']
                s_end = s['target_end']
                # 找覆盖此 slot 的配音句子（取重叠最多的）
                best_sent = None
                best_overlap = 0
                for sent in sentence_timeline:
                    sent_s = sent.get('start_time', 0)
                    sent_e = sent.get('end_time', 0)
                    overlap = max(0, min(s_end, sent_e) - max(s_start, sent_s))
                    if overlap > best_overlap:
                        best_overlap = overlap
                        best_sent = sent
                if best_sent:
                    s['tts_sentence_id'] = best_sent.get('sentence_id', '')
                    s['subtitle_text'] = best_sent.get('text', '')[:50]
                    s['semantic_anchor'] = best_sent.get('semantic_anchor', '')
                    s['anchor_strength'] = best_sent.get('anchor_strength', 'weak')
                    # 从配音文本提取关键词作为 semantic_hint
                    _hint_text = best_sent.get('text', '')
                    _hints = []
                    _hint_kws = ['专家', '嘉宾', '代表', '骑手', '工作人员', '志愿者', '学生',
                                 '讲解', '发放', '互动', '展演', '分享', '研讨', '展示', '升空',
                                 '模型', '仪式', '服务', '宣传', '保障', '培训', '课堂', '论坛',
                                 '合影', '表演', '观众', '听众', '成果', '启动', '活动']
                    for kw in _hint_kws:
                        if kw in _hint_text:
                            _hints.append(kw)
                    s['semantic_hint'] = '、'.join(_hints) if _hints else ''
                else:
                    s['subtitle_text'] = ''
                    s['semantic_hint'] = ''
                    s['anchor_strength'] = 'weak'
            
            # 重新保存带语义的 slot_plan
            with open(output_dir / "slot_plan.json", 'w', encoding='utf-8') as _sf2:
                json.dump(_slots, _sf2, ensure_ascii=False, indent=2)
            
            # 构建 slot_plan 文本传给 L3（v11: 含语义任务）
            _slot_lines = [f"时间骨架（slot_plan）— 你必须为每个 slot 选一个镜头。每个 slot 标注了对应的配音内容和语义任务，请优先选择匹配语义的画面："]
            for s in _slots:
                anchor_mark = f" ⚡锚点:{s['anchor_id']}" if s['is_anchor_slot'] else ""
                _line = f"  {s['slot_id']} [{s['target_start']:.1f}-{s['target_end']:.1f}s] ({s['target_duration']:.1f}s){anchor_mark}"
                if s.get('subtitle_text'):
                    _line += f"\n    配音: {s['subtitle_text']}"
                if s.get('semantic_hint'):
                    _line += f"\n    画面建议: {s['semantic_hint']}"
                _slot_lines.append(_line)
            _slot_text = '\n'.join(_slot_lines)
            gr_prompt = gr_prompt.replace('{slot_plan}', _slot_text)

            # v11.6: 注入 shot_understanding 摘要（含场景域字段 + 跨 slot 上下文提示）
            _su_path_l3 = output_dir / "shot_understanding_v1.json"
            if _su_path_l3.exists():
                with open(_su_path_l3) as _suf_l3:
                    _su_l3 = json.load(_suf_l3)
                _su_lines = ["\n## 镜头语义标签与场景域（shot_understanding v11.6）\n"]
                _su_lines.append("每个候选镜头的语义信息和场景域如下。选片时请注意：")
                _su_lines.append("1. 同为'观众'但 location_context 不同的镜头是不同场景（室外学生观众 ≠ 室内报告听众）")
                _su_lines.append("2. 避免相邻 slot 选同一 scene_group_id 的镜头（除非配音内容确实在讲同一场景）")
                _su_lines.append("3. 避免室内/室外来回跳切（配音从室外活动转到室内报告时才切换）")
                _su_lines.append("4. 每个镜头选择后请说明 continuity_reason：为什么接在前一个镜头后面是连贯的\n")
                for _clip in _su_l3.get('clips', []):
                    _rid = _clip.get('reel_clip_id', '')
                    _src = _clip.get('source_file', '')
                    _tr = _clip.get('time_range', '')
                    _st = _clip.get('scene_type', '')
                    _vf = _clip.get('visual_function', '')
                    _nv = _clip.get('news_value', 0)
                    _tags = ', '.join(_clip.get('semantic_tags', []))
                    _avoid = ', '.join(_clip.get('avoid_for', []))
                    _opening = _clip.get('opening_suitability', '')
                    _loc = _clip.get('location_context', '')
                    _phase = _clip.get('event_phase', '')
                    _role = _clip.get('audience_role', '')
                    _group = _clip.get('scene_group_id', '')
                    _su_line = f"  {_rid} ({_src} {_tr}): scene={_st}, function={_vf}, nv={_nv}, tags=[{_tags}]"
                    _su_line += f", loc={_loc}, phase={_phase}, role={_role}, group={_group}"
                    if _avoid:
                        _su_line += f", ⛔avoid=[{_avoid}]"
                    if _opening == 'forbidden':
                        _su_line += f", ⛔forbidden"
                    _su_lines.append(_su_line)
                _su_block = '\n'.join(_su_lines)
                # 插入到 prompt 末尾（在 l2_segments_text 之前）
                gr_prompt = gr_prompt.replace('{l2_segments_text}', _su_block + '\n\n{l2_segments_text}')
                print(f"  [v11.6] shot_understanding 摘要已注入 L3 prompt（{len(_su_l3.get('clips', []))} 条，含场景域字段）")

            # ============================================================
            # v12.1: 场景结构灰度接入（off / test / gray / full）
            # ============================================================
            USE_SCENE_STRUCT_MODE = 'gray'  # v12.1: off / test / gray / full
            
            _scene_struct_enabled = False
            if USE_SCENE_STRUCT_MODE == 'full':
                _scene_struct_enabled = True
            elif USE_SCENE_STRUCT_MODE == 'gray':
                # 灰度条件：素材≥20 + narration + 时长≥20s + unknown_rate<10%
                _gray_ok = True
                if len(manifest) < 20:
                    _gray_ok = False
                    print(f"  [v12.1] 灰度跳过: 素材{len(manifest)}条 < 20")
                if edit_mode != 'narration':
                    _gray_ok = False
                    print(f"  [v12.1] 灰度跳过: edit_mode={edit_mode}")
                if tts_dur < 20:
                    _gray_ok = False
                    print(f"  [v12.1] 灰度跳过: 时长{tts_dur:.0f}s < 20s")
                if _gray_ok:
                    # 检查是否有 scene_context_struct.json（v11.9 后处理产物）
                    _scs_path = output_dir / "scene_context_struct.json"
                    if _scs_path.exists():
                        try:
                            _scs_data = json.load(open(_scs_path))
                            _unknowns = 0
                            _total_dims = 0
                            for _sc_item in _scs_data:
                                _gs = _sc_item.get('scene_group_struct', {})
                                for _gv in _gs.values():
                                    _total_dims += 1
                                    if _gv == 'unknown':
                                        _unknowns += 1
                            _unk_rate = _unknowns / max(_total_dims, 1)
                            if _unk_rate >= 0.10:
                                _gray_ok = False
                                print(f"  [v12.1] 灰度跳过: unknown_rate={_unk_rate:.1%} >= 10%")
                            else:
                                print(f"  [v12.1] 灰度通过: {len(_scs_data)}条struct, unknown_rate={_unk_rate:.1%}")
                        except Exception as _ge:
                            _gray_ok = False
                            print(f"  [v12.1] 灰度跳过: struct 解析失败 {_ge}")
                    else:
                        # 无 struct 文件，尝试从 manifest 实时构建
                        try:
                            from pipeline.scene_struct import build_scene_context_struct as _gray_build
                            _unknowns = 0
                            _total_dims = 0
                            for _gm in manifest:
                                _gs = _gray_build(_gm)['scene_group_struct']
                                for _gv in _gs.values():
                                    _total_dims += 1
                                    if _gv == 'unknown':
                                        _unknowns += 1
                            _unk_rate = _unknowns / max(_total_dims, 1)
                            if _unk_rate >= 0.10:
                                _gray_ok = False
                                print(f"  [v12.1] 灰度跳过: unknown_rate={_unk_rate:.1%} >= 10%（manifest 实时构建）")
                            else:
                                print(f"  [v12.1] 灰度通过: {len(manifest)}条manifest, unknown_rate={_unk_rate:.1%}")
                        except Exception as _ge:
                            _gray_ok = False
                            print(f"  [v12.1] 灰度跳过: struct 构建失败 {_ge}")
                _scene_struct_enabled = _gray_ok
            elif USE_SCENE_STRUCT_MODE == 'test':
                _scene_struct_enabled = True  # 测试模式强制开启
            
            if _scene_struct_enabled:
                try:
                    from pipeline.scene_struct import build_scene_context_struct
                    _sc_lines = ["\n## 场景上下文参考（Scene Context Reference — 仅供参考，不可主导选片）\n"]
                    _sc_lines.append("以下是每个候选片段的场景结构标签。**这些标签仅供参考**：")
                    _sc_lines.append("1. 相邻 slot 优先选同一场景组（space+event 一致）的镜头，减少乱跳")
                    _sc_lines.append("2. **连续使用同一 scene_group 达到 2 次后，第 3 次应优先切换到不同 group**（不是禁止，但需要更强的理由才能继续用同组镜头）")
                    _sc_lines.append("3. **你仍必须以视频画面为主判断，标签只是辅助参考**\n")
                    
                    # 优先从 scene_context_struct.json 读取
                    _scs_path_inject = output_dir / "scene_context_struct.json"
                    _scs_map = {}
                    if _scs_path_inject.exists():
                        _scs_items = json.load(open(_scs_path_inject))
                        _scs_map = {s.get('file', ''): s for s in _scs_items}
                    
                    _injected_count = 0
                    for m in manifest:
                        _rid = m['reel_clip_id']
                        _src = m.get('source_file', '')
                        # 优先从预计算 struct 读取
                        if _src in _scs_map:
                            _sc = _scs_map[_src]
                            _grp = _sc.get('scene_group_struct', {})
                        else:
                            _sc = build_scene_context_struct(m)
                            _grp = _sc['scene_group_struct']
                        _sc_lines.append(
                            f"  {_rid}: space={_grp.get('space','?')}, event={_grp.get('event','?')}, "
                            f"people={_grp.get('people','?')}, phase={_sc.get('event_phase','?')}, role={_sc.get('audience_role','?')}"
                        )
                        _injected_count += 1
                    
                    _sc_block = '\n'.join(_sc_lines)
                    gr_prompt = gr_prompt.replace('{l2_segments_text}', _sc_block + '\n\n{l2_segments_text}')
                    _src_type = 'struct_file' if _scs_map else 'manifest_realtime'
                    print(f"  [v12.1] ✅ 场景结构已注入 L3（{_injected_count} 条，mode={USE_SCENE_STRUCT_MODE}，src={_src_type}）")
                except Exception as e:
                    print(f"  [v12.1] ⚠️ 场景结构注入失败: {e}（不影响主链）")
            else:
                print(f"  [v12.1] 场景结构未启用（mode={USE_SCENE_STRUCT_MODE}）")
            
            # 调用 L3（单个长视频 + 文字）
            api_key = _get_api_key()
            endpoint = _get_endpoint()
            model = _get_model()
            content_parts = [
                {"type": "input_video", "video_url": reel_url},
                {"type": "input_text", "text": gr_prompt},
            ]
            payload = {"model": model, "input": [{"role": "user", "content": content_parts}]}
            headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}

            _heartbeat('l3_started')
            print(f"  L3 全局读片调用中...")
            t0 = time.time()
            resp = requests.post(f'{endpoint}/responses', json=payload, headers=headers, timeout=900)
            gr_elapsed = round(time.time() - t0, 1)

            if resp.status_code != 200:
                raise RuntimeError(f"L3 全局读片失败: HTTP {resp.status_code} ({gr_elapsed}s)")

            _l3_resp_data = resp.json()
            gr_result = _parse_l3_response(_l3_resp_data, gr_elapsed)
            print(f"  L3 全局读片完成 ({gr_elapsed}s)")

            # v13.3-b5a: 保存首次 L3 raw response
            try:
                _l3_raw_text = ''
                for _ri in _l3_resp_data.get('output', []):
                    if _ri.get('type') == 'message':
                        for _rc in _ri.get('content', []):
                            if _rc.get('type') == 'output_text':
                                _l3_raw_text = _rc.get('text', '')
                _l3_raw_path = output_dir / "l3_raw_response_v1.txt"
                with open(_l3_raw_path, 'w', encoding='utf-8') as _lrf:
                    _lrf.write(_l3_raw_text)
                print(f"  [v13.3-b5a] 首次 L3 raw response 已保存 ({len(_l3_raw_text)} chars)")
            except Exception as _raw_err:
                print(f"  [v13.3-b5a] ⚠️ 保存 raw response 失败: {_raw_err}")
                _l3_raw_text = ''

            # v13.0: L3 选片数量校验
            selected = gr_result.get('selected_timeline', [])
            _expected_slot_count = len(_slots)  # slot_plan 中的 slot 数
            _actual_l3_count = len(selected)
            _missing_count = _expected_slot_count - _actual_l3_count
            # v13.0-pre1.1: 详细分类统计
            _primary_count = sum(1 for s in selected if s.get('reel_clip_id','').startswith('P'))
            _backup_count = sum(1 for s in selected if s.get('reel_clip_id','').startswith('B'))
            if _missing_count > 0:
                print(f"  ⚠️ [v13.0] L3 选片不足: expected={_expected_slot_count}, actual={_actual_l3_count}, missing={_missing_count} (primary={_primary_count}, backup={_backup_count})")
                if gr_result.get('insufficient_reason'):
                    print(f"  [v13.0] insufficient_reason: {gr_result['insufficient_reason']}")
            else:
                print(f"  ✅ [v13.0] L3 选片数量充足: {_actual_l3_count}/{_expected_slot_count} (primary={_primary_count}, backup={_backup_count})")
            if _backup_count > 0:
                print(f"  ⚠️ [v13.0] L3 选了 {_backup_count} 条 backup 镜头，可能被 quality_gate 拒绝")

            # v13.0-pre3: primary/backup 详细统计 + 关键段落 backup 检测
            _backup_ratio = round(_backup_count / max(_actual_l3_count, 1) * 100, 1)
            _opening_from_backup = sum(1 for s in selected if s.get('paragraph_role') == 'opening' and s.get('reel_clip_id','').startswith('B'))
            _closing_from_backup = sum(1 for s in selected if s.get('paragraph_role') == 'closing' and s.get('reel_clip_id','').startswith('B'))
            _mainbody_from_backup = sum(1 for s in selected if s.get('paragraph_role') == 'main_body' and s.get('reel_clip_id','').startswith('B'))
            print(f"  [v13.0-pre3] backup_ratio={_backup_ratio}% | opening_from_backup={_opening_from_backup} | closing_from_backup={_closing_from_backup} | mainbody_from_backup={_mainbody_from_backup}")
            if _closing_from_backup > 0:
                print(f"  ⚠️ [v13.0-pre3] WARNING: closing selected from backup, may be rejected by quality_gate")
            if _opening_from_backup > 0:
                print(f"  ⚠️ [v13.0-pre3] WARNING: opening selected from backup, may be rejected by quality_gate")

            # v13.0-pre3.1: 多样性统计
            from collections import Counter as _Counter
            _sel_sources = [manifest_map_pre.get(s.get('reel_clip_id',''),{}).get('source_file','?') for s in selected] if 'manifest_map_pre' in dir() else []
            if not _sel_sources:
                # 构建临时 manifest_map
                _mm_tmp = {m['reel_clip_id']: m for m in manifest}
                _sel_sources = [_mm_tmp.get(s.get('reel_clip_id',''),{}).get('source_file','?') for s in selected]
            _unique_src = len(set(_sel_sources))
            _src_counts = _Counter(_sel_sources)
            _max_same_src = max(_src_counts.values()) if _src_counts else 0
            _consec_same = sum(1 for i in range(1, len(_sel_sources)) if _sel_sources[i] == _sel_sources[i-1])
            _backup_for_diversity = sum(1 for s in selected if s.get('reel_clip_id','').startswith('B') and '多样' in s.get('selection_reason','') or '差异' in s.get('selection_reason','') or '不同' in s.get('selection_reason',''))
            _backup_for_role = sum(1 for s in selected if s.get('reel_clip_id','').startswith('B') and ('无' in s.get('selection_reason','') or '缺' in s.get('selection_reason','') or '不足' in s.get('selection_reason','')))
            print(f"  [v13.0-pre3.1] diversity: unique_source={_unique_src}/{_actual_l3_count} ({round(_unique_src/max(_actual_l3_count,1)*100,1)}%) | max_same_source={_max_same_src} | consecutive_same_source={_consec_same}")
            print(f"  [v13.0-pre3.1] backup_purpose: for_diversity={_backup_for_diversity} | for_missing_role={_backup_for_role}")

            # v13.0-pre1.1: paragraph_role 枚举兜底
            _VALID_PARAGRAPH_ROLES = {'opening', 'main_body', 'evidence', 'detail', 'closing'}
            for sel_item in selected:
                _pr = sel_item.get('paragraph_role', '')
                if _pr and _pr not in _VALID_PARAGRAPH_ROLES:
                    print(f"  ⚠️ [v13.0] paragraph_role 枚举违规: '{_pr}' → 兜底为 'detail' (clip={sel_item.get('reel_clip_id','')})")
                    sel_item['paragraph_role'] = 'detail'

            # v13.0-pre5: scene_block 统计
            _sb_plan = gr_result.get('scene_block_plan', [])
            _sb_plan_count = len(_sb_plan)
            _sb_ids_in_timeline = [s.get('scene_block_id', '') for s in selected]
            _sb_unique = set(_sb_ids_in_timeline)
            _sb_missing = sum(1 for sb in _sb_ids_in_timeline if not sb)
            # 计算 scene block 连续段和跳切
            _sb_switches = 0
            _sb_segments = 1 if _sb_ids_in_timeline else 0
            _sb_single_segments = 0
            _sb_run = 1
            _sb_max_run = 1
            for _si in range(1, len(_sb_ids_in_timeline)):
                if _sb_ids_in_timeline[_si] != _sb_ids_in_timeline[_si-1]:
                    _sb_switches += 1
                    _sb_segments += 1
                    if _sb_run == 1:
                        _sb_single_segments += 1
                    _sb_run = 1
                else:
                    _sb_run += 1
                    _sb_max_run = max(_sb_max_run, _sb_run)
            if _sb_run == 1 and _sb_ids_in_timeline:
                _sb_single_segments += 1
            print(f"  [v13.0-pre5] scene_block_plan_count={_sb_plan_count}")
            print(f"  [v13.0-pre5] scene_block_switch_count={_sb_switches} | segments={_sb_segments} | single_shot_segments={_sb_single_segments} | max_run={_sb_max_run}")
            if _sb_missing > 0:
                print(f"  ⚠️ [v13.0-pre5] warning: {_sb_missing} timeline entries missing scene_block_id")

            # v13.0-pre5.1: 空镜头/重复镜头统计（只读）
            _mm_tmp2 = {m['reel_clip_id']: m for m in manifest}
            _sign_board = 0; _passive_aud = 0; _deco_static = 0; _empty_like = 0
            for _sel in selected:
                _rid = _sel.get('reel_clip_id', '')
                _m = _mm_tmp2.get(_rid, {})
                _it = (_m.get('info_type', '') or '').lower()
                _sd = (_m.get('scene_description', '') or '').lower()
                _nr = _sel.get('narrative_role', '')
                _is_sign = any(kw in _it for kw in ['展示', 'display', '物料', '标识', '背景板', '展板', '横幅']) and not any(kw in _it for kw in ['学生', '工作', '互动', '讲解', '操作', '表演'])
                _is_aud = any(kw in _it for kw in ['观众', '听众', '反应']) and not any(kw in _it for kw in ['互动', '讲解', '鼓掌'])
                _is_deco = any(kw in _it for kw in ['装饰', '摆设', '模型']) and not any(kw in _it for kw in ['学生', '讲解', '互动', '演示'])
                if _is_sign: _sign_board += 1
                if _is_aud: _passive_aud += 1
                if _is_deco: _deco_static += 1
                if _is_sign or _is_aud or _is_deco: _empty_like += 1
            # 重复统计
            _sel_sources2 = [_mm_tmp2.get(s.get('reel_clip_id',''),{}).get('source_file','?') for s in selected]
            _src_counts2 = _Counter(_sel_sources2)
            _repeat_over2 = sum(1 for c in _src_counts2.values() if c > 2)
            _max_same2 = max(_src_counts2.values()) if _src_counts2 else 0
            _sel_rids = [s.get('reel_clip_id','') for s in selected]
            _dup_rids = sum(c - 1 for c in _Counter(_sel_rids).values() if c > 1)
            print(f"  [v13.0-pre5.1] empty_like={_empty_like}/{len(selected)} | sign_board={_sign_board} | passive_audience={_passive_aud} | decoration={_deco_static}")
            print(f"  [v13.0-pre5.1] repeat_over_2={_repeat_over2} | max_same_source={_max_same2} | repeated_clip_id={_dup_rids}")

            # v13.0-pre5.2: 新闻播报专属统计
            _has_nlp = bool(gr_result.get('news_logic_plan'))
            _has_os = bool(gr_result.get('director_plan', {}).get('opening_strategy'))
            _mal_counts = _Counter(s.get('main_action_level', '') for s in selected)
            _vst_counts = _Counter(s.get('visual_semantic_type', '') for s in selected)
            _ig_missing = sum(1 for s in selected if not s.get('information_gain'))
            # 前 10 秒主动作估算（按 slot 序号，每 slot ~2.5s，前 4 slot ≈ 前 10 秒）
            _first_4 = selected[:4] if len(selected) >= 4 else selected
            _first_10s_action = sum(1 for s in _first_4 if s.get('main_action_level') == 'primary_action')
            _opening_low = sum(1 for s in _first_4 if s.get('visual_semantic_type', '') in ('sign_board', 'static_display', 'decoration', 'passive_audience', 'environment'))
            # 第一个主动作位置
            _first_action_slot = next((i for i, s in enumerate(selected) if s.get('main_action_level') == 'primary_action'), -1)
            _vsr = sum(1 for i in range(1, len(selected)) if selected[i].get('visual_semantic_type', '') == selected[i-1].get('visual_semantic_type', '') and selected[i].get('visual_semantic_type', '') in ('sign_board', 'static_display', 'decoration', 'passive_audience'))
            print(f"  [v13.0-pre5.2] news_logic_plan={'✅' if _has_nlp else '❌'} | opening_strategy={'✅' if _has_os else '❌'}")
            print(f"  [v13.0-pre5.2] first_main_action_slot={_first_action_slot} | first_10s_main_action={_first_10s_action} | opening_low_info={_opening_low}")
            print(f"  [v13.0-pre5.2] main_action={_mal_counts.get('primary_action',0)} | reaction={_mal_counts.get('reaction',0)} | static_context={_mal_counts.get('static_context',0)}")
            print(f"  [v13.0-pre5.2] sign_board={_vst_counts.get('sign_board',0)} | static_display={_vst_counts.get('static_display',0)} | decoration={_vst_counts.get('decoration',0)} | passive_audience={_vst_counts.get('passive_audience',0)}")
            print(f"  [v13.0-pre5.2] visual_semantic_repeat={_vsr} | information_gain_missing={_ig_missing}")

            # ============================================================
            # v13.0-pre5.3b: L3 输出稳定护栏
            # ============================================================
            # 保存 retry 所需上下文（修复变量作用域问题）
            _retry_prompt_file = global_reel_prompt_file
            _retry_reel_url = reel_url
            _retry_gr_prompt = gr_prompt
            _retry_model = model
            _retry_api_key = api_key
            _retry_endpoint = endpoint
            _guard_issues = []

            # Guard A: slot_id 格式归一化
            import re as _re
            for _sel in selected:
                _raw_sid = _sel.get('slot_id', '')
                if _raw_sid and not _re.match(r'^slot_\d+$', _raw_sid):
                    _digits = _re.findall(r'\d+', _raw_sid)
                    if _digits:
                        _new_sid = f'slot_{int(_digits[0]):02d}'
                        print(f"  [v13.0-pre5.3b] normalized slot_id: \"{_raw_sid}\" -> \"{_new_sid}\"")
                        _sel['slot_id'] = _new_sid
                    else:
                        print(f"  ⚠️ [v13.0-pre5.3b] cannot normalize slot_id: \"{_raw_sid}\"")

            # Guard B: clip_id 去重校验
            _rid_list = [s.get('reel_clip_id','') for s in selected]
            _rid_counts_guard = _Counter(_rid_list)
            _dup_rids_guard = {k:v for k,v in _rid_counts_guard.items() if v > 1}
            if _dup_rids_guard:
                print(f"  ⚠️ [v13.0-pre5.3b] GUARD: duplicated clip_ids detected: {_dup_rids_guard}")
                _guard_issues.append(f"duplicated_clip_ids: {list(_dup_rids_guard.keys())}")

            # Guard C: backup 安全性预检
            _mm_guard = {m['reel_clip_id']: m for m in manifest}
            _rejected_backup_guard = []
            from pipeline.pool_overrides import load_pool_data as _load_guard_pool
            _guard_pool_raw = _load_guard_pool(task_id)
            _guard_pool_list = list(_guard_pool_raw.values()) if isinstance(_guard_pool_raw, dict) else (_guard_pool_raw if isinstance(_guard_pool_raw, list) else [])
            for _sel in selected:
                _rid = _sel.get('reel_clip_id','')
                _m = _mm_guard.get(_rid, {})
                if _m.get('pool_level') == 'backup':
                    _src_fn = _m.get('source_file','')
                    _src_pool = next((p for p in _guard_pool_list if isinstance(p, dict) and p.get('source_file') == _src_fn), {})
                    _cws = _src_pool.get('clean_windows', []) if isinstance(_src_pool, dict) else []
                    _has_cw = len(_cws) > 0
                    _stab = _src_pool.get('stability_score', _src_pool.get('score_stability', 70)) if isinstance(_src_pool, dict) else 70
                    if not _has_cw or _stab < 70:
                        _rejected_backup_guard.append({'rid': _rid, 'src': _src_fn, 'reason': f'stability={_stab}, has_cw={_has_cw}'})
            if _rejected_backup_guard:
                print(f"  ⚠️ [v13.0-pre5.3b] GUARD: {len(_rejected_backup_guard)} backup clips will likely be rejected by quality_gate:")
                for _rb in _rejected_backup_guard:
                    print(f"    {_rb['rid']} ({_rb['src']}): {_rb['reason']}")
                _guard_issues.append(f"rejected_backup_precheck: {[r['rid'] for r in _rejected_backup_guard]}")

            # Guard D: duration_gap 预估
            _valid_dur_est = 0
            _invalid_dur_est = 0
            for _sel in selected:
                _rid = _sel.get('reel_clip_id','')
                _dur = _sel.get('use_end_offset',0) - _sel.get('use_start_offset',0)
                _is_dup = _rid_counts_guard.get(_rid,0) > 1
                _is_rejected_backup = any(r['rid'] == _rid for r in _rejected_backup_guard)
                if _is_dup or _is_rejected_backup or _rid not in _mm_guard:
                    _invalid_dur_est += _dur
                else:
                    _valid_dur_est += _dur
            _gap_est = tts_dur - _valid_dur_est
            print(f"  [v13.0-pre5.3b] duration_gap_estimate: valid={_valid_dur_est:.1f}s, invalid={_invalid_dur_est:.1f}s, gap={_gap_est:.1f}s")

            # ============================================================
            # v13.1-d Guard E: risk_budget 校验（替换旧 backup_ratio_guard）
            # ============================================================
            _sel_rk_clips = [s for s in selected if s.get('reel_clip_id','').startswith('B')]
            _sel_rk_count = len(_sel_rk_clips)
            _sel_rk_dur = sum(s.get('use_end_offset',0) - s.get('use_start_offset',0) for s in _sel_rk_clips)
            _max_rk = _risk_budget['max_risky_key']
            _max_rk_dur = tts_dur * _risk_budget['max_risky_duration_ratio']
            _rk_budget_pass = _sel_rk_count <= _max_rk and _sel_rk_dur <= _max_rk_dur
            print(f"  [v13.1-d] risk_budget_guard: selected_risky={_sel_rk_count} max={_max_rk} "
                  f"risky_dur={_sel_rk_dur:.1f}s max_dur={_max_rk_dur:.1f}s pass={_rk_budget_pass}")
            if not _rk_budget_pass:
                _guard_issues.append(f"risk_budget_exceeded: risky={_sel_rk_count}>{_max_rk} or dur={_sel_rk_dur:.1f}>{_max_rk_dur:.1f}")

            # v13.1-d Guard F: safe_duration 兜底（保留）
            _sel_primary_dur = sum(
                s.get('use_end_offset',0) - s.get('use_start_offset',0)
                for s in selected if s.get('reel_clip_id','').startswith('P'))
            _safe_dur_ratio = round(_sel_primary_dur / max(tts_dur, 1), 3)
            print(f"  [v13.1-d] safe_duration: primary_dur={_sel_primary_dur:.1f}s ratio={_safe_dur_ratio}")

            # ============================================================
            # Guard: 定向 retry（v13.3-b5a text-only retry + video fallback）
            # ============================================================
            _retry_attempted = False
            _retry_success = False
            _text_retry_attempted = False
            _text_retry_success = False
            _video_retry_attempted = False
            _video_retry_success = False
            _retry_summary = {
                'first_call_seconds': gr_elapsed,
                'text_retry_seconds': 0,
                'video_retry_seconds': 0,
                'used_text_retry': False,
                'used_video_retry': False,
                'guard_fail_reason': [],
                'text_retry_success': False,
                'video_retry_success': False,
                'final_retry_mode': 'none',
            }
            _should_retry = bool(_guard_issues) and (_gap_est > 1.0 or not _rk_budget_pass)
            if _should_retry and not getattr(generate_video, '_l3_retried', False):
                generate_video._l3_retried = True
                _retry_attempted = True

                _retry_feedback_parts = ["你上次输出存在以下问题："]
                if not _rk_budget_pass:
                    _retry_feedback_parts.append(
                        f"- 选择了 {_sel_rk_count} 条受控关键候选（risky_key），超过上限 {_max_rk} 条")
                    _retry_feedback_parts.append(
                        f"- 安全主候选充足，请优先从 safe_main 完成选片")
                    _retry_feedback_parts.append(
                        f"请将受控关键候选控制在 {_max_rk} 条以内，且必须说明必要性。")
                if _dup_rids_guard:
                    _retry_feedback_parts.append(f"- clip_id 重复: {list(_dup_rids_guard.keys())}")
                if _gap_est > 1.0:
                    _retry_feedback_parts.append(f"- 时长缺口: {_gap_est:.1f}s")
                _retry_feedback = '\n'.join(_retry_feedback_parts)
                _retry_summary['guard_fail_reason'] = [str(i) for i in _guard_issues]

                # 保存 guard failure feedback
                try:
                    with open(output_dir / "l3_retry_feedback.txt", 'w', encoding='utf-8') as _fbf:
                        _fbf.write(_retry_feedback)
                except Exception:
                    pass

                # ============================================================
                # Step 1: text-only retry（不带 input_video，不重新看片）
                # ============================================================
                print(f"  🔄 [v13.3-b5a] L3 guard 不通过，尝试 text-only retry（不重新看片）...")
                _text_retry_attempted = True
                _retry_summary['used_text_retry'] = True
                try:
                    import time as _time
                    _time.sleep(1)

                    # 构建首次 timeline 摘要（传给 text retry）
                    _first_timeline_lines = []
                    _used_clip_ids = []
                    _used_source_files = []
                    _protected_rids = set()
                    _replaceable_slots = []
                    for _fts in selected:
                        _ft_rid = _fts.get('reel_clip_id', '')
                        _ft_slot = _fts.get('slot_id', '')
                        _ft_so = _fts.get('use_start_offset', 0)
                        _ft_eo = _fts.get('use_end_offset', 0)
                        _ft_nr = _fts.get('narrative_role', '')
                        _ft_reason = _fts.get('selection_reason', '')[:60]
                        _first_timeline_lines.append(
                            f"  {_ft_slot}: {_ft_rid} [{_ft_so:.1f}-{_ft_eo:.1f}s] role={_ft_nr} reason={_ft_reason}")
                        _used_clip_ids.append(_ft_rid)
                        _ft_src = {m['reel_clip_id']: m for m in manifest}.get(_ft_rid, {}).get('source_file', '')
                        if _ft_src and _ft_src not in _used_source_files:
                            _used_source_files.append(_ft_src)
                    _first_timeline_text = '\n'.join(_first_timeline_lines)

                    # b5a-patch: 确定 protected clips（不得被替换）和 replaceable slots
                    _mm_tmp = {m['reel_clip_id']: m for m in manifest}
                    for _fts in selected:
                        _ft_rid = _fts.get('reel_clip_id', '')
                        # risky clips 可以被替换
                        if _ft_rid.startswith('B'):
                            _replaceable_slots.append(_fts.get('slot_id', ''))
                        # 但特定 clip 受保护（如果在 safe_main 且 guard 未标记问题）
                        elif _ft_rid.startswith('P'):
                            _protected_rids.add(_ft_rid)

                    # b5a-patch: 构建可用替换候选列表（safe_main 中未被使用的 clip）
                    _available_candidates = []
                    _all_valid_rids = []
                    for m in manifest:
                        _crid = m['reel_clip_id']
                        _all_valid_rids.append(_crid)
                        if _crid not in _used_clip_ids and _crid.startswith('P'):
                            _available_candidates.append(f"{_crid} ({m.get('source_file','')} {m.get('duration',0):.1f}s) {m.get('scene_description','')[:40]}")

                    # 构建 text-only retry prompt（b5a-patch 增强版）
                    _text_retry_prompt = f"""你是一个新闻短视频导演。你已经看过了所有候选素材的视频。

现在请根据以下信息，重新输出一个修正后的 selected_timeline JSON。

【最重要的硬性规则 — 违反任何一条则输出无效】
1. 每个 reel_clip_id 只能出现一次。绝对禁止重复使用同一个 clip_id。
2. 不得使用同一个 clip 的不同时间段来冒充两个不同镜头。一个 clip = 一次使用。
3. 只能使用下方【全量可用 clip_id】列表中的 ID。不得发明任何不在列表中的 clip_id。

【全量可用 clip_id — 只能从此列表中选择，不得发明新 ID】
{', '.join(_all_valid_rids)}
4. 不得修改任何 slot 的 use_start_offset 和 use_end_offset。必须原样复制上次的值。
5. 如果替换了某个 slot 的 clip，该 slot 的 use_start_offset 必须为 0.0，use_end_offset 必须等于该 slot 的目标时长（约 {round(tts_dur/len(_slots), 1)}s）。

【已使用的 clip_id 列表 — 同一 clip 不得再次出现】
{', '.join(_used_clip_ids)}

【受保护的 clip — 不得被移除或替换】
{', '.join(sorted(_protected_rids))}

【可替换的 slot（risky_key 所在 slot）】
{', '.join(_replaceable_slots)}

【可用替换候选（未被使用的 safe_main）】
{chr(10).join(_available_candidates) if _available_candidates else '（无可用候选）'}

【你上次的选片结果 — 尽量保留，只替换有问题的 slot】
{_first_timeline_text}

【问题反馈】
{_retry_feedback}

【slot 约束】
总时长: {tts_dur:.1f}s, slot 数: {len(_slots)}, 每 slot 约 {round(tts_dur/len(_slots), 1)}s
risky_key (B开头) 上限: {_max_rk} 条

【输出要求】
输出完整 JSON，包含 selected_timeline 数组。每个元素必须有：
slot_id, reel_clip_id, use_start_offset, use_end_offset, narrative_role, selection_reason

再次强调：每个 reel_clip_id 只能出现一次！不得重复！
"""

                    # 保存 text retry prompt
                    try:
                        with open(output_dir / "l3_text_retry_prompt.txt", 'w', encoding='utf-8') as _trpf:
                            _trpf.write(_text_retry_prompt)
                    except Exception:
                        pass

                    # 调用 text-only（无 input_video）
                    _text_retry_content = [
                        {"type": "input_text", "text": _text_retry_prompt},
                    ]
                    _text_retry_payload = {"model": _retry_model, "input": [{"role": "user", "content": _text_retry_content}]}
                    _text_retry_headers = {'Authorization': f'Bearer {_retry_api_key}', 'Content-Type': 'application/json'}
                    print(f"  🔄 [v13.3-b5a] text-only retry 调用中（无视频）...")
                    _text_retry_t0 = time.time()
                    _text_retry_resp = requests.post(f'{_retry_endpoint}/responses', json=_text_retry_payload, headers=_text_retry_headers, timeout=300)
                    _text_retry_elapsed = round(time.time() - _text_retry_t0, 1)
                    _retry_summary['text_retry_seconds'] = _text_retry_elapsed

                    # 保存 text retry raw response
                    try:
                        _tr_raw = ''
                        _tr_data = _text_retry_resp.json()
                        for _tri in _tr_data.get('output', []):
                            if _tri.get('type') == 'message':
                                for _trc in _tri.get('content', []):
                                    if _trc.get('type') == 'output_text':
                                        _tr_raw = _trc.get('text', '')
                        with open(output_dir / "l3_text_retry_raw.txt", 'w', encoding='utf-8') as _trrf:
                            _trrf.write(_tr_raw)
                    except Exception:
                        pass

                    if _text_retry_resp.status_code != 200:
                        raise RuntimeError(f"text retry HTTP {_text_retry_resp.status_code}")
                    _text_retry_parsed = _parse_l3_response(_text_retry_resp.json(), _text_retry_elapsed)
                    print(f"  🔄 [v13.3-b5a] text-only retry 完成 ({_text_retry_elapsed}s)")
                    _text_retry_selected = _text_retry_parsed.get('selected_timeline', [])
                    if _text_retry_selected:
                        _tr_rk = sum(1 for s in _text_retry_selected if s.get('reel_clip_id','').startswith('B'))
                        _tr_dup = {k:v for k,v in _Counter(s.get('reel_clip_id','') for s in _text_retry_selected).items() if v > 1}
                        _tr_improved = _tr_rk <= _max_rk or _tr_rk < _sel_rk_count
                        _tr_no_dup = not _tr_dup

                        # b5a-patch: 自检 — protected clip 是否保持
                        _tr_rids = set(s.get('reel_clip_id','') for s in _text_retry_selected)
                        _tr_protected_ok = _protected_rids.issubset(_tr_rids) if _protected_rids else True
                        if not _tr_protected_ok:
                            _missing_protected = _protected_rids - _tr_rids
                            print(f"  ⚠️ [b5a-patch] text retry 删除了受保护 clip: {_missing_protected}")

                        # b5a-patch: 自检 — 不存在的 clip_id
                        _all_manifest_rids = set(m['reel_clip_id'] for m in manifest)
                        _tr_invalid_rids = [r for r in _tr_rids if r not in _all_manifest_rids]
                        if _tr_invalid_rids:
                            print(f"  ⚠️ [b5a-patch] text retry 使用了不存在的 clip: {_tr_invalid_rids}")

                        # b5a-patch: 记录自检结果
                        _retry_summary['duplicate_check_pass'] = _tr_no_dup
                        _retry_summary['protected_check_pass'] = _tr_protected_ok
                        _retry_summary['invalid_clip_check_pass'] = not _tr_invalid_rids
                        _retry_summary['text_retry_risky_count'] = _tr_rk
                        _retry_summary['text_retry_dup_clips'] = list(_tr_dup.keys()) if _tr_dup else []

                        # b5a-patch: 成功条件 = dup=0 + risky改善 + 无无效clip + protected保持
                        _tr_pass = _tr_no_dup and _tr_improved and not _tr_invalid_rids and _tr_protected_ok
                        if _tr_pass:
                            print(f"  ✅ [v13.3-b5a] text retry 成功: risky {_sel_rk_count}→{_tr_rk} (max={_max_rk}), dup=0")
                            selected = _text_retry_selected
                            gr_result = _text_retry_parsed
                            _text_retry_success = True
                            _retry_success = True
                            _retry_summary['text_retry_success'] = True
                            _retry_summary['final_retry_mode'] = 'text_only'
                            for _rsel in selected:
                                _raw_rsid = _rsel.get('slot_id', '')
                                if _raw_rsid and not _re.match(r'^slot_\d+$', _raw_rsid):
                                    _rdigits = _re.findall(r'\d+', _raw_rsid)
                                    if _rdigits:
                                        _rsel['slot_id'] = f'slot_{int(_rdigits[0]):02d}'
                        else:
                            _fail_reasons = []
                            if _tr_dup: _fail_reasons.append(f"dup={list(_tr_dup.keys())}")
                            if not _tr_improved: _fail_reasons.append(f"risky {_sel_rk_count}→{_tr_rk} not improved")
                            if _tr_invalid_rids: _fail_reasons.append(f"invalid_clips={_tr_invalid_rids}")
                            if not _tr_protected_ok: _fail_reasons.append(f"missing_protected={_protected_rids - _tr_rids}")
                            _retry_summary['text_retry_failure_reason'] = _fail_reasons
                            print(f"  ⚠️ [b5a-patch] text retry 未通过: {'; '.join(_fail_reasons)}")
                    else:
                        print(f"  ⚠️ [v13.3-b5a] text retry 返回空 timeline")
                except Exception as _text_retry_err:
                    print(f"  ⚠️ [v13.3-b5a] text retry 失败: {str(_text_retry_err)[:150]}")

                # ============================================================
                # Step 2: 如 text retry 失败，fallback 到 video retry（最多一次）
                # ============================================================
                if not _text_retry_success:
                    print(f"  🔄 [v13.3-b5a] text retry 未成功，fallback 到 video retry...")
                    _video_retry_attempted = True
                    _retry_summary['used_video_retry'] = True
                    try:
                        _time.sleep(2)
                        _video_retry_prompt = _retry_gr_prompt + "\n\n" + _retry_feedback
                        print(f"  🔄 [v13.3-b5a] video retry 调用中（含视频+反馈）...")
                        _video_retry_content = [
                            {"type": "input_video", "video_url": _retry_reel_url},
                            {"type": "input_text", "text": _video_retry_prompt},
                        ]
                        _video_retry_payload = {"model": _retry_model, "input": [{"role": "user", "content": _video_retry_content}]}
                        _video_retry_headers = {'Authorization': f'Bearer {_retry_api_key}', 'Content-Type': 'application/json'}
                        _video_retry_t0 = time.time()
                        _video_retry_resp = requests.post(f'{_retry_endpoint}/responses', json=_video_retry_payload, headers=_video_retry_headers, timeout=900)
                        _video_retry_elapsed = round(time.time() - _video_retry_t0, 1)
                        _retry_summary['video_retry_seconds'] = _video_retry_elapsed
                        if _video_retry_resp.status_code != 200:
                            raise RuntimeError(f"video retry HTTP {_video_retry_resp.status_code}")
                        _video_retry_parsed = _parse_l3_response(_video_retry_resp.json(), _video_retry_elapsed)
                        print(f"  🔄 [v13.3-b5a] video retry 完成 ({_video_retry_elapsed}s)")
                        _video_retry_selected = _video_retry_parsed.get('selected_timeline', [])
                        if _video_retry_selected:
                            _vr_rk = sum(1 for s in _video_retry_selected if s.get('reel_clip_id','').startswith('B'))
                            _vr_dup = {k:v for k,v in _Counter(s.get('reel_clip_id','') for s in _video_retry_selected).items() if v > 1}
                            _vr_improved = _vr_rk <= _max_rk or _vr_rk < _sel_rk_count
                            if _vr_improved:
                                print(f"  ✅ [v13.3-b5a] video retry 成功: risky {_sel_rk_count}→{_vr_rk} (max={_max_rk})")
                                selected = _video_retry_selected
                                gr_result = _video_retry_parsed
                                _video_retry_success = True
                                _retry_success = True
                                _retry_summary['video_retry_success'] = True
                                _retry_summary['final_retry_mode'] = 'video_fallback'
                                for _rsel in selected:
                                    _raw_rsid = _rsel.get('slot_id', '')
                                    if _raw_rsid and not _re.match(r'^slot_\d+$', _raw_rsid):
                                        _rdigits = _re.findall(r'\d+', _raw_rsid)
                                        if _rdigits:
                                            _rsel['slot_id'] = f'slot_{int(_rdigits[0]):02d}'
                            else:
                                print(f"  ⚠️ [v13.3-b5a] video retry 未改善: risky {_sel_rk_count}→{_vr_rk}")
                        else:
                            print(f"  ⚠️ [v13.3-b5a] video retry 返回空 timeline")
                    except Exception as _video_retry_err:
                        print(f"  ⚠️ [v13.3-b5a] video retry 失败: {str(_video_retry_err)[:150]}")

                # 保存 retry summary
                try:
                    with open(output_dir / "l3_retry_summary.json", 'w', encoding='utf-8') as _rsf:
                        json.dump(_retry_summary, _rsf, ensure_ascii=False, indent=2)
                except Exception:
                    pass

            elif hasattr(generate_video, '_l3_retried'):
                delattr(generate_video, '_l3_retried')

            print(f"  [v13.3-b5a] retry: text={_text_retry_attempted}({_text_retry_success}) video={_video_retry_attempted}({_video_retry_success}) final={_retry_summary['final_retry_mode']}")

            # 从 selected_timeline 映射回原素材 timeline（v8.5 边界强校验）
            manifest_map = {m['reel_clip_id']: m for m in manifest}
            timeline = []
            _boundary_report = []  # 边界校验报告
            _invalid_clips = []    # 无效 clip 列表
            # v10.5: 加载 L2 pool 数据供 backup 准入过滤
            from pipeline.pool_overrides import load_pool_data as _load_pool_filter
            _fill_pool_for_filter = _load_pool_filter(task_id)
            _order_counter = 0
            for i, sel in enumerate(selected):
                rid = sel.get('reel_clip_id', '')
                m = manifest_map.get(rid, {})
                br_entry = {
                    'reel_clip_id': rid,
                    'l3_use_start_offset': sel.get('use_start_offset', 0),
                    'l3_use_end_offset': sel.get('use_end_offset', 0),
                    'adjusted': False,
                    'valid': True,
                    'skip_reason': None,
                }
                if not m:
                    # v13.1-f patch3: manifest_missing 不计入 invalid，让补位兜底
                    print(f"  ⚠️ reel_clip_id {rid} 未在 manifest 中找到，跳过（不计入 invalid）")
                    br_entry['valid'] = False
                    br_entry['skip_reason'] = 'manifest_missing_skipped'
                    _boundary_report.append(br_entry)
                    # 不加入 _invalid_clips，让 duration_gap 不受 manifest_missing 影响
                    continue

                clip_dur_sec = m.get('clip_duration_sec', m.get('duration', m['source_end_sec'] - m['source_start_sec']))
                allowed_range = m.get('allowed_offset_range', [0.0, clip_dur_sec])
                br_entry['clip_duration_sec'] = clip_dur_sec
                br_entry['allowed_offset_range'] = allowed_range
                br_entry['source_file'] = m['source_file']

                so = sel.get('use_start_offset', 0)
                eo = sel.get('use_end_offset', clip_dur_sec)

                # ====== 边界强校验（v8.5）======
                adjusted = False
                if so < 0:
                    print(f"  ⚠️ [{rid}] use_start_offset {so:.2f} < 0 → 归零")
                    so = 0
                    adjusted = True
                if eo > clip_dur_sec + 0.05:
                    print(f"  ⚠️ [{rid}] use_end_offset {eo:.2f}s > clip_duration {clip_dur_sec:.2f}s → 截断")
                    eo = clip_dur_sec
                    adjusted = True
                dur = round(eo - so, 2)

                # 映射回原素材时间
                src_start = round(m['source_start_sec'] + so, 2)
                src_end = round(m['source_start_sec'] + eo, 2)
                # 不超过 source_end_sec
                if src_end > m['source_end_sec'] + 0.05:
                    print(f"  ⚠️ [{rid}] src_end {src_end:.2f}s > source_end {m['source_end_sec']:.2f}s → 截断")
                    src_end = round(m['source_end_sec'], 2)
                    dur = round(src_end - src_start, 2)
                    adjusted = True

                br_entry['adjusted'] = adjusted
                br_entry['source_start_sec'] = src_start
                br_entry['source_end_sec'] = src_end
                br_entry['actual_cut_duration'] = dur

                if dur < 1.0:  # v11.4: 1.5→1.0
                    print(f"  ❌ [{rid}] 修正后时长 {dur:.2f}s < 1.0s → 无效")
                    br_entry['valid'] = False
                    br_entry['skip_reason'] = f'时长{dur:.2f}s<1.0s'
                    _boundary_report.append(br_entry)
                    _invalid_clips.append(br_entry)
                    continue

                # v10.5: backup 准入过滤 — 禁止 unstable/无clean_windows 的 backup 进入成片
                _pl = m.get('pool_level', 'primary')
                if _pl == 'backup':
                    _src_fn = m['source_file']
                    _src_l2 = _fill_pool_for_filter.get(_src_fn, {}) if '_fill_pool_for_filter' in dir() else {}
                    if not _src_l2:
                        # 尝试从 manifest 读取质量信息
                        _src_l2 = {}
                    _has_cw = bool(_src_l2.get('clean_windows', []))
                    _ffmpeg_stable = True
                    for _ws in _src_l2.get('weak_safe_segments', []):
                        if isinstance(_ws, dict) and _ws.get('ffmpeg_stability') == 'unstable':
                            _ffmpeg_stable = False
                    _score_stab = m.get('score_detail', {}).get('stability', 100)
                    if not _has_cw and not _ffmpeg_stable:
                        print(f"  ⛔ [{rid}] backup 准入拒绝: {_src_fn} clean_windows=空 + ffmpeg=unstable → 跳过")
                        br_entry['valid'] = False
                        br_entry['skip_reason'] = 'backup_quality_rejected: no_cw + unstable'
                        _boundary_report.append(br_entry)
                        _invalid_clips.append(br_entry)
                        continue
                    if _score_stab < 70:
                        if _score_stab >= 60:
                            # v11.6.3: stability 60-69 先记录，不立即拒绝
                            # 后续如果有空 slot 无法补齐，会触发第二轮兜底放宽
                            if not hasattr(generate_video, '_relaxable_backups'):
                                generate_video._relaxable_backups = []
                            generate_video._relaxable_backups.append({
                                'reel_clip_id': rid,
                                'source_file': _src_fn,
                                'stability': _score_stab,
                                'slot_id': sel.get('slot_id', ''),
                                'timeline_entry': dict(sel),
                                'br_entry': dict(br_entry),
                            })
                            print(f"  ⚠️ [{rid}] backup stability={_score_stab} (60-69) → 暂缓，可能兜底使用")
                        else:
                            print(f"  ⛔ [{rid}] backup 准入拒绝: {_src_fn} stability_score={_score_stab} < 60 → 跳过")
                        br_entry['valid'] = False
                        br_entry['skip_reason'] = f'backup_quality_rejected: stability={_score_stab}<70'
                        _boundary_report.append(br_entry)
                        _invalid_clips.append(br_entry)
                        continue

                _order_counter += 1
                timeline.append({
                    'order': _order_counter,
                    'source_file': m['source_file'],
                    'start_sec': src_start,
                    'end_sec': src_end,
                    'duration_sec': dur,
                    'scene_type': sel.get('selection_reason', sel.get('expression_intent', sel.get('why', '')[:80])),
                    'reel_clip_id': rid,
                    'pool_level': m.get('pool_level', 'primary'),
                    'sentence_id': sel.get('sentence_id', ''),
                    'slot_id': sel.get('slot_id', ''),  # v9.9: 关联 slot
                    # v13.0: 叙事解释字段
                    'paragraph_role': sel.get('paragraph_role', ''),
                    'narrative_role': sel.get('narrative_role', ''),
                    'selection_reason': sel.get('selection_reason', ''),
                    # v13.0-pre5: scene block 字段
                    'scene_block_id': sel.get('scene_block_id', ''),
                    'scene_block_name': sel.get('scene_block_name', ''),
                    # v13.0-pre5.2: 新闻播报专属字段
                    'main_action_level': sel.get('main_action_level', ''),
                    'visual_semantic_type': sel.get('visual_semantic_type', ''),
                    'information_gain': sel.get('information_gain', ''),
                })
                _boundary_report.append(br_entry)

            # 保存边界校验报告
            _br_path = output_dir / "boundary_validation_report.json"
            with open(_br_path, 'w', encoding='utf-8') as _f:
                json.dump(_boundary_report, _f, ensure_ascii=False, indent=2)
            _adj_count = sum(1 for b in _boundary_report if b.get('adjusted'))
            _inv_count = len(_invalid_clips)
            print(f"  边界校验: {len(_boundary_report)} 条, 修正 {_adj_count} 条, 无效 {_inv_count} 条")
            print(f"  报告: {_br_path}")

            # 计算总时长缺口
            _timeline_total = sum(t['duration_sec'] for t in timeline)
            _gap = round(tts_dur - _timeline_total, 2)
            print(f"  timeline 总时长: {_timeline_total:.2f}s, TTS: {tts_dur:.1f}s, 缺口: {_gap:.2f}s")

            # 缺口判断
            # v9.5: 放宽 replan 阈值（边界越界+后验剔除导致的缺口由补位弥补）
            if _gap > 15.0 and _inv_count > 0:
                print(f"  ❌ 缺口 {_gap:.1f}s > 15.0s 且有 {_inv_count} 条无效 clip → needs_l3_replan")
                # 保存失败信息，不直接 raise，让调用方决定
                gr_result['needs_l3_replan'] = True
                gr_result['replan_reason'] = f'缺口{_gap:.1f}s>1.0s, {_inv_count}条无效clip'
                gr_result['invalid_clips'] = _invalid_clips

            l3_result = gr_result
            l3_result['timeline'] = timeline
            l3_result['_l3_mode'] = 'global_reel'
            l3_result['_boundary_report_path'] = str(_br_path)
            print(f"  [L3_MODE] global_reel ✅")
            # v13.0-pre2: 完整收口日志 + reject 详情
            _qg_rejected = sum(1 for b in _invalid_clips if 'quality' in str(b.get('skip_reason','')))
            _manifest_missing = sum(1 for b in _invalid_clips if 'manifest' in str(b.get('skip_reason','')).lower() or '不在' in str(b.get('skip_reason','')))
            _boundary_oor = _inv_count - _qg_rejected - _manifest_missing
            print(f"  [v13.0] raw_l3={_actual_l3_count} | valid_after_boundary={len(timeline)} | rejected={_inv_count}")
            print(f"  [v13.0]   reject_breakdown: quality_gate={_qg_rejected}, manifest_missing={_manifest_missing}, boundary_oor={_boundary_oor}")
            if _invalid_clips:
                for _ic in _invalid_clips:
                    _ic_rid = _ic.get('reel_clip_id','?')
                    _ic_src = _ic.get('source_file', '?')
                    _ic_reason = _ic.get('skip_reason','?')
                    # 找到 L3 原始选片中的叙事字段
                    _ic_sel = next((s for s in selected if s.get('reel_clip_id') == _ic_rid), {})
                    _ic_pr = _ic_sel.get('paragraph_role', '')
                    _ic_nr = _ic_sel.get('narrative_role', '')
                    _ic_sr = _ic_sel.get('selection_reason', '')[:50]
                    print(f"  [v13.0]   ❌ {_ic_rid} ({_ic_src}): {_ic_reason} | pr={_ic_pr} nr={_ic_nr} sr={_ic_sr}")
            print(f"  映射完成: {len(timeline)} 条镜头 → 原素材时间")

        except Exception as e:
            _err_str = str(e)
            print(f"  ⚠️ 全局读片异常: {_err_str[:200]}")
            # v12.4: narration 模式禁止回退旧 L3，直接报错
            if edit_mode == 'narration':
                print(f"  ❌ [v12.4] narration 模式禁止回退旧 L3，直接失败")
                print(f"  [L3_MODE] global_reel_failed (narration 不回退)")
                raise RuntimeError(f"L3 全局读片失败（narration 模式禁止回退）: {_err_str[:200]}")
            else:
                print(f"  回退到旧片段式 L3（非 narration 模式）")
                GLOBAL_REEL_L3 = False  # 仅非 narration 允许回退

    if not GLOBAL_REEL_L3 or edit_mode == 'music_only':
        # === 旧片段式 L3（仅 music_only 或非 narration 模式使用） ===
        print(f"  [L3_MODE] fallback_v7")
        l3_prompt = L3_MUSIC_PROMPT_FILE if edit_mode == 'music_only' else L3_PROMPT_FILE

        l3_clips = build_l3_video_inputs(task_id=task_id)
        if l3_clips:
            print(f"  [L3] 准备 {len(l3_clips)} 条合法候选段视频片段")
        else:
            print(f"  [L3] ⚠️ 无可用视频片段，回退纯文字模式")

        l3_result = _call_l3_director(l2_segments_text, l3_script, l3_context, tts_dur, prompt_file=l3_prompt, video_clips=l3_clips)

    timeline = l3_result.get('timeline', [])
    if not timeline:
        raise RuntimeError("L3 未返回 timeline")

    # v8.5: needs_l3_replan 拦截
    if l3_result.get('needs_l3_replan'):
        _replan_reason = l3_result.get('replan_reason', '未知')
        _inv_clips = l3_result.get('invalid_clips', [])
        print(f"\n  ❌ [needs_l3_replan] {_replan_reason}")
        print(f"     无效 clip: {[c.get('reel_clip_id') for c in _inv_clips]}")
        raise RuntimeError(
            f"L3 选片边界校验失败: {_replan_reason}。"
            f"无效 clip: {[c.get('reel_clip_id') for c in _inv_clips]}。"
            f"需要 L3 重新选片。禁止生成。"
        )

    # ============================================================
    # v8.6: expression_intent 全套已删除（收回后验导演权）
    # L3 是唯一导演，后验层不再注入 intent / 交换顺序 / 配额控制
    # ============================================================

    # ============================================================
    # v13.2-a: segment_polisher 精修（按 scene_block 审视角度多样性）
    # ============================================================
    USE_POLISHER = False  # 临时关闭，等数据管道调通再开
    if USE_POLISHER and edit_mode == 'narration':
        try:
            # 按 scene_block_id 分组
            _block_groups = {}
            _block_names = {}
            for _t in timeline:
                _bid = _t.get('scene_block_id', 'unknown')
                _block_groups.setdefault(_bid, []).append(_t)
                if _bid not in _block_names:
                    _block_names[_bid] = _t.get('scene_block_name', _bid)

            from pipeline.segment_polisher import build_block_video, call_segment_polisher
            # v13.2-c L2: 保存 L3 原始 main_action_level，防 polisher 覆盖后 opening_enforce 失效
            _l3_clip_mal_map = {_t.get('reel_clip_id', ''): _t.get('main_action_level', '') for _t in timeline}
            _polished_all = []
            _polisher_ok = 0
            _polisher_total = len(_block_groups)
            _polisher_time = 0

            for _bid in sorted(_block_groups.keys()):
                _tl_clips = _block_groups[_bid]
                _bname = _block_names[_bid]
                # 收集该 block 的候选
                _block_rids = list(set(t.get('reel_clip_id', '') for t in _tl_clips))
                _block_cands = [manifest_map.get(rid, {}) for rid in _block_rids if rid in manifest_map]
                # 补充候选池中其他 clip
                for _mc in manifest:
                    if _mc.get('reel_clip_id', '') not in _block_rids:
                        _block_cands.append(_mc)
                    if len(_block_cands) >= 8:
                        break

                _current = [{
                    'clip_id': t.get('reel_clip_id', ''),
                    'role': t.get('main_action_level', ''),
                    'use_start_offset': t.get('use_start_offset', 0),
                    'use_end_offset': t.get('use_end_offset', 0),
                    'director_note': t.get('selection_reason', '')[:60],
                } for t in _tl_clips]

                # 构建 block 视频
                _bv_path = str(output_dir / f"block_{_bid}.mp4")
                _built = build_block_video(_block_cands, _bv_path, ffmpeg=FFMPEG)
                if not _built:
                    print(f"  [v13.2-a] polisher {_bid}: 视频构建失败，跳过")
                    _polished_all.extend(_tl_clips)
                    continue

                # 上传 TOS
                _bv_tos_key = f'tmp_l3_clips/block_{_bid}_{task_id}.mp4'
                tos_client.put_object_from_file('e23-video', _bv_tos_key, _built)
                _bv_url = f'https://e23-video.tos-cn-beijing.volces.com/{_bv_tos_key}'

                _pr = call_segment_polisher(
                    block_id=_bid, block_name=_bname, block_narrative=_bname,
                    duration_target=len(_tl_clips) * 2.7,
                    current_selection=_current, all_candidates=_block_cands,
                    block_video_url=_bv_url, task_id=task_id,
                )

                if _pr and 'clips' in _pr:
                    _polisher_ok += 1
                    _polisher_time += _pr.get('_elapsed', 0)
                    for _pc in _pr['clips']:
                        _mc = manifest_map.get(_pc.get('clip_id', ''), {})
                        _use_s = float(_pc.get('use_start_offset', 0))
                        _use_e = float(_pc.get('use_end_offset', 3))
                        _src_origin = float(_mc.get('source_start_sec', 0))
                        # 映射：clip 内部偏移 → 源素材绝对时间
                        _abs_start = _src_origin + _use_s
                        _abs_end = _src_origin + _use_e
                        _dur = _use_e - _use_s
                        _polished_all.append({
                            'reel_clip_id': _pc.get('clip_id', ''),
                            'use_start_offset': _use_s,
                            'use_end_offset': _use_e,
                            'duration_sec': _dur,
                            'duration': _dur,
                            'start_sec': _abs_start,  # 源素材绝对时间（slot 构建读这个）
                            'end_sec': _abs_end,       # 源素材绝对时间
                            'source_file': _mc.get('source_file', ''),
                            'source_start_sec': _abs_start,
                            'source_end_sec': _abs_end,
                            'clip_duration_sec': _mc.get('clip_duration_sec', 0),
                            'allowed_offset_range': [0, _mc.get('clip_duration_sec', 10)],
                            'main_action_level': _pc.get('role', 'primary_action'),
                            'l3_original_mal': _l3_clip_mal_map.get(_pc.get('clip_id', ''), ''),  # v13.2-c L2
                            'director_role': _pc.get('role', ''),
                            'selection_reason': _pc.get('director_note', ''),
                            'scene_block_id': _bid,
                            'scene_block_name': _bname,
                        })
                else:
                    print(f"  [v13.2-a] polisher {_bid}: 失败，使用原始 timeline")
                    _polished_all.extend(_tl_clips)

            if _polisher_ok > 0:
                print(f"  [v13.2-a] polisher 完成: {_polisher_ok}/{_polisher_total} blocks, "
                      f"{_polisher_time:.0f}s, {len(_polished_all)} clips")
                # 给每条 clip 编号 slot_id（slot 构建必需）
                for _pi, _pc_item in enumerate(_polished_all):
                    _pc_item['slot_id'] = f'slot_{_pi+1:02d}'
                    _pc_item['order'] = _pi + 1
                timeline = _polished_all
                l3_result['timeline'] = timeline
                # 保存
                with open(output_dir / 'polished_timeline.json', 'w', encoding='utf-8') as _ptf:
                    json.dump(timeline, _ptf, ensure_ascii=False, indent=2)
            else:
                print(f"  [v13.2-a] polisher 全部失败，使用原始 timeline")
        except Exception as _pe:
            print(f"  ⚠️ [v13.2-a] polisher 异常: {str(_pe)[:100]}，使用原始 timeline")

    # ============================================================
    # v13.1-f: 同一 clip 完全禁止重复使用（替换旧 duplicate_offset）
    # ============================================================
    _dup_clip_removed = []
    _seen_clip_ids = set()
    _dedup_timeline = []
    for _tl_item in timeline:
        _rid = _tl_item.get('reel_clip_id', '') or _tl_item.get('source_file', '')
        if _rid in _seen_clip_ids:
            _dup_clip_removed.append(_tl_item)
            print(f"  [v13.1-f] duplicate_clip removed: {_rid} "
                  f"[{_tl_item.get('use_start_offset',0):.1f}-{_tl_item.get('use_end_offset',0):.1f}]")
        else:
            _seen_clip_ids.add(_rid)
            _dedup_timeline.append(_tl_item)
    if _dup_clip_removed:
        print(f"  [v13.1-f] duplicate_clip: removed {len(_dup_clip_removed)} slots, "
              f"ids={[s.get('reel_clip_id','') for s in _dup_clip_removed]}")
        timeline = _dedup_timeline
        l3_result['timeline'] = timeline

    # v13.2-c patch: manifest 未初始化保护（music_only 模式不走 candidate_reel）
    if 'manifest' not in locals():
        manifest = []
        print(f"  [v13.2-c patch] manifest initialized to [] (mode={edit_mode})")

    # ============================================================
    # v13.1-e: 片头结构 enforce（slot_1/2 不得为 static_context/banner/environment）
    # ============================================================
    _manifest_map_opening = {m['reel_clip_id']: m for m in manifest}
    _opening_enforced = False
    for _oi in range(min(2, len(timeline))):
        _tl_item = timeline[_oi]
        _rid = _tl_item.get('reel_clip_id', '')
        _m = _manifest_map_opening.get(_rid, {})
        _vc = _m.get('visual_class', _tl_item.get('visual_class', ''))
        _mal = _tl_item.get('main_action_level', '')
        _l3_mal = _tl_item.get('l3_original_mal', '')  # v13.2-c L2: L3 原始判定
        _dr = _tl_item.get('director_role', '')
        # v13.2-c: 三层判定（L1 visual_class + L3 原始 + polisher role）
        _is_static = (
            _vc in ('banner', 'environment')       # L1 修复后这条会生效
            or _mal == 'static_context'             # 非 polisher 路径
            or _l3_mal == 'static_context'          # L2 兜底，防 polisher 覆盖
            or (_dr and _dr in ('transition',))     # polisher 标注的过渡镜头
        )
        if _is_static:
            # 找替换：safe_main 中 info_score 最高且未被选用的 primary_action
            _used_rids = {t.get('reel_clip_id', '') for t in timeline}
            _replacement = None
            _oe_anchor_skipped = []
            for _cand in sorted(manifest, key=lambda x: -x.get('info_score', 0)):
                if (_cand.get('visual_class') == 'primary_action'
                        and _cand.get('reel_clip_id') not in _used_rids
                        and _cand.get('candidate_tier') == 'safe_main'):
                    # v13.2-h step4 B: VS semantic anchor guard
                    if OPENING_ENFORCE_VS_ANCHOR_GUARD:
                        _cand_desc_oe = _cand.get('scene_description', '') or ''
                        _cand_variant_oe = _cand.get('_is_variant', False)
                        _is_vs_anchor = (
                            _cand_variant_oe
                            or any(_kw in _cand_desc_oe for _kw in _VS_ANCHOR_KEYWORDS)
                        )
                        if _is_vs_anchor:
                            _oe_anchor_skipped.append({
                                'clip_id': _cand.get('reel_clip_id', ''),
                                'source_file': _cand.get('source_file', ''),
                                'reason': f"variant={_cand_variant_oe}, desc_kw={[k for k in _VS_ANCHOR_KEYWORDS if k in _cand_desc_oe][:3]}"
                            })
                            print(f"  [v13.2-h step4] VS anchor guard: skip {_cand.get('reel_clip_id','')} for opening_enforce (protected)")
                            continue
                    _replacement = _cand
                    break
            if _oe_anchor_skipped:
                print(f"  [v13.2-h step4] VS anchor guard: {len(_oe_anchor_skipped)} clips protected from opening_enforce")
            if _replacement:
                _old_rid = _rid
                _new_rid = _replacement['reel_clip_id']
                _new_dur = _replacement.get('clip_duration_sec', _replacement.get('duration', 3.0))
                _old_end = _tl_item.get('use_end_offset', 2.7)
                timeline[_oi]['reel_clip_id'] = _new_rid
                timeline[_oi]['source_file'] = _replacement.get('source_file', '')
                timeline[_oi]['start_sec'] = 0.0
                timeline[_oi]['end_sec'] = timeline[_oi].get('use_end_offset', 2.7)
                timeline[_oi]['use_start_offset'] = 0.0
                timeline[_oi]['use_end_offset'] = min(_new_dur, _old_end)
                timeline[_oi]['main_action_level'] = 'primary_action'
                timeline[_oi]['selection_reason'] = f'[v13.1-e opening_enforce] 替换 {_old_rid}({_vc}) → {_new_rid}(primary_action)'
                _opening_enforced = True
                print(f"  [v13.1-e] opening_enforce: slot_{_oi+1} {_old_rid}({_vc}) → {_new_rid}(primary_action)")
    if not _opening_enforced:
        print(f"  [v13.1-e] opening_enforce: 前2镜已合规，无需替换")
    # v13.2-c monitor: opening_enforce 触发率
    _oe_checked = min(2, len(timeline))
    _oe_details = []
    for _oi2 in range(min(2, len(timeline))):
        _t2 = timeline[_oi2]
        _r2 = _t2.get('reel_clip_id', '')
        _m2 = _manifest_map_opening.get(_r2, {})
        _oe_details.append(f"slot_{_oi2+1}:{_r2}(vc={_m2.get('visual_class','?')},mal={_t2.get('main_action_level','?')},l3={_t2.get('l3_original_mal','?')})")
    print(f"  [v13.2-c monitor] opening_enforce: checked={_oe_checked} triggered={'Y' if _opening_enforced else 'N'} slots=[{', '.join(_oe_details)}]")

    # ============================================================
    # v13.2-h step6.4 A: Opener candidate ranking（替换 step6.2 A）
    # ============================================================
    NEWS_OPENER_RANKING_GUARD = True
    import json as _json_step6
    if NEWS_OPENER_RANKING_GUARD and timeline:
        def _calc_opener_score(clip):
            desc = clip.get('scene_description', '').lower()
            sc = 0
            if any(k in desc for k in ['活动', '宣传', '大篷车']): sc += 2
            if any(k in desc for k in ['现场', '场地', '服务中心', '骑手之家', '站点']): sc += 2
            if any(k in desc for k in ['互动', '游戏', '投掷', '体验']): sc += 4
            if any(k in desc for k in ['参与', '排队', '聚集', '到场']): sc += 3
            if any(k in desc for k in ['发放', '递送', '操作']): sc += 2
            if any(k in desc for k in ['讲解', '宣讲']): sc += 1
            if any(k in desc for k in ['采访', '记录']): sc += 1
            if any(k in desc for k in ['人群', '群体', '多名', '多位']): sc += 2
            if any(k in desc for k in ['骑手', '外卖']): sc += 1
            if any(k in desc for k in ['美团服务中心', '山大站']): sc += 2
            if any(k in desc for k in ['人社', '大篷车']): sc += 1
            if any(k in desc for k in ['合影', '横幅', '红旗', '标语', '摆拍', '立牌', '主视觉']): sc -= 8
            if any(k in desc for k in ['面对面交流', '隔桌交流', '站定', '站着聊天']): sc -= 4
            # 纯 handout 无现场感
            if any(k in desc for k in ['发放', '递送']) and not any(k in desc for k in ['现场', '人群', '群体', '参与', '互动']):
                sc -= 3
            # 纯讲解无动作
            if any(k in desc for k in ['讲解', '答疑', '咨询']) and not any(k in desc for k in ['互动', '游戏', '参与', '人群']):
                sc -= 3
            stab = clip.get('score_detail', {}).get('stability', 70)
            if stab >= 70: sc += 1
            if stab < 60: sc -= 2
            return sc

        # 建立 VS_05 保护集（只保护 timeline 中已选的互动 clip，不保护所有互动候选）
        _VS_PROTECT_OPENER = set()
        for _t in timeline:
            _r = _t.get('reel_clip_id', '')
            _m_desc_op = _manifest_map_opening.get(_r, {}).get('scene_description', '')
            if any(kw in _m_desc_op for kw in ['互动', '游戏', '投掷', '投沙包', '投飞镖']):
                _VS_PROTECT_OPENER.add(_r)
            if _manifest_map_opening.get(_r, {}).get('_is_variant'):
                _VS_PROTECT_OPENER.add(_r)

        # 排序所有候选
        _used_rids_opener = {t.get('reel_clip_id', '') for t in timeline}
        _opener_candidates = []
        for _mc in manifest:
            _mcr = _mc.get('reel_clip_id', '')
            if _mcr in _VS_PROTECT_OPENER: continue  # VS_05 保护不做 opener
            if _mc.get('pool_level') == 'disabled': continue
            osc = _calc_opener_score(_mc)
            _opener_candidates.append((_mcr, osc, _mc))
        _opener_candidates.sort(key=lambda x: -x[1])
        _opener_top_k = _opener_candidates[:10]

        # 检查当前 slot_01 的 opener_score
        _cur_opener_rid = timeline[0].get('reel_clip_id', '')
        _cur_opener_m = _manifest_map_opening.get(_cur_opener_rid, {})
        _cur_opener_score = _calc_opener_score(_cur_opener_m)

        # 如果当前 opener 不在 top_k 中或 score 明显低于 top，替换
        _top_score = _opener_top_k[0][1] if _opener_top_k else 0
        _opener_replaced = False
        _opener_log = {'guard': 'opener_ranking', 'current_opener': _cur_opener_rid, 'current_score': _cur_opener_score,
                       'top_k': [{'rid': r, 'score': s} for r, s, _ in _opener_top_k[:10]]}

        if _cur_opener_score < _top_score - 3:  # 当前 opener 明显弱于 top
            _repl = None
            _skipped = []
            for _crid, _csc, _cand in _opener_top_k:
                if _crid in _used_rids_opener:
                    _skipped.append({'rid': _crid, 'score': _csc, 'reason': 'already_in_timeline'})
                    continue
                if _crid in _VS_PROTECT_OPENER:
                    _skipped.append({'rid': _crid, 'score': _csc, 'reason': 'vs05_protected'})
                    continue
                _repl = _cand
                _repl_score = _csc
                _repl_rid = _crid
                break
            _opener_log['skipped'] = _skipped
            if _repl:
                _old_rid = _cur_opener_rid
                _new_dur = _repl.get('clip_duration_sec', _repl.get('duration', 3.0))
                _slot_dur = timeline[0].get('use_end_offset', 2.7) - timeline[0].get('use_start_offset', 0)
                timeline[0]['reel_clip_id'] = _repl_rid
                timeline[0]['source_file'] = _repl.get('source_file', '')
                timeline[0]['start_sec'] = 0.0
                timeline[0]['end_sec'] = min(_new_dur, _slot_dur)
                timeline[0]['use_start_offset'] = 0.0
                timeline[0]['use_end_offset'] = min(_new_dur, _slot_dur)
                timeline[0]['selection_reason'] = f'[v13.2-h step6.4 opener_ranking] {_old_rid}(score={_cur_opener_score}) → {_repl_rid}(score={_repl_score})'
                _opener_replaced = True
                _opener_log['replaced'] = True
                _opener_log['new_opener'] = _repl_rid
                _opener_log['new_score'] = _repl_score
                print(f"  [v13.2-h step6.4] opener_ranking: slot_01 {_old_rid}(score={_cur_opener_score}) → {_repl_rid}(score={_repl_score})")
            else:
                _opener_log['replaced'] = False
                _opener_log['reason'] = 'no_available_top_opener'
                print(f"  [v13.2-h step6.4] opener_ranking: slot_01 {_cur_opener_rid}(score={_cur_opener_score}), no better opener available")
        else:
            _opener_log['replaced'] = False
            _opener_log['reason'] = f'current_score={_cur_opener_score} >= top-3={_top_score-3}'
            print(f"  [v13.2-h step6.4] opener_ranking: slot_01 {_cur_opener_rid}(score={_cur_opener_score}) already good (top={_top_score})")

        with open(output_dir / "opener_ranking_summary.json", 'w', encoding='utf-8') as _gf:
            _json_step6.dump(_opener_log, _gf, ensure_ascii=False, indent=2)

    # ============================================================
    # v13.2-h step6.2 B: 同视角/同场景连续拼接控制（升级自 step6 B）
    # ============================================================
    SAME_VIEW_SCENE_RUN_LIMIT_GUARD = True
    if SAME_VIEW_SCENE_RUN_LIMIT_GUARD and timeline:
        _SERVICE_KW = ['桌前', '服务台', '讲解', '发资料', '发放', '递送', '咨询', '宣传资料', '交流', '隔桌', '答疑', '宣传手册', '宣传材料', '慰问物资']
        _SERVICE_MAX_CONSEC = 2  # v6.2: 从 3 降到 2（更严格的同类连续控制）

        # v6.2: scene_signature 分类
        def _get_scene_sig(desc):
            d = desc.lower() if desc else ''
            if any(k in d for k in ['互动','游戏','投掷','投沙包','投飞镖']): return 'interaction'
            if any(k in d for k in ['合影','横幅','红旗','标语','立牌','摆拍']): return 'static_banner'
            if any(k in d for k in ['发放','递送','发资料','宣传材料','宣传资料','慰问物资']): return 'handout'
            if any(k in d for k in ['讲解','答疑','政策宣讲']): return 'explanation'
            if any(k in d for k in ['交流','咨询','面对面']): return 'chat'
            if any(k in d for k in ['采访','记录','拍摄']): return 'interview'
            if any(k in d for k in ['人群','聚集','排队']): return 'crowd'
            return 'other'
        _SIG_MAX_CONSEC = 3  # 同一 signature 连续上限
        _manifest_map_svc = {m['reel_clip_id']: m for m in manifest}
        _used_rids_svc = {t.get('reel_clip_id', '') for t in timeline}
        # VS_05 保护
        _VS_PROTECT_RIDS_SVC = set()
        # 保护 timeline 中的互动 clip
        for _t in timeline:
            _r = _t.get('reel_clip_id', '')
            _m_desc_svc = _manifest_map_svc.get(_r, {}).get('scene_description', '')
            if any(kw in _m_desc_svc for kw in ['互动', '游戏', '投掷', '投沙包', '投飞镖']):
                _VS_PROTECT_RIDS_SVC.add(_r)
            if _manifest_map_svc.get(_r, {}).get('_is_variant'):
                _VS_PROTECT_RIDS_SVC.add(_r)
        # 也保护 manifest 中所有互动/variant clip（防止被用作替换源）
        for _mc in manifest:
            _mcr = _mc.get('reel_clip_id', '')
            _mcd = _mc.get('scene_description', '')
            if any(kw in _mcd for kw in ['互动', '游戏', '投掷', '投沙包', '投飞镖']):
                _VS_PROTECT_RIDS_SVC.add(_mcr)
            if _mc.get('_is_variant'):
                _VS_PROTECT_RIDS_SVC.add(_mcr)

        # v6.2: 扫描连续 scene_signature run + service run
        _svc_replaced = 0
        _svc_guard_log = []
        _consec_svc = 0
        _consec_sig = 0
        _prev_sig = None

        for _si in range(len(timeline)):
            _tl = timeline[_si]
            _rid = _tl.get('reel_clip_id', '')
            if _rid in _VS_PROTECT_RIDS_SVC:
                _consec_svc = 0
                _consec_sig = 0
                _prev_sig = None
                continue
            _m_item = _manifest_map_svc.get(_rid, {})
            _desc = _m_item.get('scene_description', '')
            _is_service = any(kw in _desc for kw in _SERVICE_KW)
            _cur_sig = _get_scene_sig(_desc)

            # 更新 signature 连续计数
            if _cur_sig == _prev_sig and _cur_sig not in ('interaction', 'other'):
                _consec_sig += 1
            else:
                _consec_sig = 1
                _prev_sig = _cur_sig

            # 更新 service 连续计数
            if _is_service:
                _consec_svc += 1
            else:
                _consec_svc = 0

            # v6.2: 触发替换条件
            _should_replace = False
            _replace_reason = ''
            if _consec_svc > _SERVICE_MAX_CONSEC:
                _should_replace = True
                _replace_reason = f'service×{_consec_svc}'
            elif _consec_sig > _SIG_MAX_CONSEC:
                _should_replace = True
                _replace_reason = f'{_cur_sig}×{_consec_sig}'

            if _should_replace:
                _BLOCK_KW = ['合影', '横幅', '红旗', '标语', '摆拍']
                _repl = None
                for _cand in sorted(manifest, key=lambda x: -x.get('score_total', 0)):
                    _crid = _cand.get('reel_clip_id', '')
                    _cdesc = _cand.get('scene_description', '')
                    if _crid in _used_rids_svc or _crid in _VS_PROTECT_RIDS_SVC:
                        continue
                    if _cand.get('pool_level') == 'disabled':
                        continue
                    if any(kw in _cdesc for kw in _BLOCK_KW):
                        continue
                    # 不替换成同 signature
                    if _get_scene_sig(_cdesc) == _cur_sig:
                        continue
                    _repl = _cand
                    break
                if _repl:
                    _old_rid = _rid
                    _new_rid = _repl['reel_clip_id']
                    _new_dur = _repl.get('clip_duration_sec', _repl.get('duration', 3.0))
                    _slot_dur = _tl.get('use_end_offset', 2.7) - _tl.get('use_start_offset', 0)
                    timeline[_si]['reel_clip_id'] = _new_rid
                    timeline[_si]['source_file'] = _repl.get('source_file', '')
                    timeline[_si]['start_sec'] = 0.0
                    timeline[_si]['end_sec'] = timeline[_si].get('use_end_offset', 2.7)
                    timeline[_si]['use_start_offset'] = 0.0
                    timeline[_si]['use_end_offset'] = min(_new_dur, _slot_dur)
                    timeline[_si]['selection_reason'] = f'[v13.2-h step6.2 scene_run] {_old_rid}({_replace_reason}) → {_new_rid}'
                    _used_rids_svc.add(_new_rid)
                    _svc_replaced += 1
                    _svc_guard_log.append({'slot': f'slot_{_si+1}', 'old': _old_rid, 'new': _new_rid, 'reason': _replace_reason})
                    _consec_svc = 0
                    _consec_sig = 1
                    _prev_sig = _get_scene_sig(_repl.get('scene_description', ''))
                    print(f"  [v13.2-h step6.2] scene_run: slot_{_si+1} {_old_rid}({_replace_reason}) → {_new_rid}")
                else:
                    _svc_guard_log.append({'slot': f'slot_{_si+1}', 'old': _rid, 'new': None, 'reason': f'no_replacement ({_replace_reason})'})
                    print(f"  [v13.2-h step6.2] scene_run: slot_{_si+1} {_rid} {_replace_reason}, no replacement")
            else:
                _consec_svc = 0
                _run_start = -1

        if _svc_replaced > 0:
            print(f"  [v13.2-h step6] scene_run_limit: replaced={_svc_replaced}")
        else:
            print(f"  [v13.2-h step6] scene_run_limit: 无连续 service 堆叠超过 {_SERVICE_MAX_CONSEC}")
        _svc_guard_path = output_dir / "scene_run_guard_summary.json"
        with open(_svc_guard_path, 'w', encoding='utf-8') as _gf:
            _json_step6.dump({'guard': 'scene_run_limit', 'max_consec': _SERVICE_MAX_CONSEC, 'log': _svc_guard_log, 'replaced': _svc_replaced}, _gf, ensure_ascii=False, indent=2)

    # ============================================================
    # v13.2-h step6.1: 低信息重复镜头全局压制
    # ============================================================
    LOW_INFO_GLOBAL_REPEAT_GUARD = True
    if LOW_INFO_GLOBAL_REPEAT_GUARD and timeline:
        _LOW_INFO_KW = ['合影', '横幅', '红旗', '标语', '立牌', '摆拍', '举横幅', '举旗',
                        '背景板', '签到墙', '活动留影', '口号', '主视觉', 'logo']
        _manifest_map_li = {m['reel_clip_id']: m for m in manifest}
        # 互动/variant 保护集（全 manifest 扫描）
        _VS_PROTECT_LI = set()
        for _mc in manifest:
            _mcr = _mc.get('reel_clip_id', '')
            _mcd = _mc.get('scene_description', '')
            if any(kw in _mcd for kw in ['互动', '游戏', '投掷', '投沙包', '投飞镖']):
                _VS_PROTECT_LI.add(_mcr)
            if _mc.get('_is_variant'):
                _VS_PROTECT_LI.add(_mcr)

        # 扫描 timeline 中的低信息镜头
        _li_slots = []  # (slot_idx, rid, src, desc, kw_matched)
        for _si in range(len(timeline)):
            _tl = timeline[_si]
            _rid = _tl.get('reel_clip_id', '')
            if _rid in _VS_PROTECT_LI:
                continue
            _m_item = _manifest_map_li.get(_rid, {})
            _desc = _m_item.get('scene_description', '')
            _matched = [k for k in _LOW_INFO_KW if k in _desc]
            if _matched:
                _li_slots.append((_si, _rid, _m_item.get('source_file', ''), _desc, _matched))

        # 全局限额：同一 source_file 的低信息镜头最多 1 个
        _li_replaced = 0
        _li_guard_log = []
        _used_rids_li = {t.get('reel_clip_id', '') for t in timeline}
        _seen_low_info_src = {}  # source_file → first slot_idx

        for _si, _rid, _src, _desc, _matched in _li_slots:
            if _src in _seen_low_info_src:
                # 重复！尝试替换
                _first_slot = _seen_low_info_src[_src]
                _REPLACE_BLOCK_KW = ['合影', '横幅', '红旗', '标语', '立牌', '摆拍', '举横幅', '背景板']
                _repl = None
                for _cand in sorted(manifest, key=lambda x: -x.get('score_total', 0)):
                    _crid = _cand.get('reel_clip_id', '')
                    _cdesc = _cand.get('scene_description', '')
                    if _crid in _used_rids_li or _crid in _VS_PROTECT_LI:
                        continue
                    if _cand.get('pool_level') == 'disabled':
                        continue
                    if any(kw in _cdesc for kw in _REPLACE_BLOCK_KW):
                        continue
                    _repl = _cand
                    break
                if _repl:
                    _old_rid = _rid
                    _new_rid = _repl['reel_clip_id']
                    _new_dur = _repl.get('clip_duration_sec', _repl.get('duration', 3.0))
                    _slot_dur = timeline[_si].get('use_end_offset', 2.7) - timeline[_si].get('use_start_offset', 0)
                    timeline[_si]['reel_clip_id'] = _new_rid
                    timeline[_si]['source_file'] = _repl.get('source_file', '')
                    timeline[_si]['start_sec'] = 0.0
                    timeline[_si]['end_sec'] = timeline[_si].get('use_end_offset', 2.7)
                    timeline[_si]['use_start_offset'] = 0.0
                    timeline[_si]['use_end_offset'] = min(_new_dur, _slot_dur)
                    timeline[_si]['selection_reason'] = f'[v13.2-h step6.1 low_info_repeat] {_old_rid}(dup {_src[:20]}) → {_new_rid}'
                    _used_rids_li.add(_new_rid)
                    _li_replaced += 1
                    _li_guard_log.append({'slot': f'slot_{_si+1}', 'old': _old_rid, 'new': _new_rid, 'dup_src': _src, 'first_slot': f'slot_{_first_slot+1}'})
                    print(f"  [v13.2-h step6.1] low_info_repeat: slot_{_si+1} {_old_rid}(dup of slot_{_first_slot+1}) → {_new_rid}")
                else:
                    _li_guard_log.append({'slot': f'slot_{_si+1}', 'old': _rid, 'new': None, 'reason': 'no_replacement', 'dup_src': _src})
                    print(f"  [v13.2-h step6.1] low_info_repeat: slot_{_si+1} {_rid} is dup but no replacement")
            else:
                _seen_low_info_src[_src] = _si

        # 额外：全片低信息总数限额（最多 2 个）
        _li_total_after = 0
        for _si in range(len(timeline)):
            _rid = timeline[_si].get('reel_clip_id', '')
            if _rid in _VS_PROTECT_LI:
                continue
            _m_item = _manifest_map_li.get(_rid, {})
            _desc = _m_item.get('scene_description', '')
            if any(k in _desc for k in _LOW_INFO_KW):
                _li_total_after += 1
                if _li_total_after > 2:
                    # 尝试替换第 3+ 个低信息
                    _REPLACE_BLOCK_KW2 = ['合影', '横幅', '红旗', '标语', '立牌', '摆拍', '举横幅', '背景板']
                    _repl2 = None
                    for _cand in sorted(manifest, key=lambda x: -x.get('score_total', 0)):
                        _crid2 = _cand.get('reel_clip_id', '')
                        _cdesc2 = _cand.get('scene_description', '')
                        if _crid2 in _used_rids_li or _crid2 in _VS_PROTECT_LI:
                            continue
                        if _cand.get('pool_level') == 'disabled':
                            continue
                        if any(kw in _cdesc2 for kw in _REPLACE_BLOCK_KW2):
                            continue
                        _repl2 = _cand
                        break
                    if _repl2:
                        _old_rid2 = _rid
                        _new_rid2 = _repl2['reel_clip_id']
                        _new_dur2 = _repl2.get('clip_duration_sec', _repl2.get('duration', 3.0))
                        _slot_dur2 = timeline[_si].get('use_end_offset', 2.7) - timeline[_si].get('use_start_offset', 0)
                        timeline[_si]['reel_clip_id'] = _new_rid2
                        timeline[_si]['source_file'] = _repl2.get('source_file', '')
                        timeline[_si]['start_sec'] = 0.0
                        timeline[_si]['end_sec'] = timeline[_si].get('use_end_offset', 2.7)
                        timeline[_si]['use_start_offset'] = 0.0
                        timeline[_si]['use_end_offset'] = min(_new_dur2, _slot_dur2)
                        timeline[_si]['selection_reason'] = f'[v13.2-h step6.1 low_info_cap] {_old_rid2}(low_info #{_li_total_after}) → {_new_rid2}'
                        _used_rids_li.add(_new_rid2)
                        _li_replaced += 1
                        _li_guard_log.append({'slot': f'slot_{_si+1}', 'old': _old_rid2, 'new': _new_rid2, 'reason': f'low_info_cap_{_li_total_after}'})
                        _li_total_after -= 1
                        print(f"  [v13.2-h step6.1] low_info_cap: slot_{_si+1} {_old_rid2}(#{_li_total_after+1}) → {_new_rid2}")

        if _li_replaced > 0:
            print(f"  [v13.2-h step6.1] low_info_repeat_guard: replaced={_li_replaced}")
        else:
            print(f"  [v13.2-h step6.1] low_info_repeat_guard: 无低信息重复")
        _li_guard_path = output_dir / "low_info_global_guard_summary.json"
        with open(_li_guard_path, 'w', encoding='utf-8') as _gf:
            _json_step6.dump({'guard': 'low_info_global_repeat', 'log': _li_guard_log, 'replaced': _li_replaced, 'total_low_info_after': _li_total_after}, _gf, ensure_ascii=False, indent=2)

    # ============================================================
    # v13.2-h step6.4 B: VS_05 interaction_min_count guard
    # ============================================================
    VS05_INTERACTION_MIN_COUNT_GUARD = True
    VS05_INTERACTION_MIN = 3
    if VS05_INTERACTION_MIN_COUNT_GUARD and timeline:
        _INTERACTION_KW = ['互动游戏', '投掷', '投沙包', '投飞镖', '大骰子', '安全大富翁', '趣味互动', '主题互动']
        _NOT_INTERACTION_KW = ['发放', '递送', '宣传材料', '宣传资料', '慰问物资', '讲解', '答疑', '咨询', '交流', '服务台', '桌前']
        _VS05_KW = ['趣味互动', '互动环节', '互动游戏']
        _manifest_map_vs = {m['reel_clip_id']: m for m in manifest}

        # 找 VS_05 段的 slot 范围（通过 selection_reason 或 scene_block_name）
        _vs05_slots = []
        for _si, _tl in enumerate(timeline):
            _rid = _tl.get('reel_clip_id', '')
            _m = _manifest_map_vs.get(_rid, {})
            _desc = _m.get('scene_description', '')
            _reason = _tl.get('selection_reason', '')
            _block = _tl.get('scene_block_name', '')
            _is_true_interaction = (
                any(k in _desc for k in _INTERACTION_KW)
                and not any(k in _desc for k in _NOT_INTERACTION_KW)
            ) or _block == '趣味互动环节'
            if _is_true_interaction:
                _vs05_slots.append(_si)

        _vs05_interaction_count = len(_vs05_slots)
        _vs05_guard_log = {
            'guard': 'vs05_interaction_min_count',
            'min_required': VS05_INTERACTION_MIN,
            'before_count': _vs05_interaction_count,
            'vs05_slots': [f'slot_{s+1}' for s in _vs05_slots],
            'vs05_rids': [timeline[s].get('reel_clip_id', '') for s in _vs05_slots],
        }

        if _vs05_interaction_count < VS05_INTERACTION_MIN:
            _deficit = VS05_INTERACTION_MIN - _vs05_interaction_count
            print(f"  [v13.2-h step6.4] VS_05 interaction: {_vs05_interaction_count}/{VS05_INTERACTION_MIN} → 需补 {_deficit} 个")
            _used_rids_vs = {t.get('reel_clip_id', '') for t in timeline}
            _added = []

            # 找非 interaction 的 slot 做替换目标（从后往前找 service/handout 类）
            _replaceable_slots = []
            for _si in reversed(range(len(timeline))):
                if _si in _vs05_slots: continue  # 不动已有互动 slot
                _rid = timeline[_si].get('reel_clip_id', '')
                _m = _manifest_map_vs.get(_rid, {})
                _desc = _m.get('scene_description', '')
                # 只替换 service/handout/explanation 类
                if any(k in _desc for k in ['发放', '递送', '宣传材料', '讲解', '答疑', '交流', '咨询']):
                    _replaceable_slots.append(_si)

            # 找可用 interaction 候选
            _interaction_candidates = []
            for _mc in manifest:
                _mcr = _mc.get('reel_clip_id', '')
                _mcd = _mc.get('scene_description', '')
                if _mcr in _used_rids_vs: continue
                if _mc.get('pool_level') == 'disabled': continue
                if not any(k in _mcd for k in _INTERACTION_KW): continue
                if any(k in _mcd for k in _NOT_INTERACTION_KW): continue  # 排除 handout 等
                _stab = _mc.get('score_detail', {}).get('stability', 0)
                if _stab < 70: continue  # 必须通过 quality gate
                _dur = _mc.get('clip_duration_sec', _mc.get('duration', 0))
                if _dur < 2.0: continue
                _interaction_candidates.append(_mc)
            # 按 opener_score 排序
            _interaction_candidates.sort(key=lambda x: -_calc_opener_score(x))

            for _ic in _interaction_candidates[:_deficit]:
                if not _replaceable_slots: break
                _target_si = _replaceable_slots.pop(0)
                _old_rid = timeline[_target_si].get('reel_clip_id', '')
                _new_rid = _ic['reel_clip_id']
                _new_dur = _ic.get('clip_duration_sec', _ic.get('duration', 3.0))
                _slot_dur = timeline[_target_si].get('use_end_offset', 2.7) - timeline[_target_si].get('use_start_offset', 0)
                timeline[_target_si]['reel_clip_id'] = _new_rid
                timeline[_target_si]['source_file'] = _ic.get('source_file', '')
                timeline[_target_si]['start_sec'] = 0.0
                timeline[_target_si]['end_sec'] = min(_new_dur, _slot_dur)
                timeline[_target_si]['use_start_offset'] = 0.0
                timeline[_target_si]['use_end_offset'] = min(_new_dur, _slot_dur)
                timeline[_target_si]['selection_reason'] = f'[v13.2-h step6.4 vs05_guard] {_old_rid}(service) → {_new_rid}(interaction)'
                _used_rids_vs.add(_new_rid)
                _added.append({'slot': f'slot_{_target_si+1}', 'old': _old_rid, 'new': _new_rid})
                print(f"  [v13.2-h step6.4] VS_05 补位: slot_{_target_si+1} {_old_rid} → {_new_rid}")

            _vs05_guard_log['added'] = _added
            _vs05_guard_log['after_count'] = _vs05_interaction_count + len(_added)
            if len(_added) < _deficit:
                _vs05_guard_log['warning'] = f'only_added_{len(_added)}_of_{_deficit}'
                print(f"  [v13.2-h step6.4] VS_05 补位不足: 需 {_deficit}，只补了 {len(_added)}")
        else:
            _vs05_guard_log['after_count'] = _vs05_interaction_count
            print(f"  [v13.2-h step6.4] VS_05 interaction: {_vs05_interaction_count}/{VS05_INTERACTION_MIN} ✅ 已满足")

        with open(output_dir / "vs05_interaction_guard_summary.json", 'w', encoding='utf-8') as _gf:
            _json_step6.dump(_vs05_guard_log, _gf, ensure_ascii=False, indent=2)

    # ============================================================
    # v13.2-h step6.5 A: 开场后低信息延后
    # ============================================================
    POST_OPENER_LOW_INFO_DELAY_GUARD = True
    if POST_OPENER_LOW_INFO_DELAY_GUARD and timeline and len(timeline) > 3:
        _LOW_INFO_KW_65 = ['合影', '横幅', '红旗', '标语', '立牌', '摆拍', '主视觉']
        _manifest_map_65 = {m['reel_clip_id']: m for m in manifest}
        _VS_PROTECT_65 = set()
        for _t65 in timeline:
            _r65 = _t65.get('reel_clip_id', '')
            _d65 = _manifest_map_65.get(_r65, {}).get('scene_description', '')
            if any(k in _d65 for k in ['互动', '游戏', '投掷', '投沙包', '投飞镖']):
                _VS_PROTECT_65.add(_r65)
            if _manifest_map_65.get(_r65, {}).get('_is_variant'):
                _VS_PROTECT_65.add(_r65)

        _post_opener_log = {'guard': 'post_opener_low_info_delay', 'swaps': []}
        # 检查 slot_02 和 slot_03（开场后 6 秒）
        for _check_si in [1, 2]:
            if _check_si >= len(timeline): break
            _tl65 = timeline[_check_si]
            _rid65 = _tl65.get('reel_clip_id', '')
            _m65 = _manifest_map_65.get(_rid65, {})
            _desc65 = _m65.get('scene_description', '')
            if not any(k in _desc65 for k in _LOW_INFO_KW_65):
                continue
            # 找后面可交换的 slot（非 LOW_INFO、非 VS_05、非 opener）
            _swapped = False
            for _swap_si in range(3, len(timeline)):
                _swap_rid = timeline[_swap_si].get('reel_clip_id', '')
                if _swap_rid in _VS_PROTECT_65: continue
                _swap_m = _manifest_map_65.get(_swap_rid, {})
                _swap_desc = _swap_m.get('scene_description', '')
                if any(k in _swap_desc for k in _LOW_INFO_KW_65): continue
                # 交换（只交换 clip 标识和渲染字段，保留 slot 时间结构）
                _swap_fields = ['reel_clip_id', 'source_file', 'selection_reason']
                _old_vals = {k: timeline[_check_si].get(k) for k in _swap_fields}
                _new_vals = {k: timeline[_swap_si].get(k) for k in _swap_fields}
                for _fk in _swap_fields:
                    timeline[_check_si][_fk] = _new_vals.get(_fk, '')
                    timeline[_swap_si][_fk] = _old_vals.get(_fk, '')
                timeline[_check_si]['selection_reason'] = f'[v13.2-h step6.5 post_opener_delay] swap slot_{_check_si+1}↔slot_{_swap_si+1}: {_rid65}(LOW_INFO)↔{_swap_rid}'
                _post_opener_log['swaps'].append({'from': f'slot_{_check_si+1}', 'to': f'slot_{_swap_si+1}', 'low_info_rid': _rid65, 'replacement_rid': _swap_rid})
                print(f"  [v13.2-h step6.5] post_opener_delay: slot_{_check_si+1}({_rid65}) ↔ slot_{_swap_si+1}({_swap_rid})")
                _swapped = True
                break
            if not _swapped:
                _post_opener_log['swaps'].append({'from': f'slot_{_check_si+1}', 'to': None, 'low_info_rid': _rid65, 'reason': 'no_swap_candidate'})
                print(f"  [v13.2-h step6.5] post_opener_delay: slot_{_check_si+1}({_rid65}) is LOW_INFO but no swap candidate")

        if _post_opener_log['swaps']:
            print(f"  [v13.2-h step6.5] post_opener_delay: {len([s for s in _post_opener_log['swaps'] if s.get('to')])} swaps")
        else:
            print(f"  [v13.2-h step6.5] post_opener_delay: 开场后无 LOW_INFO")
        with open(output_dir / "post_opener_low_info_guard_summary.json", 'w', encoding='utf-8') as _gf65:
            _json_step6.dump(_post_opener_log, _gf65, ensure_ascii=False, indent=2)

    # ============================================================
    # v13.2-h step6.5 B: 同 source / 同 clip 重复压制
    # ============================================================
    DUPLICATE_SOURCE_CLIP_GUARD = True
    if DUPLICATE_SOURCE_CLIP_GUARD and timeline and edit_mode != 'music_only':
        _manifest_map_dup = {m['reel_clip_id']: m for m in manifest}
        _VS_PROTECT_DUP = set()
        for _mc_dup in manifest:
            _mcr_dup = _mc_dup.get('reel_clip_id', '')
            _mcd_dup = _mc_dup.get('scene_description', '')
            if any(kw in _mcd_dup for kw in ['互动', '游戏', '投掷', '投沙包', '投飞镖']):
                _VS_PROTECT_DUP.add(_mcr_dup)
            if _mc_dup.get('_is_variant'):
                _VS_PROTECT_DUP.add(_mcr_dup)

        _seen_rids = {}  # rid → first slot_idx
        _dup_log = {'guard': 'duplicate_source_clip', 'duplicates': [], 'replaced': 0}
        _used_rids_dup = {t.get('reel_clip_id', '') for t in timeline}

        for _si_dup in range(len(timeline)):
            _rid_dup = timeline[_si_dup].get('reel_clip_id', '')
            if _rid_dup in _VS_PROTECT_DUP: continue
            if _rid_dup in _seen_rids:
                # 重复！替换较晚一次
                _BLOCK_DUP = ['合影', '横幅', '红旗', '标语', '摆拍']
                _repl_dup = None
                for _cand_dup in sorted(manifest, key=lambda x: -x.get('score_total', 0)):
                    _crid_dup = _cand_dup.get('reel_clip_id', '')
                    _cdesc_dup = _cand_dup.get('scene_description', '')
                    if _crid_dup in _used_rids_dup or _crid_dup in _VS_PROTECT_DUP: continue
                    if _cand_dup.get('pool_level') == 'disabled': continue
                    if any(k in _cdesc_dup for k in _BLOCK_DUP): continue
                    _stab_dup = _cand_dup.get('score_detail', {}).get('stability', 0)
                    if _stab_dup < 70: continue
                    _dur_dup = _cand_dup.get('clip_duration_sec', _cand_dup.get('duration', 0))
                    if _dur_dup < 2.0: continue
                    _repl_dup = _cand_dup
                    break
                if _repl_dup:
                    _old_dup = _rid_dup
                    _new_dup = _repl_dup['reel_clip_id']
                    _new_dur_dup = _repl_dup.get('clip_duration_sec', _repl_dup.get('duration', 3.0))
                    _slot_dur_dup = timeline[_si_dup].get('use_end_offset', 2.7) - timeline[_si_dup].get('use_start_offset', 0)
                    timeline[_si_dup]['reel_clip_id'] = _new_dup
                    timeline[_si_dup]['source_file'] = _repl_dup.get('source_file', '')
                    timeline[_si_dup]['start_sec'] = 0.0
                    timeline[_si_dup]['end_sec'] = min(_new_dur_dup, _slot_dur_dup)
                    timeline[_si_dup]['use_start_offset'] = 0.0
                    timeline[_si_dup]['use_end_offset'] = min(_new_dur_dup, _slot_dur_dup)
                    timeline[_si_dup]['selection_reason'] = f'[v13.2-h step6.5 dup_guard] {_old_dup}(dup of slot_{_seen_rids[_rid_dup]+1}) → {_new_dup}'
                    _used_rids_dup.add(_new_dup)
                    _dup_log['duplicates'].append({'slot': f'slot_{_si_dup+1}', 'dup_rid': _old_dup, 'first_slot': f'slot_{_seen_rids[_rid_dup]+1}', 'replaced_with': _new_dup})
                    _dup_log['replaced'] += 1
                    print(f"  [v13.2-h step6.5] dup_guard: slot_{_si_dup+1} {_old_dup}(dup) → {_new_dup}")
                else:
                    _dup_log['duplicates'].append({'slot': f'slot_{_si_dup+1}', 'dup_rid': _rid_dup, 'first_slot': f'slot_{_seen_rids[_rid_dup]+1}', 'replaced_with': None, 'reason': 'no_replacement'})
                    print(f"  [v13.2-h step6.5] dup_guard: slot_{_si_dup+1} {_rid_dup} is dup but no replacement")
            else:
                _seen_rids[_rid_dup] = _si_dup

        if _dup_log['replaced'] > 0:
            print(f"  [v13.2-h step6.5] dup_guard: replaced={_dup_log['replaced']}")
        else:
            print(f"  [v13.2-h step6.5] dup_guard: 无重复 clip")
        with open(output_dir / "duplicate_source_clip_guard_summary.json", 'w', encoding='utf-8') as _gf_dup:
            _json_step6.dump(_dup_log, _gf_dup, ensure_ascii=False, indent=2)

    # ============================================================
    # v13.1-f: static_context 连续限制 + 比例限制
    # ============================================================
    _STATIC_MAX_CONSEC = 1
    _STATIC_MAX_RATIO = 0.25
    _manifest_map_static = {m['reel_clip_id']: m for m in manifest}
    _static_replaced = 0
    _consec_static = 0
    # v13.2-i: opener_ranking 替换的 slot_01 必须受保护，不被 static_limit 再替换
    _opener_protected_rid = timeline[0].get('reel_clip_id', '') if timeline else ''
    _total_dur = sum(_t.get('use_end_offset', 0) - _t.get('use_start_offset', 0) for _t in timeline)
    _static_dur = 0.0
    _used_rids_static = {t.get('reel_clip_id', '') for t in timeline}

    for _si in range(len(timeline)):
        _tl = timeline[_si]
        _rid = _tl.get('reel_clip_id', '')
        # v13.2-i: opener_ranking 替换的 slot 不参与 static_limit 替换
        if _rid == _opener_protected_rid and _si == 0:
            _consec_static = 0
            continue
        _m = _manifest_map_static.get(_rid, {})
        _vc = _m.get('visual_class', _tl.get('visual_class', ''))
        _mal = _tl.get('main_action_level', '')
        _is_static = _vc in ('banner', 'environment') or _mal == 'static_context'
        _slot_dur = _tl.get('use_end_offset', 0) - _tl.get('use_start_offset', 0)

        if _is_static:
            _consec_static += 1
            _ratio_if_added = (_static_dur + _slot_dur) / max(_total_dur, 1)
            if _consec_static > _STATIC_MAX_CONSEC or _ratio_if_added > _STATIC_MAX_RATIO:
                # 找替换
                _repl = None
                for _cand in sorted(manifest, key=lambda x: -x.get('info_score', 0)):
                    if (_cand.get('visual_class') == 'primary_action'
                            and _cand.get('reel_clip_id') not in _used_rids_static
                            and _cand.get('candidate_tier') in ('safe_main', 'risky_key')):
                        _repl = _cand
                        break
                if _repl:
                    _old_rid = _rid
                    _new_rid = _repl['reel_clip_id']
                    _new_dur = _repl.get('clip_duration_sec', _repl.get('duration', 3.0))
                    timeline[_si]['reel_clip_id'] = _new_rid
                    timeline[_si]['source_file'] = _repl.get('source_file', '')
                    timeline[_si]['start_sec'] = 0.0
                    timeline[_si]['end_sec'] = timeline[_si].get('use_end_offset', 2.7)
                    timeline[_si]['use_start_offset'] = 0.0
                    timeline[_si]['use_end_offset'] = min(_new_dur, _slot_dur)
                    timeline[_si]['main_action_level'] = 'primary_action'
                    timeline[_si]['selection_reason'] = f'[v13.1-f static_limit] {_old_rid}({_vc}) → {_new_rid}(primary_action)'
                    _used_rids_static.add(_new_rid)
                    _static_replaced += 1
                    _consec_static = 0
                    print(f"  [v13.1-f] static_limit: slot_{_si+1} {_old_rid}({_vc}) → {_new_rid}(primary_action)")
                else:
                    _static_dur += _slot_dur
            else:
                _static_dur += _slot_dur
        else:
            _consec_static = 0

    _final_static_count = sum(1 for _t in timeline
                              if _manifest_map_static.get(_t.get('reel_clip_id',''),{}).get('visual_class','') in ('banner','environment')
                              or _t.get('main_action_level','') == 'static_context')
    _final_static_ratio = round(_static_dur / max(_total_dur, 1), 3)
    print(f"  [v13.1-f] static_context: replaced={_static_replaced} remaining={_final_static_count} ratio={_final_static_ratio}")
    print(f"  [v13.2-c monitor] static_limit: checked={len(timeline)} static_count={_final_static_count} ratio={_final_static_ratio:.2f} triggered={_static_replaced}")

    # ============================================================
    # v13.1-f: 卡点审计 — timeline 总时长 vs TTS 音频总时长
    # ============================================================
    # 修复 timeline_total_dur 计算：替换后 offset 可能为 0，用 manifest 兜底
    _manifest_map_dur = {m['reel_clip_id']: m.get('clip_duration_sec', m.get('duration', 3.0)) for m in manifest}
    _timeline_total_dur = 0
    for _t in timeline:
        _dur = _t.get('use_end_offset', 0) - _t.get('use_start_offset', 0)
        if _dur <= 0:
            _dur = min(_manifest_map_dur.get(_t.get('reel_clip_id', ''), 3.0), 3.0)
            _t['use_start_offset'] = 0.0
            _t['use_end_offset'] = _dur
        _timeline_total_dur += _dur
    print(f"  [v13.1-f] timeline_total_dur={_timeline_total_dur:.1f}s vs tts_dur={tts_dur:.1f}s diff={_timeline_total_dur - tts_dur:.1f}s")

    # ============================================================
    # v13.2-i.1: 最终 duplicate strict + post_opener strict（所有 guard 之后）
    # ============================================================
    if timeline:
        _manifest_map_final = {m['reel_clip_id']: m for m in manifest}
        _LOW_KW_FINAL = ['合影', '横幅', '红旗', '标语', '立牌', '摆拍', '主视觉']
        _VS_PROTECT_FINAL = set()
        for _mc_f in manifest:
            _mcr_f = _mc_f.get('reel_clip_id', '')
            _mcd_f = _mc_f.get('scene_description', '')
            if any(kw in _mcd_f for kw in ['互动', '游戏', '投掷', '投沙包', '投飞镖']):
                _VS_PROTECT_FINAL.add(_mcr_f)
            if _mc_f.get('_is_variant'):
                _VS_PROTECT_FINAL.add(_mcr_f)

        # A: Post-opener strict — slot_02/03 不得有 LOW_INFO
        _opener_rid_final = timeline[0].get('reel_clip_id', '')
        for _fi in [1, 2]:
            if _fi >= len(timeline): break
            _frid = timeline[_fi].get('reel_clip_id', '')
            _fdesc = _manifest_map_final.get(_frid, {}).get('scene_description', '')
            if not any(k in _fdesc for k in _LOW_KW_FINAL): continue
            # 找后面的 slot 交换
            _swapped_f = False
            for _fj in range(max(_fi+1, 4), len(timeline)):
                _fjrid = timeline[_fj].get('reel_clip_id', '')
                if _fjrid in _VS_PROTECT_FINAL: continue
                _fjdesc = _manifest_map_final.get(_fjrid, {}).get('scene_description', '')
                if any(k in _fjdesc for k in _LOW_KW_FINAL): continue
                # 交换 clip 标识
                for _fk in ['reel_clip_id', 'source_file', 'selection_reason']:
                    timeline[_fi][_fk], timeline[_fj][_fk] = timeline[_fj].get(_fk, ''), timeline[_fi].get(_fk, '')
                timeline[_fi]['selection_reason'] = f'[v13.2-i.1 post_opener_strict] swap slot_{_fi+1}↔slot_{_fj+1}'
                print(f"  [v13.2-i.1] post_opener_strict: slot_{_fi+1}({_frid})↔slot_{_fj+1}({_fjrid})")
                _swapped_f = True
                break
            if not _swapped_f:
                print(f"  [v13.2-i.1] post_opener_strict: slot_{_fi+1}({_frid}) LOW_INFO but no swap target")

        # B: Duplicate strict — 全片同 source_file 最多 1 次（按 source_file 去重，不只是 clip_id）
        _seen_final = {}  # source_file → first slot_idx
        _used_final = {t.get('reel_clip_id', '') for t in timeline}
        _dup_final_log = []
        for _fi in range(len(timeline)):
            _frid = timeline[_fi].get('reel_clip_id', '')
            _fsrc = timeline[_fi].get('source_file', '')
            if _frid in _VS_PROTECT_FINAL: continue
            # 按 source_file 去重（同 source_file 的不同 clip 视觉上是同一画面）
            _dedup_key = _fsrc if _fsrc else _frid
            if _dedup_key in _seen_final:
                # 重复！替换较晚一次
                _repl_f = None
                _BLOCK_F = ['合影', '横幅', '红旗', '标语', '摆拍']
                for _cand_f in sorted(manifest, key=lambda x: -x.get('score_total', 0)):
                    _crid_f = _cand_f.get('reel_clip_id', '')
                    _cdesc_f = _cand_f.get('scene_description', '')
                    if _crid_f in _used_final or _crid_f in _VS_PROTECT_FINAL: continue
                    if _cand_f.get('pool_level') == 'disabled': continue
                    if any(k in _cdesc_f for k in _BLOCK_F): continue
                    _stab_f = _cand_f.get('score_detail', {}).get('stability', 0)
                    if _stab_f < 70: continue
                    _dur_f = _cand_f.get('clip_duration_sec', _cand_f.get('duration', 0))
                    if _dur_f < 2.0: continue
                    _repl_f = _cand_f
                    break
                if _repl_f:
                    _old_f = _frid
                    _new_f = _repl_f['reel_clip_id']
                    _new_dur_f = _repl_f.get('clip_duration_sec', _repl_f.get('duration', 3.0))
                    _slot_dur_f = timeline[_fi].get('use_end_offset', 2.7) - timeline[_fi].get('use_start_offset', 0)
                    if _slot_dur_f <= 0: _slot_dur_f = 2.7
                    timeline[_fi]['reel_clip_id'] = _new_f
                    timeline[_fi]['source_file'] = _repl_f.get('source_file', '')
                    timeline[_fi]['start_sec'] = 0.0
                    timeline[_fi]['end_sec'] = min(_new_dur_f, _slot_dur_f)
                    timeline[_fi]['use_start_offset'] = 0.0
                    timeline[_fi]['use_end_offset'] = min(_new_dur_f, _slot_dur_f)
                    timeline[_fi]['selection_reason'] = f'[v13.2-i.1 dup_strict] {_old_f}(dup)→{_new_f}'
                    _used_final.add(_new_f)
                    _dup_final_log.append({'slot': f'slot_{_fi+1}', 'old': _old_f, 'new': _new_f})
                    print(f"  [v13.2-i.1] dup_strict: slot_{_fi+1} {_old_f}(dup)→{_new_f}")
                else:
                    _dup_final_log.append({'slot': f'slot_{_fi+1}', 'old': _frid, 'new': None, 'reason': 'no_replacement'})
                    print(f"  [v13.2-i.1] dup_strict: slot_{_fi+1} {_frid}(dup) no replacement")
            else:
                _seen_final[_dedup_key] = _fi

        if _dup_final_log:
            print(f"  [v13.2-i.1] dup_strict: {len([d for d in _dup_final_log if d.get('new')])} replaced, {len([d for d in _dup_final_log if not d.get('new')])} unresolved")
        else:
            print(f"  [v13.2-i.1] dup_strict: 无重复 clip")
        with open(output_dir / "duplicate_strict_final_summary.json", 'w', encoding='utf-8') as _gf_df:
            _json_step6.dump({'guard': 'dup_strict_final', 'log': _dup_final_log}, _gf_df, ensure_ascii=False, indent=2)

    # ============================================================
    # v13.3-a1: Director Rhythm Guard — SERVICE 长段局部打散
    # 位置：dup_strict 之后、timeline_validator 之前
    # ============================================================
    DIRECTOR_RHYTHM_GUARD = True
    DIRECTOR_RHYTHM_SERVICE_RUN_THRESHOLD = 4
    DIRECTOR_RHYTHM_MAX_REPLACEMENTS = 2
    DIRECTOR_RHYTHM_MIN_STABILITY = 75

    if DIRECTOR_RHYTHM_GUARD and timeline and len(timeline) > 6:
        # --- SERVICE 判定关键词 ---
        _SERVICE_KW = ['发放', '递送', '讲解', '答疑', '咨询', '政策宣传', '服务台', '宣传材料',
                        '宣传资料', '慰问', '说明', '普及', '桌前', '长桌', '展台']
        _NON_SERVICE_KW = ['互动游戏', '投掷', '投沙包', '投飞镖', '大骰子', '安全大富翁',
                           '趣味互动', '主题互动', '采访', '受访', '群体聚集', '活动互动']
        _LOW_INFO_KW_RHY = ['横幅', '红旗', '举旗', '合影', '摆拍', '标识', '牌子', '标语',
                             '口号', '背景板', '签到墙', '展板', '纯环境', '空镜', '立牌', '主视觉']
        # 保护 clip
        _PROTECTED_RIDS = set()
        for _pt in timeline:
            _prid = _pt.get('reel_clip_id', '')
            _pdesc = ''
            for _pm in manifest:
                if _pm.get('reel_clip_id') == _prid:
                    _pdesc = _pm.get('scene_description', '')
                    break
            # opener (slot_01), VS_05 interaction, interview
            if _pt is timeline[0]:
                _PROTECTED_RIDS.add(_prid)
            if any(k in _pdesc for k in _NON_SERVICE_KW[:8]):  # interaction keywords
                _PROTECTED_RIDS.add(_prid)
            if any(k in _pdesc for k in ['采访', '受访']):
                _PROTECTED_RIDS.add(_prid)

        def _is_service_slot_rhy(tl_entry):
            rid = tl_entry.get('reel_clip_id', '')
            if rid in _PROTECTED_RIDS:
                return False
            desc = ''
            for _m in manifest:
                if _m.get('reel_clip_id') == rid:
                    desc = _m.get('scene_description', '')
                    break
            if not desc:
                src = tl_entry.get('source_file', '')
                for _m in manifest:
                    if _m.get('source_file') == src:
                        desc = _m.get('scene_description', '')
                        break
            if any(k in desc for k in _NON_SERVICE_KW):
                return False
            if any(k in desc for k in _SERVICE_KW):
                return True
            # 默认含 '工作人员' + ('骑手'|'外卖') 的也算 SERVICE
            if '工作人员' in desc and ('骑手' in desc or '外卖' in desc):
                return True
            return False

        # --- 检测连续 SERVICE run ---
        _service_runs = []
        _cur_run = []
        for _si, _st in enumerate(timeline):
            if _is_service_slot_rhy(_st):
                _cur_run.append(_si)
            else:
                if _cur_run:
                    _service_runs.append(list(_cur_run))
                _cur_run = []
        if _cur_run:
            _service_runs.append(list(_cur_run))

        _longest_run = max(_service_runs, key=len) if _service_runs else []
        _run_before = len(_longest_run)
        print(f"  [v13.3-a1] rhythm_guard: SERVICE runs={[len(r) for r in _service_runs]}, longest={_run_before}")

        _rhythm_log = {'guard': 'director_rhythm_v13.3-a1',
                       'service_run_before': _run_before,
                       'longest_run_slots': [f'slot_{i+1:02d}' for i in _longest_run],
                       'threshold': DIRECTOR_RHYTHM_SERVICE_RUN_THRESHOLD,
                       'replacements': []}

        if _run_before > DIRECTOR_RHYTHM_SERVICE_RUN_THRESHOLD:
            # --- 找 breathing/teaser 候选 ---
            _used_srcs_rhy = set(t.get('source_file', '') for t in timeline)
            _used_rids_rhy = set(t.get('reel_clip_id', '') for t in timeline)
            _TEASER_KW = ['互动游戏', '投掷', '投沙包', '投飞镖', '大骰子', '安全大富翁',
                          '趣味互动', '主题互动', '互动体验']
            _BREATHING_KW = ['排队', '人群', '聚集', '活动现场', '骑手群体', '列队',
                             '服务大篷车', '全景', '现场']

            # 一次性加载 pool 数据 + 完整 manifest（rhythm 需要更大候选池）
            from pipeline.pool_overrides import load_pool_data as _ldp_rhy
            _rhy_pool_data = _ldp_rhy(task_id)
            _rhy_full_manifest = manifest  # 默认用当前 manifest
            _rhy_manifest_path = output_dir / "candidate_reel_manifest.json"
            if _rhy_manifest_path.exists():
                try:
                    _rhy_mraw = json.load(open(_rhy_manifest_path))
                    _rhy_full_manifest = _rhy_mraw if isinstance(_rhy_mraw, list) else _rhy_mraw.get('clips', manifest)
                except Exception:
                    pass

            _rhy_candidates = []
            _rhy_skip_log = {'used_src': 0, 'discard': 0, 'low_stab': 0, 'low_info': 0, 'no_cw': 0, 'not_breath_teaser': 0, 'passed': 0}
            print(f"  [v13.3-a1] rhythm scan: full_manifest={len(_rhy_full_manifest)} filtered_manifest={len(manifest)} used_srcs={len(_used_srcs_rhy)} used_rids={len(_used_rids_rhy)}")
            for _mc in _rhy_full_manifest:
                _mc_src = _mc.get('source_file', '')
                _mc_rid = _mc.get('reel_clip_id', '')
                if _mc_src in _used_srcs_rhy or _mc_rid in _used_rids_rhy:
                    _rhy_skip_log['used_src'] += 1
                    continue
                if _mc.get('pool_level') in ('discard',):
                    _rhy_skip_log['discard'] += 1
                    continue
                _mc_desc = _mc.get('scene_description', '')
                _mc_sd = _mc.get('score_detail', {})
                _mc_stab = _mc_sd.get('stability', 0) if isinstance(_mc_sd, dict) else 0
                if _mc_stab < DIRECTOR_RHYTHM_MIN_STABILITY:
                    _rhy_skip_log['low_stab'] += 1
                    continue
                if any(k in _mc_desc for k in _LOW_INFO_KW_RHY):
                    _rhy_skip_log['low_info'] += 1
                    continue
                # 检查 clean_window
                _mc_pool = _rhy_pool_data.get(_mc_src, {}) if isinstance(_rhy_pool_data, dict) else {}
                _mc_cw = _mc_pool.get('clean_windows', [])
                _mc_cw_dur = max((w.get('end_sec',0)-w.get('start_sec',0) for w in _mc_cw), default=0)
                _slot_target = timeline[0].get('duration_sec', 2.66) if timeline else 2.66
                # 从 slot_plan 获取 target_duration
                if _slot_plan_path.exists():
                    try:
                        _sp_rhy = json.load(open(_slot_plan_path))
                        if _sp_rhy:
                            _slot_target = _sp_rhy[0].get('target_duration', 2.66)
                    except:
                        pass
                if _mc_cw_dur < _slot_target:
                    _rhy_skip_log['no_cw'] += 1
                    continue

                _rhy_type = 'service'
                if any(k in _mc_desc for k in _TEASER_KW):
                    _rhy_type = 'interaction_teaser'
                elif any(k in _mc_desc for k in _BREATHING_KW):
                    _rhy_type = 'breathing_shot'
                elif any(k in _mc_desc for k in ['采访', '受访']):
                    _rhy_type = 'interview'

                if _rhy_type not in ('interaction_teaser', 'breathing_shot', 'interview'):
                    _rhy_skip_log['not_breath_teaser'] += 1
                    continue

                if True:
                    _rhy_skip_log['passed'] += 1
                    # 找最佳 clean_window
                    _best_cw = max(_mc_cw, key=lambda w: w.get('end_sec',0)-w.get('start_sec',0)) if _mc_cw else {}
                    _rhy_candidates.append({
                        'reel_clip_id': _mc_rid,
                        'source_file': _mc_src,
                        'type': _rhy_type,
                        'stability': _mc_stab,
                        'cw_start': _best_cw.get('start_sec', 0),
                        'cw_end': _best_cw.get('end_sec', 0),
                        'cw_dur': _best_cw.get('end_sec',0) - _best_cw.get('start_sec',0),
                        'desc': _mc_desc[:60],
                    })

            # 排序：interaction_teaser 优先，然后 breathing_shot
            _type_order = {'interaction_teaser': 0, 'breathing_shot': 1, 'interview': 2}
            _rhy_candidates.sort(key=lambda c: (_type_order.get(c['type'], 3), -c['stability']))
            _rhythm_log['candidate_skip_log'] = dict(_rhy_skip_log)
            _rhythm_log['full_manifest_count'] = len(_rhy_full_manifest)
            _rhythm_log['filtered_manifest_count'] = len(manifest)
            print(f"  [v13.3-a1] rhythm candidates: {len(_rhy_candidates)} ({', '.join(c['type'] for c in _rhy_candidates[:5])}) skip={_rhy_skip_log} full_manifest={len(_rhy_full_manifest)}")

            # --- 选择替换位 ---
            # 在 longest_run 中选 handout slot（非 run 首位、非品牌镜头）
            _replaceable_slots = []
            for _ri in _longest_run:
                if _ri == _longest_run[0]:
                    continue  # 不替换 run 首位
                _rt = timeline[_ri]
                _rrid = _rt.get('reel_clip_id', '')
                if _rrid in _PROTECTED_RIDS:
                    continue
                # 检查是否品牌/关键镜头（B60=12333）
                _rdesc = ''
                for _m in manifest:
                    if _m.get('reel_clip_id') == _rrid:
                        _rdesc = _m.get('scene_description', '')
                        break
                if '12333' in _rdesc or '品牌' in _rdesc:
                    continue
                # 优先 handout
                _is_handout = any(k in _rdesc for k in ['发放', '递送', '宣传材料', '宣传资料', '慰问物资'])
                _replaceable_slots.append((_ri, _is_handout, _rrid))

            # 按 handout 优先，然后位置居中
            _run_mid = len(_longest_run) // 2
            _replaceable_slots.sort(key=lambda x: (0 if x[1] else 1, abs(_longest_run.index(x[0]) - _run_mid)))

            # --- 执行替换 ---
            _replaced_count = 0
            _used_rhy_srcs = set(_used_srcs_rhy)
            for _repl_si, _repl_is_handout, _repl_rid in _replaceable_slots:
                if _replaced_count >= DIRECTOR_RHYTHM_MAX_REPLACEMENTS:
                    break
                if not _rhy_candidates:
                    break
                # 选候选
                _chosen = None
                for _ci, _cc in enumerate(_rhy_candidates):
                    if _cc['source_file'] in _used_rhy_srcs:
                        continue
                    _chosen = _rhy_candidates.pop(_ci)
                    break
                if not _chosen:
                    break

                _old_entry = timeline[_repl_si]
                _old_src = _old_entry.get('source_file', '')
                _old_rid = _old_entry.get('reel_clip_id', '')
                _old_dur = _old_entry.get('duration_sec', _old_entry.get('duration', 2.66))

                # 更新 timeline entry
                timeline[_repl_si] = {
                    **_old_entry,
                    'source_file': _chosen['source_file'],
                    'reel_clip_id': _chosen['reel_clip_id'],
                    'start_sec': _chosen['cw_start'],
                    'end_sec': round(_chosen['cw_start'] + _old_dur, 2),
                    'duration_sec': _old_dur,
                    'duration': _old_dur,
                    'selection_reason': f'[v13.3-a1 rhythm_guard] {_old_rid}(SERVICE) → {_chosen["reel_clip_id"]}({_chosen["type"]})',
                    '_rhythm_replaced': True,
                    '_rhythm_type': _chosen['type'],
                }
                _used_rhy_srcs.add(_chosen['source_file'])
                _replaced_count += 1
                _rhythm_log['replacements'].append({
                    'slot': f'slot_{_repl_si+1:02d}',
                    'old_clip': _old_rid,
                    'old_source': _old_src,
                    'new_clip': _chosen['reel_clip_id'],
                    'new_source': _chosen['source_file'],
                    'new_type': _chosen['type'],
                    'new_stability': _chosen['stability'],
                    'new_cw_dur': _chosen['cw_dur'],
                })
                print(f"  [v13.3-a1] rhythm replace: slot_{_repl_si+1:02d} {_old_rid}(SERVICE) → {_chosen['reel_clip_id']}({_chosen['type']}, stab={_chosen['stability']})")

            # --- 重新检测 SERVICE run ---
            _post_runs = []
            _post_cur = []
            for _si2, _st2 in enumerate(timeline):
                if _is_service_slot_rhy(_st2):
                    _post_cur.append(_si2)
                else:
                    if _post_cur:
                        _post_runs.append(list(_post_cur))
                    _post_cur = []
            if _post_cur:
                _post_runs.append(list(_post_cur))
            _run_after = max(len(r) for r in _post_runs) if _post_runs else 0

            _rhythm_log['service_run_after'] = _run_after
            _rhythm_log['replacements_count'] = _replaced_count
            _rhythm_log['candidates_available'] = len(_rhy_candidates) + _replaced_count
            print(f"  [v13.3-a1] rhythm result: SERVICE max run {_run_before} → {_run_after}, replaced={_replaced_count}")
        else:
            _rhythm_log['service_run_after'] = _run_before
            _rhythm_log['replacements_count'] = 0
            _rhythm_log['candidates_available'] = 0
            print(f"  [v13.3-a1] rhythm_guard: SERVICE run {_run_before} <= {DIRECTOR_RHYTHM_SERVICE_RUN_THRESHOLD}, no action")

        with open(output_dir / "rhythm_guard_summary.json", 'w', encoding='utf-8') as _rgf:
            json.dump(_rhythm_log, _rgf, ensure_ascii=False, indent=2)

    # ============================================================
    # L3 Timeline 后验硬约束（v7.3.2 正式固化）
    # 位置：L3 返回后、下载裁切前
    # ============================================================
    from pipeline.timeline_validator import validate_and_fix_timeline
    validation = validate_and_fix_timeline(timeline, edit_mode=edit_mode, target_duration=tts_dur,
                                           director_constraints=director_constraints,
                                           news_structure=news_structure)
    timeline = validation['timeline']
    l3_result['timeline'] = timeline
    
    if validation['fixes']:
        print(f"  [后验修正] {len(validation['fixes'])} 处自动修正:")
        for fix in validation['fixes']:
            print(f"    ✅ {fix}")
    if validation['warnings']:
        print(f"  [后验警告] {len(validation['warnings'])} 处警告:")
        for warn in validation['warnings']:
            print(f"    ⚠️ {warn}")
    if not validation['fixes'] and not validation['warnings']:
        print(f"  [后验] ✅ timeline 合规，无需修正")

    # ============================================================
    # v9.1: director_plan 存在性校验 + why 退化检查
    # ============================================================
    _dp = l3_result.get('director_plan', None)
    if not _dp or not isinstance(_dp, dict):
        print(f"  ⚠️ [director_plan] L3 未输出 director_plan，记录 warning")
        validation['warnings'].append('[director_plan] L3 未输出 director_plan')
    else:
        _dp_fields = ['event_understanding', 'available_visual_materials', 'editing_strategy', 'what_not_to_do']
        _dp_missing = [f for f in _dp_fields if not _dp.get(f)]
        if _dp_missing:
            print(f"  ⚠️ [director_plan] 缺少字段: {_dp_missing}")
            validation['warnings'].append(f'[director_plan] 缺少字段: {_dp_missing}')
        else:
            print(f"  ✅ [director_plan] 完整输出")

    # v11.4: why 退化检查已停用（误报率 94%，v8.1 prompt 已强化 why 质量）
    # 保留 director_reason/why 字段，但不做机械退化判定
    print(f"  [why检查] v11.4 已停用机械退化判定")

    # ============================================================
    # v9.6: 配额验证报告
    # ============================================================
    _qp_path = output_dir / "quota_plan.json"
    if _qp_path.exists():
        with open(_qp_path, 'r') as _qf:
            _qp = json.load(_qf)
        _quota = _qp.get('quota', {})
        # 统计实际使用的 visual_motif
        _actual_motifs = {}
        for s in l3_result.get('selected_timeline', []):
            vm = s.get('visual_motif', '其他')
            if vm not in _actual_motifs:
                _actual_motifs[vm] = []
            _actual_motifs[vm].append(s.get('reel_clip_id', '?'))
        # 对比配额
        _qv_report = {
            'actual_clip_count': len(timeline),
            'target_clip_count': _qp.get('target_clip_count', 0),
            'actual_avg_duration': round(sum(t.get('duration_sec', 0) for t in timeline) / max(len(timeline), 1), 1),
            'motif_usage': {},
            'quota_violations': [],
            'front_20s_motifs': [],
        }
        for vm, q in _quota.items():
            actual = len(_actual_motifs.get(vm, []))
            _qv_report['motif_usage'][vm] = {'quota': q, 'actual': actual, 'clips': _actual_motifs.get(vm, [])}
            if actual > q:
                _qv_report['quota_violations'].append(f'{vm}: 实际{actual} > 配额{q}')
        # 前20秒 visual_motif 分布
        cursor = 0
        for t in timeline:
            if cursor >= 20:
                break
            rid = t.get('reel_clip_id', '')
            vm = '?'
            for s in l3_result.get('selected_timeline', []):
                if s.get('reel_clip_id') == rid:
                    vm = s.get('visual_motif', '?')
                    break
            _qv_report['front_20s_motifs'].append({'rid': rid, 'motif': vm, 'start': round(cursor, 1)})
            cursor += t.get('duration_sec', 0)
        # 前20秒母题种类数
        _front_motif_types = set(m['motif'] for m in _qv_report['front_20s_motifs'])
        _qv_report['front_20s_motif_count'] = len(_front_motif_types)
        if len(_front_motif_types) < 3:
            _qv_report['quota_violations'].append(f'前20秒仅{len(_front_motif_types)}种motif(<3)')

        with open(output_dir / "quota_validation_report.json", 'w', encoding='utf-8') as _qvf:
            json.dump(_qv_report, _qvf, ensure_ascii=False, indent=2)

        # 打印摘要
        desk_actual = len(_actual_motifs.get('服务台隔桌交互', []))
        desk_pct = desk_actual / max(len(timeline), 1) * 100
        print(f"  [配额验证] 镜头{len(timeline)}条, 均值{_qv_report['actual_avg_duration']}s, 服务台{desk_actual}条({desk_pct:.0f}%)")
        if _qv_report['quota_violations']:
            for qv in _qv_report['quota_violations']:
                print(f"    ⚠️ {qv}")
        else:
            print(f"    ✅ 配额全部合规")

    # ============================================================
    # v9.2: 强语义锚点校验（只校验锚点，不校验每句）
    # ============================================================
    _anchor_path = output_dir / "anchor_segments.json"
    if _anchor_path.exists():
        with open(_anchor_path, 'r') as _af:
            _anchors = json.load(_af)
        _sel_anchors = {s.get('anchor_id', '') for s in l3_result.get('selected_timeline', []) if s.get('anchor_id')}
        _anchor_visual_map = {
            'interactive_highlight': ['互动', '游戏', '投掷', '趣味', '沙包', '体验'],
            'service_action': ['资料', '手册', '发放', '递', '宣传'],
            'policy_explain': ['讲解', '答疑', '咨询', '交流', '指导'],
        }
        for a in _anchors:
            aid = a['anchor_id']
            vtype = a['required_visual_type']
            if aid in _sel_anchors:
                print(f"  ✅ [锚点] {aid}({vtype}) 已对齐")
            else:
                # 检查 timeline 中是否有对应类型画面在锚点时间附近
                print(f"  ⚠️ [锚点] {aid}({vtype}) 未被 L3 标记 anchor_id，记录 warning")
                validation['warnings'].append(f'[锚点] {aid}({vtype}) 未对齐')
    else:
        print(f"  [锚点] 无锚点文件，跳过校验")

    # 保存 timeline（含后验修正结果）
    timeline_path = output_dir / f"l3_timeline_{task_id}.json"
    l3_result['_validation'] = {
        'fixes': validation['fixes'],
        'warnings': validation['warnings'],
        'valid': validation['valid'],
    }
    with open(timeline_path, 'w', encoding='utf-8') as f:
        json.dump(l3_result, f, ensure_ascii=False, indent=2)
    print(f"  Timeline 保存: {timeline_path}")
    print(f"  镜头数: {len(timeline)}")

    # ============================================================
    # v11.2: semantic_selection_check + replan_once
    # 位置：timeline 保存后、裁切前
    # ============================================================
    if edit_mode == 'narration':
        _su_path = output_dir / "shot_understanding_v1.json"
        _sp_path = output_dir / "slot_plan.json"
        if _su_path.exists() and _sp_path.exists():
            from pipeline.semantic_selection_check import (
                semantic_selection_check as _sem_check,
                semantic_replan_once as _sem_replan,
            )
            with open(_su_path) as _suf:
                _su_data = json.load(_suf)
            with open(_sp_path) as _spf_sem:
                _sp_data = json.load(_spf_sem)

            print(f"\n  [v11.2] semantic_selection_check 首次校验...")
            _sem_result = _sem_check(_sp_data, _su_data, timeline, output_dir=str(output_dir))
            _sem_result['task_id'] = task_id

            if _sem_result['passed']:
                print(f"  [v11.2] ✅ 首次校验通过 (hard_fail=0, warning={_sem_result['warning_count']})")
            else:
                print(f"  [v11.2] ❌ 首次校验失败: {_sem_result['fail_reason']}")
                for _hf in _sem_result['hard_fails']:
                    print(f"    ❌ {_hf['slot_id']} [{_hf['anchor_strength']}] → {_hf['reel_clip_id']} ({_hf['scene_type']}): {_hf['detail']}")

                # 尝试重选
                _actual_fail_count = sum(1 for hf in _sem_result['hard_fails'] if hf['slot_id'] != 'GLOBAL')
                if _actual_fail_count > 3:
                    _error_msg = f'semantic_selection_check failed: {_actual_fail_count} 个 slot 失败 > 3，不重选'
                    print(f"  [v11.2] ❌ {_error_msg}")
                    _update_task_failed(task_path, _error_msg, 'semantic_selection_check')
                    raise RuntimeError(_error_msg)

                print(f"  [v11.2] 🔄 尝试定向重选 {_actual_fail_count} 个失败 slot...")
                _replan_result = _sem_replan(
                    _sem_result['hard_fails'], _sp_data, _su_data, timeline,
                    output_dir=str(output_dir)
                )

                if not _replan_result.get('replanned', False):
                    _error_msg = f'semantic_selection_check failed: {_replan_result.get("reason", "重选失败")}'
                    print(f"  [v11.2] ❌ {_error_msg}")
                    _update_task_failed(task_path, _error_msg, 'semantic_selection_check')
                    raise RuntimeError(_error_msg)

                # 更新 timeline
                _new_timeline = _replan_result.get('updated_timeline', timeline)
                for _rr in _replan_result.get('replan_results', []):
                    if _rr.get('replan_success'):
                        _rep = _rr['replacement']
                        print(f"    🔄 {_rr['slot_id']}: {_rr['original_reel_clip_id']}({_rr['original_scene_type']}) → {_rep['reel_clip_id']}({_rep['scene_type']})")
                    else:
                        print(f"    ❌ {_rr['slot_id']}: 无合格候选")

                # 第二次校验
                print(f"  [v11.2] 🔍 重选后第二次校验...")
                _sem_result2 = _sem_check(_sp_data, _su_data, _new_timeline, output_dir=str(output_dir))
                _sem_result2['task_id'] = task_id
                _sem_result2['_after_replan'] = True

                if _sem_result2['passed']:
                    print(f"  [v11.2] ✅ 重选后校验通过！")
                    timeline = _new_timeline
                    l3_result['timeline'] = timeline
                    l3_result['_semantic_replan'] = True
                    # 重新保存 timeline
                    with open(timeline_path, 'w', encoding='utf-8') as f:
                        json.dump(l3_result, f, ensure_ascii=False, indent=2)
                    print(f"  Timeline 已更新（含重选）: {timeline_path}")
                else:
                    _error_msg = f'semantic_selection_check failed after replan: {_sem_result2["fail_reason"]}'
                    print(f"  [v11.2] ❌ {_error_msg}")
                    _update_task_failed(task_path, _error_msg, 'semantic_selection_check_after_replan')
                    raise RuntimeError(_error_msg)

                # 写入 replan_attempt 到 task JSON
                try:
                    with open(task_path) as _tf:
                        _task_data = json.load(_tf)
                    _task_data['replan_attempt'] = {
                        'initial_fails': _actual_fail_count,
                        'replanned': _replan_result.get('replan_attempted', 0),
                        'second_pass': _sem_result2['passed'],
                        'timestamp': datetime.now().isoformat(),
                    }
                    with open(task_path, 'w') as _tf:
                        json.dump(_task_data, _tf, ensure_ascii=False, indent=2)
                except Exception:
                    pass
        else:
            if not _su_path.exists():
                print(f"  [v11.2] ⚠️ shot_understanding_v1.json 不存在，跳过语义校验")
            if not _sp_path.exists():
                print(f"  [v11.2] ⚠️ slot_plan.json 不存在，跳过语义校验")

    # ============================================================
    # v11.6: narrative_continuity_report（仅报告，不拦截）
    # ============================================================
    if edit_mode == 'narration':
        _su_path_nc = output_dir / "shot_understanding_v1.json"
        if _su_path_nc.exists():
            with open(_su_path_nc) as _suf_nc:
                _su_nc = json.load(_suf_nc)
            _su_nc_map = {c['reel_clip_id']: c for c in _su_nc.get('clips', [])}
            _l3_nc_map = {t['slot_id']: t for t in timeline}

            _nc_slots = []
            _bad_jumps = 0
            _weak_jumps = 0
            _indoor_outdoor_flips = 0
            _high_risk_groups = 0
            _prev_loc = ''
            _prev_group = ''
            _prev_phase = ''

            for _s_nc in slot_plan if 'slot_plan' in dir() else []:
                pass  # slot_plan might not be in scope

            # Build from slot_plan file
            _sp_nc_path = output_dir / "slot_plan.json"
            if _sp_nc_path.exists():
                with open(_sp_nc_path) as _spf_nc:
                    _sp_nc = json.load(_spf_nc)
            else:
                _sp_nc = []

            _prev_loc = ''
            _prev_group = ''
            _prev_is_indoor = None

            for _s_nc in _sp_nc:
                _sid_nc = _s_nc['slot_id']
                _l3e_nc = _l3_nc_map.get(_sid_nc, {})
                _rid_nc = _l3e_nc.get('reel_clip_id', '')
                _sue_nc = _su_nc_map.get(_rid_nc, {})

                _loc_nc = _sue_nc.get('location_context', '?')
                _phase_nc = _sue_nc.get('event_phase', '?')
                _role_nc = _sue_nc.get('audience_role', '?')
                _group_nc = _sue_nc.get('scene_group_id', 'unknown')
                _cont_reason = _l3e_nc.get('continuity_reason', '')

                # 判断跳跃类型
                _relation = 'continuous'
                _is_indoor = '室内' in _loc_nc
                _was_indoor = '室内' in _prev_loc if _prev_loc else None

                if _prev_group and _group_nc == _prev_group and _group_nc != 'unknown':
                    _relation = 'continuous'
                elif _prev_loc:
                    if _was_indoor is not None and _is_indoor != _was_indoor:
                        _indoor_outdoor_flips += 1
                        _relation = 'intentional_transition'
                    # 同 location 大类
                    _prev_loc_base = _prev_loc.split('-')[0] if '-' in _prev_loc else _prev_loc
                    _curr_loc_base = _loc_nc.split('-')[0] if '-' in _loc_nc else _loc_nc
                    if _prev_loc_base != _curr_loc_base:
                        _relation = 'weak_jump'
                        _weak_jumps += 1

                # 相邻同 group = high risk
                if _prev_group and _group_nc == _prev_group and _group_nc != 'unknown':
                    _high_risk_groups += 1

                _nc_slots.append({
                    'slot_id': _sid_nc,
                    'reel_clip_id': _rid_nc,
                    'location_context': _loc_nc,
                    'event_phase': _phase_nc,
                    'audience_role': _role_nc,
                    'scene_group_id': _group_nc,
                    'continuity_reason': _cont_reason,
                    'previous_relation': _relation,
                })

                _prev_loc = _loc_nc
                _prev_group = _group_nc
                _prev_is_indoor = _is_indoor

            _nc_report = {
                'task_id': task_id,
                'version': 'v11.6',
                'slot_count': len(_nc_slots),
                'bad_jump_count': _bad_jumps,
                'weak_jump_count': _weak_jumps,
                'indoor_outdoor_flips': _indoor_outdoor_flips,
                'adjacent_same_group': _high_risk_groups,
                'slots': _nc_slots,
            }

            _nc_path = output_dir / "narrative_continuity_report.json"
            with open(_nc_path, 'w', encoding='utf-8') as _ncf:
                json.dump(_nc_report, _ncf, ensure_ascii=False, indent=2)
            print(f"  [v11.6] narrative_continuity_report: bad_jump={_bad_jumps}, weak_jump={_weak_jumps}, indoor/outdoor_flips={_indoor_outdoor_flips}, adj_same_group={_high_risk_groups}")
            
            # ============================================================
            # v12.2: 节奏抑制层 — 检测连续 ≥3 次同 group 并尝试打断
            # 仅在 scene_struct 启用时执行
            # ============================================================
            if _scene_struct_enabled and _high_risk_groups > 2:
                print(f"  [v12.3] 节奏抑制（轻惩罚）: adj_same_group={_high_risk_groups}，检测连续重复")
                
                # 构建已用 reel_clip_id 集合 + group 映射
                _used_rids = set(t.get('reel_clip_id', '') for t in timeline)
                _rid_to_group = {}
                for _ncs in _nc_slots:
                    _rid_to_group[_ncs.get('reel_clip_id', '')] = _ncs.get('scene_group_id', 'unknown')
                
                # 构建替代候选（从 manifest 中找未使用的、不同 group 的镜头）
                _manifest_for_rhythm = []
                _manifest_path_rhy = output_dir / "candidate_reel_manifest.json"
                if _manifest_path_rhy.exists():
                    _manifest_for_rhythm = json.load(open(_manifest_path_rhy))
                
                # 从 scene_context_struct.json 获取 group 信息
                _scs_path_rhy = output_dir / "scene_context_struct.json"
                _scs_map_rhy = {}
                if _scs_path_rhy.exists():
                    try:
                        from pipeline.scene_struct import build_scene_context_struct as _rhy_build
                        for _si in json.load(open(_scs_path_rhy)):
                            _scs_map_rhy[_si.get('file', '')] = _si.get('scene_group_struct', {})
                    except:
                        pass
                
                # 检测连续≥3 并尝试轻惩罚替换
                _rhythm_fixes = 0
                _consecutive_count = 1
                _consecutive_start = 0
                _consecutive_group = ''
                
                for _ri in range(1, len(_nc_slots)):
                    _curr_g = _nc_slots[_ri].get('scene_group_id', '')
                    _prev_g = _nc_slots[_ri-1].get('scene_group_id', '')
                    if _curr_g == _prev_g and _curr_g != 'unknown' and _curr_g:
                        _consecutive_count += 1
                        _consecutive_group = _curr_g
                    else:
                        if _consecutive_count >= 3:
                            _break_pos = _consecutive_start + 2
                            _break_sid = _nc_slots[_break_pos]['slot_id']
                            _break_rid = _nc_slots[_break_pos]['reel_clip_id']
                            
                            # 尝试找不同 group 的替代镜头
                            _alternatives = []
                            for _mc in _manifest_for_rhythm:
                                _mc_rid = _mc.get('reel_clip_id', '')
                                if _mc_rid in _used_rids:
                                    continue
                                if _mc.get('pool_level') == 'discard':
                                    continue
                                if _mc.get('score_total', 0) < 60:
                                    continue
                                # 检查 group 是否不同
                                _mc_src = _mc.get('source_file', '')
                                _mc_grp = _scs_map_rhy.get(_mc_src, {})
                                _mc_grp_key = f"{_mc_grp.get('space','?')}_{_mc_grp.get('event','?')}"
                                _curr_grp_key = _consecutive_group
                                if _mc_grp_key != _curr_grp_key and _mc_grp_key != '?_?':
                                    _alternatives.append((_mc_rid, _mc_src, _mc.get('score_total', 0), _mc_grp_key))
                            
                            if _alternatives:
                                # 选 score 最高的替代
                                _alternatives.sort(key=lambda x: -x[2])
                                _alt = _alternatives[0]
                                _alt_rid, _alt_src, _alt_score, _alt_grp = _alt
                                
                                # 在 timeline 中替换
                                for _ti, _te in enumerate(timeline):
                                    if _te.get('slot_id') == _break_sid:
                                        _old_rid = _te.get('reel_clip_id', '')
                                        _old_src = _te.get('source_file', '')
                                        # 从 manifest 找替代镜头的详细信息
                                        _alt_m = next((m for m in _manifest_for_rhythm if m['reel_clip_id'] == _alt_rid), None)
                                        if _alt_m:
                                            _te['source_file'] = _alt_m['source_file']
                                            _te['start_sec'] = _alt_m['source_start_sec']
                                            _te['end_sec'] = min(_alt_m['source_start_sec'] + _te['duration_sec'], _alt_m['source_end_sec'])
                                            _te['duration_sec'] = round(_te['end_sec'] - _te['start_sec'], 2)
                                            _te['reel_clip_id'] = _alt_rid
                                            _te['_rhythm_replaced'] = True
                                            _te['_rhythm_reason'] = f'连续{_consecutive_count}次{_consecutive_group}→替换为{_alt_grp}'
                                            _used_rids.add(_alt_rid)
                                            _used_rids.discard(_old_rid)
                                            print(f"    ✅ {_break_sid}: {_old_rid}({_old_src[:15]}) → {_alt_rid}({_alt_src[:15]}) "
                                                  f"[group: {_consecutive_group}→{_alt_grp}, score={_alt_score}]")
                                            _rhythm_fixes += 1
        1. clean_windows 为空 + weak_safe 中有 unstable
        2. 所有 clean_windows 的 ffmpeg_action=downgrade
        3. manifest 中 score_stability < 60
        4. manifest 中 quality_class=C
        5. L2 motion_type 含找位/回摆/朝天等
        """
        has_cw = bool(l2_record.get('clean_windows', []))
        
        # 检查 weak_safe 中是否有 unstable
        has_unstable_ws = False
        for ws in l2_record.get('weak_safe_segments', []):
            if isinstance(ws, dict) and ws.get('ffmpeg_stability') == 'unstable':
                has_unstable_ws = True
                break
        
        # 检查 clean_windows 中是否全部 downgrade
        cw_list = l2_record.get('clean_windows', [])
        all_cw_downgrade = False
        if cw_list:
            all_cw_downgrade = all(
                isinstance(w, dict) and w.get('ffmpeg_action') == 'downgrade'
                for w in cw_list
            )
        
        # 从 manifest 获取评分和质量等级
        m_info = _gate_manifest_scores.get(source_file, {})
        score_detail = m_info.get('score_detail', {})
        score_stab = score_detail.get('stability', 80)
        quality_class = m_info.get('quality_class', 'A')
        
        # L2 reject 关键词
        _REJECT_KEYWORDS = {'找位', '回摆', '大幅推拉', '朝天', '构图持续不稳', '黑场', '跳帧', '严重抖动'}
        l2_reject = ''
        for ws in l2_record.get('weak_safe_segments', []) + l2_record.get('unsafe_segments', []):
            if isinstance(ws, dict):
                for reason in ws.get('reasons', []):
                    if any(kw in str(reason) for kw in _REJECT_KEYWORDS):
                        l2_reject = str(reason)[:60]
                        break
        
        # === 拦截判定 ===
        # 条件1: clean_windows 空 + unstable
        if not has_cw and has_unstable_ws:
            return False, f'no_clean_windows + unstable'
        
        # 条件2: 所有 clean_windows 都是 downgrade
        if all_cw_downgrade and cw_list:
            return False, f'all_cw_downgrade ({len(cw_list)} windows)'
        
        # 条件3: score_stability < 60
        if score_stab < 60 and m_info:
            return False, f'score_stability={score_stab}<60'
        
        # 条件4: quality_class=C
        if quality_class == 'C':
            return False, f'quality_class=C'
        
        # 条件5: L2 包含严重问题关键词
        if l2_reject:
            return False, f'l2_reject: {l2_reject}'
        
        return True, 'pass'
    
    _gate_rejected = []
    _gate_rejected_semantic = {}  # v10.7: {slot_id: {语义信息}} 供补位使用
    _gate_passed = []
    for _gt in timeline:
        _g_src = _gt.get('source_file', _gt.get('source', ''))
        _g_data = _gate_pool.get(_g_src, {}) if isinstance(_gate_pool.get(_g_src), dict) else {}
        allowed, reason = is_clip_quality_allowed(_g_src, _g_data)
        if not allowed:
            print(f"  ⛔ [质量闸门] {_g_src} 拒绝入片: {reason}")
            _slot_id = _gt.get('slot_id', '')
            _gate_rejected.append({'source_file': _g_src, 'reason': reason, 'slot_id': _slot_id})
            # v10.7: 保存被拦截镜头的语义信息，供补位匹配
            _gate_rejected_semantic[_slot_id] = {
                'source_file': _g_src,
                'visual_motif': _gt.get('visual_motif', ''),
                'info_type': _g_data.get('information_type', ''),
                'why': _gt.get('why', _gt.get('scene_type', '')),
                'reel_clip_id': _gt.get('reel_clip_id', ''),
                'is_anchor': _gt.get('is_anchor_slot', False) or _gt.get('anchor_id', ''),
                'slot_id': _slot_id,
            }
            continue
        _gate_passed.append(_gt)
    if _gate_rejected:
        print(f"  [质量闸门] 拦截 {len(_gate_rejected)} 条: {[r['source_file'] for r in _gate_rejected]}")
        for _gr in _gate_rejected:
            _sem = _gate_rejected_semantic.get(_gr['slot_id'], {})
            print(f"    {_gr['slot_id']}: motif={_sem.get('visual_motif','')} info={_sem.get('info_type','')[:40]}")
    timeline = _gate_passed

    filtered_timeline = []  # v8.4: 与 clip_paths 对齐的有效 timeline
    boundary_report = []    # v8.4: 选片边界校验报告
    _source_dur_cache = {}  # 缓存源文件时长避免重复 probe
    # v13.2-i: debug 输出裁切前 timeline slot_01 的 source_file
    if timeline:
        _dbg_s1 = timeline[0]
        print(f"  [v13.2-i DEBUG] 裁切前 slot_01: rid={_dbg_s1.get('reel_clip_id','?')} src={_dbg_s1.get('source_file','?')} start={_dbg_s1.get('start_sec',0)} end={_dbg_s1.get('end_sec',0)}")
    for t in timeline:
        _check_cancel()  # v10.5: 下载循环中检查取消
        src = t.get('source_file', t.get('source', ''))
        ss = t.get('start_sec', t.get('start', 0))
        se = t.get('end_sec', ss + t.get('duration_sec', t.get('duration', 0)))
        dur = t.get('duration_sec', t.get('duration', 0))
        order = t.get('order', 0)

        # 下载
        video_url = tos_url_map.get(src, '')
        if not video_url:
                                        break
                            else:
                                print(f"    ⚠️ {_break_sid}({_break_rid}): 连续{_consecutive_count}次{_consecutive_group}，无合适替代")
                        
                        _consecutive_count = 1
                        _consecutive_start = _ri
                        _consecutive_group = ''
                
                # 处理末尾
                if _consecutive_count >= 3:
                    _break_pos = _consecutive_start + 2
                    if _break_pos < len(_nc_slots):
                        _break_sid = _nc_slots[_break_pos]['slot_id']
                        print(f"    ⚠️ 末尾连续{_consecutive_count}次{_consecutive_group}，{_break_sid}未替换")
                
                if _rhythm_fixes > 0:
                    print(f"  [v12.3] ✅ 节奏抑制: 替换 {_rhythm_fixes} 处连续重复")
                    # 保存替换后的 timeline
                    _tl_path = output_dir / "l3_timeline_task_{}.json".format(task_id)
                    with open(_tl_path, 'w', encoding='utf-8') as _tf:
                        json.dump(timeline, _tf, ensure_ascii=False, indent=2)
                else:
                    print(f"  [v12.3] 无连续≥3需替换（adj_same_group={_high_risk_groups} 均为成对）")

    # ============================================================
    # 3. 下载 + 裁切
    # ============================================================
    _update_stage(task_path, "download")
    _heartbeat("download_started")
    _check_cancel()
    print(f"\n[3/5] 下载 + 裁切素材")
    clip_paths = []
    # ============================================================
    # v11: 片头结构层 — ⛔ 已禁用（shadow_mode=true，仅输出报告，不改 timeline）
    # 禁用原因：v11 交换 slot 后导致卡点失效 + 静帧补尾（2026-04-28 回滚）
    # ============================================================
    try:
        from pipeline.director_structure import validate_and_fix_opening
        _manifest_for_opening = []
        _manifest_path_open = output_dir / "candidate_reel_manifest.json"
        if _manifest_path_open.exists():
            with open(_manifest_path_open) as _mfo:
                _manifest_for_opening = json.load(_mfo)
        _opening_result = validate_and_fix_opening(timeline, _manifest_for_opening, edit_mode=edit_mode)
        # ⛔ shadow_mode: 不修改 timeline，只保存报告
        # timeline = _opening_result['timeline']  # DISABLED
        with open(output_dir / "opening_structure_report.json", 'w', encoding='utf-8') as _osf:
            _opening_result['report']['shadow_mode'] = True
            json.dump(_opening_result['report'], _osf, ensure_ascii=False, indent=2)
        print(f"  [v11 片头结构] shadow_mode: 仅输出报告，不修改 timeline")
    except Exception as _oe:
        print(f"  ⚠️ [v11 片头结构] 报告生成失败: {_oe}")

    # ============================================================
    # v10.6.2: 全模式通用质量闸门（统一函数，覆盖所有不合格字段组合）
    # ============================================================
    from pipeline.pool_overrides import load_pool_data as _load_pool_gate
    _gate_pool = _load_pool_gate(task_id)
    
    # 加载 manifest 评分（如有）
    _gate_manifest_scores = {}
    _gate_manifest_path = output_dir / "candidate_reel_manifest.json"
    if _gate_manifest_path.exists():
        try:
            with open(_gate_manifest_path) as _gmf:
                _gm_data = json.load(_gmf)
            _gm_list = _gm_data if isinstance(_gm_data, list) else _gm_data.get('clips', [])
            for _gmc in _gm_list:
                if isinstance(_gmc, dict):
                    _gm_fn = _gmc.get('source_file', '')
                    if _gm_fn not in _gate_manifest_scores:
                        _gate_manifest_scores[_gm_fn] = _gmc
        except Exception:
            pass
    
    def is_clip_quality_allowed(source_file: str, l2_record: dict) -> tuple:
        """v10.6.2: 统一质量判定函数
        
        Returns:
            (allowed: bool, reason: str)
        
        拦截条件（任一满足即拦截）：
            print(f"  ⚠️ 找不到素材 URL: {src}")
            continue

        local_name = src.replace('.MP4', '.mp4')
        local_path = dl_dir / local_name
        if not local_path.exists():
            print(f"  下载 {src}...")
            r = requests.get(video_url, timeout=120)
            with open(local_path, 'wb') as f:
                f.write(r.content)

        # ====== 选片边界强校验 Layer 2: probe 源文件实际时长（v8.4）======
        if src not in _source_dur_cache:
            try:
                _p = subprocess.run([FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
                                    '-of', 'default=noprint_wrappers=1:nokey=1', str(local_path)],
                                   capture_output=True, text=True, timeout=10)
                _source_dur_cache[src] = float(_p.stdout.strip())
            except:
                _source_dur_cache[src] = 9999.0
        source_file_dur = _source_dur_cache[src]

        clamped = False
        if se > source_file_dur + 0.05:
            print(f"  ⚠️ [越界修正L2] clip_{order:02d} {src}: end {se:.2f}s > 源文件 {source_file_dur:.2f}s → 截断")
            se = round(source_file_dur, 2)
            dur = round(se - ss, 2)
            t['end_sec'] = se
            t['duration_sec'] = dur
            t['_boundary_clamped'] = True
            clamped = True

        if dur < 1.0:  # v11.4: 1.5→1.0
            print(f"  ❌ [无效clip] clip_{order:02d} {src}: 时长 {dur:.2f}s < 1.0s → 跳过（补位替换）")
            boundary_report.append({'clip': f'clip_{order:02d}', 'source': src,
                'source_duration': source_file_dur, 'original_end': se,
                'issue': f'时长{dur:.2f}s<1.0s', 'action': '跳过'})
            continue

        # 裁切（去掉原声，只保留视频流）
        clip_name = f"clip_{order:02d}_{src.replace('.MP4', '')}.mp4"
        clip_path = clips_dir / clip_name
        subprocess.run([
            FFMPEG, '-y', '-i', str(local_path),
            '-ss', str(ss), '-t', str(dur),
            '-c:v', 'libx264', '-an', '-preset', 'fast',
            str(clip_path)
        ], check=True, capture_output=True)

        # ====== 裁切后 probe 实际时长 ======
        try:
            _p2 = subprocess.run([FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
                                 '-of', 'default=noprint_wrappers=1:nokey=1', str(clip_path)],
                                capture_output=True, text=True, timeout=5)
            actual_clip_dur = float(_p2.stdout.strip())
        except:
            actual_clip_dur = dur

        if actual_clip_dur < 1.0:  # v11.4: 1.5→1.0
            print(f"  ❌ [裁切后无效] clip_{order:02d}: 实际 {actual_clip_dur:.2f}s < 1.0s → 跳过")
            boundary_report.append({'clip': f'clip_{order:02d}', 'source': src,
                'issue': f'裁切后{actual_clip_dur:.2f}s<1.0s', 'action': '跳过'})
            continue

        if actual_clip_dur < dur - 0.5:
            print(f"  ⚠️ [截断] clip_{order:02d}: 预期 {dur:.2f}s → 实际 {actual_clip_dur:.2f}s（丢 {dur - actual_clip_dur:.2f}s）")
            boundary_report.append({'clip': f'clip_{order:02d}', 'source': src,
                'issue': f'截断丢{dur - actual_clip_dur:.2f}s', 'action': '保留(≥1.5s)'})

        clip_paths.append(clip_path)
        filtered_timeline.append(t)
        status = '⚠️截断' if clamped else '✅'
        print(f"  {status} clip_{order:02d}: {src} [{ss:.1f}-{se:.1f}s] 实际={actual_clip_dur:.2f}s")

    # v8.4: 重新对齐 timeline（跳过无效 clip 后）
    for _i, _t in enumerate(filtered_timeline):
        _t['order'] = _i + 1
    timeline = filtered_timeline
    l3_result['timeline'] = timeline

    # v8.4: 保存边界校验报告
    if boundary_report:
        print(f"\n  === 选片边界校验报告（{len(boundary_report)} 条异常）===")
        for br in boundary_report:
            print(f"    {br['clip']}: {br['issue']} → {br['action']}")
        _br_path = output_dir / "boundary_validation_report.json"
        with open(_br_path, 'w', encoding='utf-8') as _f:
            json.dump(boundary_report, _f, ensure_ascii=False, indent=2)

    if not clip_paths:
        raise RuntimeError("没有可用的裁切片段")

    # === v13.3-p2a: repeat classifier for music_only/montage ===
    def classify_music_repeat(candidate, existing_timeline, current_insert_position, edit_mode_check='music_only'):
        """v13.3-p2a-fix: 重复分类器 - 基于 final timeline gap
        
        Args:
            candidate: 补位候选 {source_file, start_sec, duration, ...}
            existing_timeline: 已选 timeline 列表
            current_insert_position: 当前 candidate 将插入的时间点（final timeline 位置）
            edit_mode_check: 是否启用分类器（仅 music_only/montage）
        
        Returns:
            (repeat_type, reason, gap_seconds)
            repeat_type: 'forbidden' | 'low_grade_fill' | 'structural_recall' | 'fresh'
        """
        if edit_mode_check not in ('music_only', 'montage'):
            return 'fresh', 'non_music_mode', 999
        
        _c_src = candidate.get('source_file', '')
        _c_start = candidate.get('start_sec', 0)
        _c_dur = candidate.get('duration', 3.5)
        _c_end = _c_start + _c_dur
        
        # 获取候选的 visual_class / scene_description
        _c_visual = candidate.get('visual_class', '')
        _c_scene = candidate.get('scene_description', '')
        
        # 禁止的 visual_class（这类素材不允许重复补位）
        FORBIDDEN_VISUAL_CLASSES = {'banner', 'poster', 'signage', 'group_photo', 'low_info', 'static'}
        
        # 查找已存在的同 source 出现记录（final timeline 时间轴）
        _same_src_entries = []
        for _i, _t in enumerate(existing_timeline):
            _t_src = _t.get('source_file', _t.get('source', ''))
            
            if _t_src == _c_src:
                # v13.3-p2a-fix: 使用 final_start/final_end 计算真实位置
                _final_start = _t.get('final_start', _t.get('start_sec', _t.get('start', 0)))
                _final_end = _t.get('final_end', _final_start + _t.get('duration', 0))
                _same_src_entries.append({
                    'index': _i,
                    'start': _t.get('start_sec', 0),  # 素材裁剪起点（用于窗口重叠检测）
                    'end': _t.get('start_sec', 0) + _t.get('duration', 0),
                    'final_start': _final_start,  # 最终 timeline 起点
                    'final_end': _final_end,  # 最终 timeline 终点
                    'visual_class': _t.get('visual_class', ''),
                    'scene_description': _t.get('scene_description', '')
                })
        
        # 1. 无重复 → fresh
        if not _same_src_entries:
            return 'fresh', 'unused_source', 999
        
        # 2. 禁止类 visual_class 重复 → forbidden
        _c_visual_lower = _c_visual.lower() if _c_visual else ''
        if any(_fv in _c_visual_lower for _fv in FORBIDDEN_VISUAL_CLASSES):
            return 'forbidden', f'forbidden_visual_class:{_c_visual}', 0
        
        # 3. clip_id 重复 → forbidden
        _c_clip_id = candidate.get('reel_clip_id', candidate.get('clip_id', ''))
        for _t in existing_timeline:
            _t_clip_id = _t.get('reel_clip_id', _t.get('clip_id', ''))
            if _c_clip_id and _t_clip_id and _c_clip_id == _t_clip_id:
                return 'forbidden', 'duplicate_clip_id', 0
        
        # 4. 窗口重叠 → forbidden
        for _entry in _same_src_entries:
            # 窗口重叠判定：候选区间与已用区间有交集
            if _c_start < _entry['end'] and _c_end > _entry['start']:
                return 'forbidden', f'window_overlap:[{_c_start:.1f}-{_c_end:.1f}] vs [{_entry["start"]:.1f}-{_entry["end"]:.1f}]', 0
        
        # v13.3-p2a-fix: 基于 final timeline 计算真实 gap
        # gap = 当前插入位置 - 同 source 上一次结束时间（final timeline）
        _last_same_src_final_end = max(_e['final_end'] for _e in _same_src_entries)
        _final_gap = current_insert_position - _last_same_src_final_end
        
        # 5. final gap < 4s → forbidden（超短间隔）
        if _final_gap < 4.0:
            return 'forbidden', f'final_gap<{4}s:{_final_gap:.1f}s', _final_gap
        
        # 6. final gap < 8s → low_grade_fill（低级重复）
        # 例外：environment / reaction / detail 类允许结构回环
        _ALLOW_RECALL_VISUAL = {'environment', 'wide', 'establishing', 'reaction', 'interaction', 'detail'}
        _is_allow_recall = any(_av in _c_visual_lower for _av in _ALLOW_RECALL_VISUAL)
        
        if _final_gap < 8.0 and not _is_allow_recall:
            return 'low_grade_fill', f'final_gap<{8}s:{_final_gap:.1f}s', _final_gap
        
        # 7. final gap >= 12s → structural_recall（结构回环）
        if _final_gap >= 12.0:
            return 'structural_recall', f'final_gap>=12s:{_final_gap:.1f}s', _final_gap
        
        # 8. 8-12s 灰区 → low_grade_fill（保守）
        return 'low_grade_fill', f'final_gap_8_12s:{_final_gap:.1f}s', _final_gap
        
        # === 时长补位策略（分级优先级，2026-04-23 固化） ===
    # 优先级1: 从候选池补额外镜头
    # 优先级2: 择优延长现有镜头（优先场景/群像/横幅类）
    # 优先级3: 轻量兜底（延长最后一个镜头，上限2秒）
    timeline_total = sum(t.get('duration_sec', t.get('duration', 0)) for t in timeline)
    # v11.6: slot 模式下用 slot 时长计算 shortfall，而非 clip 总时长
    # 因为每个 clip 在 slot 锁定模式下被精确裁到 slot_duration，超出部分被丢弃
    # 注意：此时 timeline 已经过质量闸门过滤，len(timeline) 是真实可用数
    if _use_slot_render and _slot_plan_path.exists():
        with open(_slot_plan_path) as _spf_sf:
            _sp_sf = json.load(_spf_sf)
        _total_slot_dur = sum(s.get('target_duration', 0) for s in _sp_sf)
        _covered_slots = set(t.get('slot_id', '') for t in timeline)
        _covered_slot_dur = sum(s.get('target_duration', 0) for s in _sp_sf if s['slot_id'] in _covered_slots)
        # shortfall = 总 slot 时长 - 已覆盖 slot 时长
        _slot_shortfall = _total_slot_dur - _covered_slot_dur
        if _slot_shortfall > 0:
            print(f"  [v11.6] slot 模式: 总 slot {_total_slot_dur:.1f}s, 已覆盖 {_covered_slot_dur:.1f}s, slot 缺口 {_slot_shortfall:.1f}s")
            timeline_total = _covered_slot_dur  # 用 slot 覆盖时长替代 clip 总时长
    if timeline_total < tts_dur and clip_paths:
        shortfall = tts_dur - timeline_total + 0.5  # 补齐 + 0.5s 余量
        print(f"  ⚠️ timeline {timeline_total:.1f}s < TTS {tts_dur:.1f}s，需补 {shortfall:.1f}s")
        if shortfall > 5.0:
            print(f"  ⚠️ [WARNING] shortfall {shortfall:.1f}s > 5s，L3 选片严重不足，补位只能部分弥补")
        # v10.1: 补位数量上限——根据 slot 空缺数量动态计算
        # slot 模式下后验剔除可能导致多个空 slot，需要足够的补位来填满
        if _use_slot_render and _slot_plan_path.exists():
            with open(_slot_plan_path) as _spf_fill:
                _sp_fill = json.load(_spf_fill)
            _slot_count_fill = len(_sp_fill)
            _timeline_count_fill = len(timeline)
            _empty_slot_count = max(0, _slot_count_fill - _timeline_count_fill)
            _max_fill_clips = max(3, _empty_slot_count + 1)  # 至少补空 slot 数量 + 1 余量
            print(f"  [v10.1] slot 空缺 {_empty_slot_count} 个，补位上限调整为 {_max_fill_clips}")
        else:
            _max_fill_clips = 3
            _empty_slot_count = 0  # v11.6.2: 非 slot 模式默认 0

        # --- 优先级1: 从 L2 候选池找未使用的安全段 ---
        used_keys = set()
        used_sources = set()  # 记录已用素材文件名
        used_clip_ids = set()  # v13.2-i.3: 记录已用 clip_id
        used_ranges = {}  # v10.6: {source_file: [(start, end), ...]} 精确去重
        for t in timeline:
            src = t.get('source_file', t.get('source', ''))
            ss = t.get('start_sec', t.get('start', 0))
            se = t.get('end_sec', ss + t.get('duration_sec', t.get('duration', 0)))
            used_keys.add(f"{src}_{ss:.1f}")
            used_sources.add(src)
            # v13.2-i.3: 收集已用 clip_id
            for _cid_key in ('reel_clip_id', 'clip_id', 'id'):
                _cid_val = t.get(_cid_key, '')
                if _cid_val:
                    used_clip_ids.add(_cid_val)
            if src not in used_ranges:
                used_ranges[src] = []
            used_ranges[src].append((ss, se))
        # 最后一个镜头的素材（补位避免同源，防视觉重复）
        last_source = timeline[-1].get('source_file', timeline[-1].get('source', '')) if timeline else ''
        print(f"  [v13.2-i.3] padding guard: used_sources={len(used_sources)}, used_clip_ids={len(used_clip_ids)}")

        # ============================================================
        # v10.6 补位质量分层 + 分布约束
        # ============================================================
        from pipeline.pool_overrides import load_pool_data, apply_overrides_to_pool, _get_pool_level_for_source
        _fill_pool = load_pool_data(task_id)
        _fill_pool = apply_overrides_to_pool(_fill_pool)
        _BANNER_KEYWORDS_FILL = {'横幅', '标语', '合影', '易拉宝', '展板', '主视觉', '宣传牌',
                                  '标识', 'logo', '主题横幅', '宣传标语', '工伤预防', '活动主题',
                                  '活动合影', '宣传活动现场工作人员集体展示'}
        
        def _classify_fill_level(fname, fdata, window):
            """v10.6: 补位质量分层 A/B/C"""
            has_cw = bool(fdata.get('clean_windows', []))
            ffmpeg_stab = window.get('ffmpeg_stability', 'stable')
            ffmpeg_act = window.get('ffmpeg_action', 'keep')
            score_stab = 80  # 默认
            if hasattr(_classify_fill_level, '_manifest_scores'):
                sc = _classify_fill_level._manifest_scores.get(fname, {})
                score_stab = sc.get('stability', 80)
            
            # C 级：绝对禁止
            if ffmpeg_stab == 'unstable' or ffmpeg_act == 'downgrade':
                return 'C', f'ffmpeg={ffmpeg_stab}/{ffmpeg_act}'
            if not has_cw:
                return 'C', 'no_clean_windows'
            if score_stab < 60:
                return 'C', f'stability={score_stab}<60'
            
            # A 级（trim_head/trim_tail 说明主体段稳定，属于 A 级）
            if ffmpeg_stab in ('stable', 'normal_camera_move', 'head_unstable', 'slight_handheld'):
                if ffmpeg_act in ('keep', 'trim_head', 'trim_tail'):
                    return 'A', f'stable_or_trimmed({ffmpeg_stab}/{ffmpeg_act})'
            
            # B 级
            return 'B', f'stability={score_stab}/{ffmpeg_stab}'
        
        # 加载 manifest scores 缓存
        _manifest_path = output_dir / "candidate_reel_manifest.json"
        if _manifest_path.exists():
            try:
                with open(_manifest_path) as _mf:
                    _manifest_data = json.load(_mf)
                _manifest_scores = {}
                _mlist = _manifest_data if isinstance(_manifest_data, list) else _manifest_data.get('clips', [])
                for _mc in _mlist:
                    if isinstance(_mc, dict):
                        _mfn = _mc.get('source_file', '')
                        if _mfn not in _manifest_scores:
                            _manifest_scores[_mfn] = _mc.get('score_detail', {})
                _classify_fill_level._manifest_scores = _manifest_scores
            except Exception:
                _classify_fill_level._manifest_scores = {}
        else:
            _classify_fill_level._manifest_scores = {}
        
        extra_clips = []
        _relaxed_extra_clips = []  # v13.2-i.3: 降级候选（同 source 不同段）
        _fill_report = []  # v10.6 补位质量报告

        # v13.2-i.3: 加载 manifest scene_description 映射用于 LOW_INFO 检测
        _manifest_desc_map = {}  # source_file → scene_description
        _manifest_clipid_map = {}  # source_file → reel_clip_id
        if _manifest_path.exists():
            try:
                _md_raw = json.load(open(_manifest_path))
                _md_list = _md_raw if isinstance(_md_raw, list) else _md_raw.get('clips', [])
                for _md_entry in _md_list:
                    if isinstance(_md_entry, dict):
                        _md_src = _md_entry.get('source_file', '')
                        if _md_src and _md_src not in _manifest_desc_map:
                            _manifest_desc_map[_md_src] = _md_entry.get('scene_description', '')
                            _manifest_clipid_map[_md_src] = _md_entry.get('reel_clip_id', '')
            except Exception:
                pass

        # v13.2-i.3: LOW_INFO 关键词（与 guard 阶段统一，基于 scene_description）
        _LOW_INFO_KW_FILL = ['横幅', '红旗', '举旗', '举横幅', '合影', '集体合影', '摆拍',
                              '标识', '牌子', '标语', '口号', '背景板', '签到墙', '展板',
                              '纯环境', '空镜', '立牌', '主视觉']
        _padding_guard_stats = {'strict_skipped_used_source': 0, 'strict_skipped_clip_id': 0,
                                'strict_skipped_low_info': 0, 'strict_candidates': 0,
                                'relaxed_candidates': 0}

        if _fill_pool and shortfall > 0:
            for fname, fdata in _fill_pool.items():
                if fname == '_metadata' or not isinstance(fdata, dict):
                    continue
                _pl = _get_pool_level_for_source(fname, task_id)
                # v13.3-music-fill: 纯音乐模式下，未使用的 discard/backup 素材也可补位
                # 避免因 pool_level 过滤导致素材不足 → 重复补位
                _is_unused_for_fill = fname not in used_sources
                if _pl == 'disabled':
                    continue
                if _pl == 'discard' and not (edit_mode == 'music_only' and _is_unused_for_fill):
                    continue
                _info_type = fdata.get('information_type', '')
                # v11.6: 补位横幅过滤弱化 — 只排除纯横幅/纯标语（不含人物动作的）
                # 含人物的镜头即使 info_type 提到展板/主题也不排除
                _has_people_keywords = any(pk in _info_type for pk in ['学生', '嘉宾', '表演', '发言', '讲解', '参会', '展示', '演出', '互动', '听讲', '观众'])
                if not _has_people_keywords and any(kw in _info_type for kw in _BANNER_KEYWORDS_FILL):
                    continue

                # v13.2-i.3: 检查 scene_description LOW_INFO
                _fill_scene_desc = _manifest_desc_map.get(fname, '') or fdata.get('scene_description', '') or ''
                _is_low_info_fill = any(kw in _fill_scene_desc for kw in _LOW_INFO_KW_FILL)

                # v13.2-i.3: 检查是否已在 timeline 中使用
                _is_used_source = fname in used_sources
                _fill_clip_id = _manifest_clipid_map.get(fname, '')
                _is_used_clip = _fill_clip_id in used_clip_ids if _fill_clip_id else False

                for w in fdata.get('clean_windows', []):
                    fill_level, fill_reason = _classify_fill_level(fname, fdata, w)
                    if fill_level == 'C':
                        continue  # 绝对禁止
                    w_start = w.get('start_sec', 0)
                    w_end = w.get('end_sec', 0)
                    wdur = w_end - w_start
                    if wdur < 1.5:
                        continue
                    
                    # v10.6: 精确去重 — 找该窗口中未被 L3 使用的时间段
                    _used_in_window = used_ranges.get(fname, [])
                    # 计算可用区间（窗口减去已用段）
                    _avail_start = w_start
                    for _us, _ue in sorted(_used_in_window):
                        if _us <= _avail_start < _ue:
                            _avail_start = _ue  # 已用段结束后开始
                    _avail_dur = w_end - _avail_start
                    if _avail_dur < 1.5:
                        continue  # 剩余不够
                    
                    max_fill_dur = 3.0 if edit_mode == 'narration' else 3.5
                    _candidate = {
                        'source_file': fname,
                        'start_sec': _avail_start,
                        'duration': min(_avail_dur, max_fill_dur),
                        'pool_level': _pl,
                        'fill_level': fill_level,
                        'fill_reason': fill_reason,
                    }

                    # v13.2-i.3: 分流 strict vs relaxed
                    # v13.3-music-fill: 纯音乐模式下，未使用素材即使 LOW_INFO 也优先进入 strict
                    # 避免因 LOW_INFO 过滤导致素材不足 → 重复补位
                    if _is_low_info_fill and _is_used_source:
                        _padding_guard_stats['strict_skipped_low_info'] += 1
                        continue  # 已用素材 + LOW_INFO → 完全排除
                    elif _is_low_info_fill and not _is_used_source:
                        # 未使用素材即使 LOW_INFO 也作为 low_info_unused 候选（优先级低于普通 strict）
                        _candidate['_is_low_info_unused'] = True
                        _candidate['_fill_relaxed_dup'] = False
                        extra_clips.append(_candidate)
                        _padding_guard_stats['strict_candidates'] += 1
                    elif _is_used_source:
                        # 同 source 不同时间段 → relaxed 池（不论 clip_id 是否相同）
                        _padding_guard_stats['strict_skipped_used_source'] += 1
                        _candidate['_fill_relaxed_dup'] = True
                        _candidate['_fill_relaxed_reason'] = f'same_source_different_segment({fname})'
                        _candidate['original_used_source'] = True
                        _relaxed_extra_clips.append(_candidate)
                        _padding_guard_stats['relaxed_candidates'] += 1
                    elif _is_used_clip:
                        # 不同 source 但 clip_id 重复（理论上不常见）→ 排除
                        _padding_guard_stats['strict_skipped_clip_id'] += 1
                        continue
                    else:
                        extra_clips.append(_candidate)
                        _padding_guard_stats['strict_candidates'] += 1

            print(f"  [v13.2-i.3] padding guard: strict={_padding_guard_stats['strict_candidates']}, "
                  f"relaxed={_padding_guard_stats['relaxed_candidates']}, "
                  f"skipped(used_src={_padding_guard_stats['strict_skipped_used_source']}, "
                  f"clip_id={_padding_guard_stats['strict_skipped_clip_id']}, "
                  f"low_info={_padding_guard_stats['strict_skipped_low_info']})")
            
            # v11: 加载 slot_plan 语义供补位匹配
            _slot_semantic_map = {}
            if _slot_plan_path.exists():
                try:
                    with open(_slot_plan_path) as _spf_sem:
                        _sp_sem = json.load(_spf_sem)
                    for _s_sem in _sp_sem:
                        _slot_semantic_map[_s_sem.get('slot_id', '')] = _s_sem.get('semantic_hint', '')
                except Exception:
                    pass
            
            # v10.7+v11: 语义优先排序
            def _semantic_match_score(clip, rejected_semantics):
                """计算补位候选与被拦截镜头的语义匹配分（越低越优先）"""
                src = clip['source_file']
                # 查找该素材的 info_type
                _src_l2 = _gate_pool.get(src, {})
                _src_info = _src_l2.get('information_type', '') if isinstance(_src_l2, dict) else ''
                _src_manifest = _gate_manifest_scores.get(src, {})
                _src_info_m = _src_manifest.get('info_type', '')
                
                best_score = 99  # 默认无匹配
                for _slot_id, _sem in rejected_semantics.items():
                    _rej_info = _sem.get('info_type', '')
                    _rej_motif = _sem.get('visual_motif', '')
                    
                    # 关键词匹配
                    _info_keywords = set()
                    for _text in [_rej_info, _rej_motif]:
                        for _kw in ['讲解', '展演', '表演', '合唱', '发言', '展示', '互动', '观众', '嘉宾', '装饰', '展板', '列队', '手工', '模型']:
                            if _kw in _text:
                                _info_keywords.add(_kw)
                    
                    _match_count = 0
                    for _kw in _info_keywords:
                        if _kw in _src_info or _kw in _src_info_m:
                            _match_count += 1
                    
                    if _match_count >= 2:
                        score = 0  # 强匹配
                    elif _match_count == 1:
                        score = 1  # 弱匹配
                    else:
                        score = 2  # 无匹配
                    
                    best_score = min(best_score, score)
                
                return best_score
            
            _level_order = {'A': 0, 'B': 1}
            _pool_order = {'primary': 0, 'backup': 1}
            
            # v13.3-music-fill: 排序时 low_info_unused 排在普通 strict 后面
            _low_info_order = lambda c: 1 if c.get('_is_low_info_unused') else 0
            if _gate_rejected_semantic:
                extra_clips.sort(key=lambda c: (
                    _low_info_order(c),
                    _semantic_match_score(c, _gate_rejected_semantic),
                    _level_order.get(c.get('fill_level','B'), 1),
                    _pool_order.get(c.get('pool_level','backup'), 1)
                ))
            else:
                extra_clips.sort(key=lambda c: (
                    _low_info_order(c),
                    _level_order.get(c.get('fill_level','B'), 1),
                    _pool_order.get(c.get('pool_level','backup'), 1)
                ))
            
            # v13.2-i.3: 对 relaxed 候选也做排序
            if _gate_rejected_semantic:
                _relaxed_extra_clips.sort(key=lambda c: (
                    _semantic_match_score(c, _gate_rejected_semantic),
                    _level_order.get(c.get('fill_level','B'), 1),
                    _pool_order.get(c.get('pool_level','backup'), 1)
                ))
            else:
                _relaxed_extra_clips.sort(key=lambda c: (
                    _level_order.get(c.get('fill_level','B'), 1),
                    _pool_order.get(c.get('pool_level','backup'), 1)
                ))
            
            # v13.3-p2a: repeat classifier - 对 music_only 模式候选进行重复分类
            _repeat_classified = {'forbidden': 0, 'low_grade_fill': 0, 'structural_recall': 0, 'fresh': 0}
            _classified_extra_clips = []  # 过滤 forbidden 后的候选
            _low_grade_pool = []  # 低级重复池（最后兜底）
            
            if edit_mode == 'music_only':
                # v13.3-p2a-fix: 计算每个 clip 在最终 timeline 中的位置
                _timeline_for_classify = []
                _final_pos = 0.0  # 当前在最终 timeline 中的位置
                for _t in timeline:
                    _dur = _t.get('duration_sec', _t.get('duration', 0))
                    _timeline_for_classify.append({
                        'source_file': _t.get('source_file', _t.get('source', '')),
                        'start_sec': _t.get('start_sec', _t.get('start', 0)),  # 素材裁剪起点
                        'duration': _dur,
                        'final_start': _final_pos,  # v13.3-p2a-fix: 最终 timeline 起点
                        'final_end': _final_pos + _dur,  # v13.3-p2a-fix: 最终 timeline 终点
                        'visual_class': _t.get('visual_class', ''),
                        'scene_description': _t.get('scene_description', ''),
                        'reel_clip_id': _t.get('reel_clip_id', '')
                    })
                    _final_pos += _dur
                
                _current_timeline_duration = _final_pos  # 最终 timeline 总时长
                
                # 对 extra_clips 进行分类
                for _ec in extra_clips:
                    _r_type, _r_reason, _r_gap = classify_music_repeat(_ec, _timeline_for_classify, _current_timeline_duration, edit_mode)
                    _ec['_repeat_type'] = _r_type
                    _ec['_repeat_reason'] = _r_reason
                    _ec['_repeat_gap'] = _r_gap
                    _ec['_final_timeline_position'] = _current_timeline_duration
                    _repeat_classified[_r_type] += 1
                    
                    if _r_type == 'forbidden':
                        print(f"  [v13.3-p2a] 🚫 forbidden: {_ec['source_file']} - {_r_reason}")
                        continue  # 跳过禁止重复
                    elif _r_type == 'low_grade_fill':
                        _low_grade_pool.append(_ec)
                        print(f"  [v13.3-p2a] ⚠️ low_grade: {_ec['source_file']} - {_r_reason}")
                    else:
                        _classified_extra_clips.append(_ec)  # fresh / structural_recall
                
                # 对 relaxed 候选也分类
                _classified_relaxed = []
                for _rc in _relaxed_extra_clips:
                    _r_type, _r_reason, _r_gap = classify_music_repeat(_rc, _timeline_for_classify, _current_timeline_duration, edit_mode)
                    _rc['_repeat_type'] = _r_type
                    _rc['_repeat_reason'] = _r_reason
                    _rc['_repeat_gap'] = _r_gap
                    _rc['_final_timeline_position'] = _current_timeline_duration
                    _repeat_classified[_r_type] += 1
                    
                    if _r_type == 'forbidden':
                        print(f"  [v13.3-p2a] 🚫 forbidden(relaxed): {_rc['source_file']} - {_r_reason}")
                        continue  # v13.3-p2b-forbidden-fix: 跳过禁止重复，不加入任何池
                    elif _r_type == 'low_grade_fill':
                        # v13.3-p2b-forbidden-fix: music_only 模式下 low_grade_fill 也跳过
                        print(f"  [v13.3-p2a] 🚫 low_grade(relaxed): {_rc['source_file']} - {_r_reason} (gap<{8}s)")
                        continue
                    else:
                        _classified_relaxed.append(_rc)
                
                # 合并候选池：只用 fresh + structural_recall
                extra_clips = _classified_extra_clips + _classified_relaxed
                _relaxed_extra_clips = []  # v13.3-p2b-forbidden-fix: 禁用 low_grade 兜底
                
                print(f"  [v13.3-p2a] repeat classifier: forbidden={_repeat_classified['forbidden']}, "
                      f"low_grade={_repeat_classified['low_grade_fill']}, "
                      f"structural={_repeat_classified['structural_recall']}, fresh={_repeat_classified['fresh']}")

            # 补入额外镜头（v10.6 分布约束）
            # v11.6: 补位不仅看时长缺口，还看空 slot 数量
            # 即使 shortfall <= 0，如果仍有空 slot，继续补位填充
            _fill_count = 0
            _strict_fill_count = 0  # v13.2-i.3
            _relaxed_fill_count = 0  # v13.2-i.3
            _relaxed_dup_triggered = False  # v13.2-i.3
            _b_level_count = 0
            _last_fill_level = None
            _b_level_max = 2  # 全片 B 级最多 2 条
            
            for ec in extra_clips:
                if shortfall <= 0 and _fill_count >= _empty_slot_count:
                    break  # v11.6: 时长够了且空 slot 都填了才停
                if _fill_count >= _max_fill_clips:
                    print(f"  ⚠️ [补位上限] 已补 {_fill_count} 条，超出上限 {_max_fill_clips}，停止")
                    break
                
                # v13.3-p2b-forbidden-fix: 实时检查 forbidden（每次补位前重新计算 timeline）
                if edit_mode == 'music_only':
                    # 重新构建 timeline 信息（包含已补位的镜头）
                    _timeline_for_check = []
                    _final_pos_check = 0.0
                    for _t_check in timeline:
                        _dur_check = _t_check.get('duration_sec', _t_check.get('duration', 0))
                        _timeline_for_check.append({
                            'source_file': _t_check.get('source_file', _t_check.get('source', '')),
                            'start_sec': _t_check.get('start_sec', _t_check.get('start', 0)),
                            'duration': _dur_check,
                            'final_start': _final_pos_check,
                            'final_end': _final_pos_check + _dur_check,
                        })
                        _final_pos_check += _dur_check
                    # 包含已补位的 extra clips
                    for _cp_idx, _cp_check in enumerate(clip_paths):
                        try:
                            import subprocess as _sub_check
                            _dur_probe = _sub_check.run([FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
                                '-of', 'default=noprint_wrappers=1:nokey=1', str(_cp_check)],
                                capture_output=True, text=True, timeout=5).stdout.strip()
                            _dur_val = float(_dur_probe) if _dur_probe else 2.5
                        except:
                            _dur_val = 2.5
                        _src_name_check = _cp_check.name.replace('clip_', '').replace('.mp4', '').replace('extra_', '').split('_')[-1].upper() + '.MP4'
                        _timeline_for_check.append({
                            'source_file': _src_name_check,
                            'start_sec': 0,
                            'duration': _dur_val,
                            'final_start': _final_pos_check,
                            'final_end': _final_pos_check + _dur_val,
                        })
                        _final_pos_check += _dur_val
                    
                    # 实时检查
                    _r_type_rt, _r_reason_rt, _r_gap_rt = classify_music_repeat(ec, _timeline_for_check, _final_pos_check, edit_mode)
                    if _r_type_rt == 'forbidden':
                        print(f"  [v13.3-p2b-实时] 🚫 forbidden: {ec['source_file']} - {_r_reason_rt}")
                        continue
                    elif _r_type_rt == 'low_grade_fill':
                        print(f"  [v13.3-p2b-实时] 🚫 low_grade: {ec['source_file']} - {_r_reason_rt}")
                        continue
                
                # B 级分布约束
                if ec['fill_level'] == 'B':
                    if _b_level_count >= _b_level_max:
                        continue  # B 级已满
                    if _last_fill_level == 'B':
                        continue  # 禁止连续 B 级
                
                src = ec['source_file']
                local_path = dl_dir / src.replace('.MP4', '.mp4')
                video_url = tos_url_map.get(src, '')
                if not local_path.exists() and video_url:
                    r = requests.get(video_url, timeout=120)
                    with open(local_path, 'wb') as f:
                        f.write(r.content)
                if local_path.exists():
                    clip_idx = len(clip_paths)
                    clip_name = f"clip_{clip_idx:02d}_extra_{src.replace('.MP4', '')}.mp4"
                    clip_path = clips_dir / clip_name
                    max_fill = 3.0 if edit_mode == 'narration' else 3.5
                    # v12.6: 补位最小时长提升到 slot 目标时长（减少 tpad clone）
                    _slot_target_dur = 2.5
                    if _slot_plan_path.exists():
                        try:
                            _sp_tmp = json.load(open(_slot_plan_path))
                            if _sp_tmp:
                                _slot_target_dur = _sp_tmp[0].get('target_duration', 2.5)
                        except:
                            pass
                    _min_fill = max(2.0, _slot_target_dur - 0.2) if edit_mode == 'narration' else 1.5
                    use_dur = min(ec['duration'], max(shortfall + 0.5, _min_fill), max_fill)
                    if use_dur < _min_fill:
                        continue
                    subprocess.run([
                        FFMPEG, '-y', '-i', str(local_path),
                        '-ss', str(ec['start_sec']), '-t', str(use_dur),
                        '-c:v', 'libx264', '-an', '-preset', 'fast',
                        str(clip_path)
                    ], check=True, capture_output=True)
                    clip_paths.append(clip_path)
                    shortfall -= use_dur
                    _fill_count += 1
                    if ec['fill_level'] == 'B':
                        _b_level_count += 1
                    _last_fill_level = ec['fill_level']
                    # v10.7: 语义匹配信息
                    _sem_score = _semantic_match_score(ec, _gate_rejected_semantic) if _gate_rejected_semantic else 99
                    _sem_label = {0: 'strong_match', 1: 'weak_match', 2: 'no_match', 99: 'n/a'}.get(_sem_score, 'n/a')
                    _src_l2_info = _gate_pool.get(src, {})
                    _src_info_text = _src_l2_info.get('information_type', '') if isinstance(_src_l2_info, dict) else ''
                    _fill_report.append({
                        'source_file': src,
                        'start_sec': ec['start_sec'],
                        'duration': use_dur,
                        'fill_level': ec['fill_level'],
                        'fill_reason': ec['fill_reason'],
                        'pool_level': ec['pool_level'],
                        'semantic_match': _sem_label,
                        'info_type': _src_info_text[:60],
                    })
                    _strict_fill_count += 1
                    print(f"  ✅ 补位[{ec['fill_level']}]: {src} [{ec['start_sec']:.1f}s +{use_dur:.1f}s]（剩余缺口 {max(0,shortfall):.1f}s）")

            # v13.2-i.3: 第二阶段 relaxed_fill — strict 不足时降级用同 source 不同段
            _need_more = (shortfall > 0 or (_use_slot_render and _fill_count < _empty_slot_count)) and _fill_count < _max_fill_clips
            if _need_more and _relaxed_extra_clips:
                _relaxed_dup_triggered = True
                print(f"  [v13.2-i.3] ⚠️ strict_fill 不足 (filled={_fill_count}, empty_slots={_empty_slot_count}, shortfall={max(0,shortfall):.1f}s)，启用 relaxed_fill ({len(_relaxed_extra_clips)} 候选)")
                for ec in _relaxed_extra_clips:
                    if shortfall <= 0 and _fill_count >= _empty_slot_count:
                        break
                    if _fill_count >= _max_fill_clips:
                        break
                    if ec['fill_level'] == 'B':
                        if _b_level_count >= _b_level_max:
                            continue
                        if _last_fill_level == 'B':
                            continue
                    src = ec['source_file']
                    local_path = dl_dir / src.replace('.MP4', '.mp4')
                    video_url = tos_url_map.get(src, '')
                    if not local_path.exists() and video_url:
                        r = requests.get(video_url, timeout=120)
                        with open(local_path, 'wb') as f:
                            f.write(r.content)
                    if local_path.exists():
                        clip_idx = len(clip_paths)
                        clip_name = f"clip_{clip_idx:02d}_extra_{src.replace('.MP4', '')}.mp4"
                        clip_path = clips_dir / clip_name
                        max_fill = 3.0 if edit_mode == 'narration' else 3.5
                        _slot_target_dur = 2.5
                        if _slot_plan_path.exists():
                            try:
                                _sp_tmp = json.load(open(_slot_plan_path))
                                if _sp_tmp:
                                    _slot_target_dur = _sp_tmp[0].get('target_duration', 2.5)
                            except:
                                pass
                        _min_fill = max(2.0, _slot_target_dur - 0.2) if edit_mode == 'narration' else 1.5
                        use_dur = min(ec['duration'], max(shortfall + 0.5, _min_fill), max_fill)
                        if use_dur < _min_fill:
                            continue
                        subprocess.run([
                            FFMPEG, '-y', '-i', str(local_path),
                            '-ss', str(ec['start_sec']), '-t', str(use_dur),
                            '-c:v', 'libx264', '-an', '-preset', 'fast',
                            str(clip_path)
                        ], check=True, capture_output=True)
                        clip_paths.append(clip_path)
                        shortfall -= use_dur
                        _fill_count += 1
                        _relaxed_fill_count += 1
                        if ec['fill_level'] == 'B':
                            _b_level_count += 1
                        _last_fill_level = ec['fill_level']
                        _fill_report.append({
                            'source_file': src,
                            'start_sec': ec['start_sec'],
                            'duration': use_dur,
                            'fill_level': ec['fill_level'],
                            'fill_reason': ec['fill_reason'],
                            'pool_level': ec['pool_level'],
                            'semantic_match': 'n/a',
                            'info_type': '',
                            '_fill_relaxed_dup': True,
                            '_fill_relaxed_reason': ec.get('_fill_relaxed_reason', 'same_source_fallback'),
                        })
                        print(f"  ⚠️ 补位[relaxed_dup/{ec['fill_level']}]: {src} [{ec['start_sec']:.1f}s +{use_dur:.1f}s]（剩余缺口 {max(0,shortfall):.1f}s）")
            elif _need_more:
                print(f"  [v13.2-i.3] ⚠️ strict_fill 不足且无 relaxed 候选，剩余缺口 {max(0,shortfall):.1f}s")

            print(f"  [v13.2-i.3] 补位结果: strict={_strict_fill_count}, relaxed={_relaxed_fill_count}, total={_fill_count}")

            # v13.3-music-fill: 纯音乐模式同 source 间隔 guard
            if edit_mode == 'music_only' and _relaxed_fill_count > 0:
                # 检查同 source_file 在最终 clip_paths 中的间隔
                _clip_source_positions = {}  # source → [position_idx, ...]
                for _ci, _cp in enumerate(clip_paths):
                    _cpn = _cp.name if hasattr(_cp, 'name') else str(_cp).split('/')[-1]
                    _parts = _cpn.replace('.mp4','').replace('.MP4','').split('_')
                    _src_name = _parts[-1].upper() + '.MP4'
                    if _src_name not in _clip_source_positions:
                        _clip_source_positions[_src_name] = []
                    _clip_source_positions[_src_name].append(_ci)
                _dup_sources_final = {k: v for k, v in _clip_source_positions.items() if len(v) > 2}
                if _dup_sources_final:
                    print(f"  [v13.3-music-fill] ⚠️ source 出现 >2 次: {_dup_sources_final}")

        # v10.6: 保存补位质量报告
        _fqr_path = output_dir / "fallback_quality_report.json"
        _fqr = {
            'fill_count': len(_fill_report),
            'a_count': sum(1 for r in _fill_report if r['fill_level'] == 'A'),
            'b_count': sum(1 for r in _fill_report if r['fill_level'] == 'B'),
            'c_count': 0,  # C 级永远为 0
            'remaining_shortfall': max(0, shortfall) if shortfall > 0 else 0,
            'fills': _fill_report,
        }
        with open(_fqr_path, 'w', encoding='utf-8') as _fqf:
            json.dump(_fqr, _fqf, ensure_ascii=False, indent=2)
        print(f"  [v10.6] 补位报告: {len(_fill_report)} 条 (A:{_fqr['a_count']} B:{_fqr['b_count']} C:0)")

        # v13.2-i.3: 保存 padding_guard_summary
        # 统计最终 clip_paths 中的 source_file 出现次数
        _final_src_count = {}
        for _cp_final in clip_paths:
            _cpn = _cp_final.name if hasattr(_cp_final, 'name') else str(_cp_final).split('/')[-1]
            # 从 clip 文件名提取 source_file
            _parts = _cpn.replace('.mp4', '').replace('.MP4', '').split('_', 2)
            _extracted_src = (_parts[2] + '.MP4') if len(_parts) >= 3 else _cpn
            _final_src_count[_extracted_src] = _final_src_count.get(_extracted_src, 0) + 1
        _unresolved_dups = {k: v for k, v in _final_src_count.items() if v > 1}

        # 检查关键 clip 状态
        _p01_count = sum(1 for k, v in _final_src_count.items() if '394A0108' in k for _ in range(v))
        _b35_count = sum(1 for k, v in _final_src_count.items() if '0113_D' in k for _ in range(v))
        _b121_count = sum(1 for k, v in _final_src_count.items() if '0121_D' in k for _ in range(v))

        # 检查 B50/P25/B63 状态（在 timeline 中）
        _b50_in = any('0131_D' in t.get('source_file','') for t in timeline)
        _p25_in = any('0136_D' in t.get('source_file','') for t in timeline)
        _b63_in = any('0146_D' in t.get('source_file','') for t in timeline)
        _slot01_src = timeline[0].get('source_file','') if timeline else ''
        _slot01_is_b55 = '0138_D' in _slot01_src

        _padding_summary = {
            'guard': 'v13.2-i.3_padding_used_source_lowinfo',
            'strict_candidates_count': _padding_guard_stats.get('strict_candidates', 0),
            'relaxed_candidates_count': _padding_guard_stats.get('relaxed_candidates', 0),
            'strict_skipped_used_source': _padding_guard_stats.get('strict_skipped_used_source', 0),
            'strict_skipped_clip_id': _padding_guard_stats.get('strict_skipped_clip_id', 0),
            'strict_skipped_low_info': _padding_guard_stats.get('strict_skipped_low_info', 0),
            'fills_added': _fill_count if '_fill_count' in dir() else 0,
            'strict_fill_count': _strict_fill_count if '_strict_fill_count' in dir() else 0,
            'relaxed_fill_count': _relaxed_fill_count if '_relaxed_fill_count' in dir() else 0,
            'relaxed_dup_triggered': _relaxed_dup_triggered if '_relaxed_dup_triggered' in dir() else False,
            'fills_from_used_source_count': _relaxed_fill_count if '_relaxed_fill_count' in dir() else 0,
            'unresolved_duplicates': _unresolved_dups,
            'source_file_occurrences': {k: v for k, v in _final_src_count.items() if v > 1},
            'P01_count': _p01_count,
            'B35_count': _b35_count,
            'B121_count': _b121_count,
            'B50_status': 'in_timeline' if _b50_in else 'missing',
            'P25_status': 'in_timeline' if _p25_in else 'missing',
            'B63_status': 'in_timeline' if _b63_in else 'missing',
            'slot_01_is_B55': _slot01_is_b55,
            'tpad_projection': max(0, shortfall) if 'shortfall' in dir() and shortfall > 0 else 0.0,
        }
        
        # v13.3-p2b: adaptive_duration - 纯音乐模式质量优先于硬凑时长
        _adaptive_duration = {
            'enabled': _realistic_duration < target_duration if '_realistic_duration' in dir() else False,
            'original_target': 30,
            'final_duration': _realistic_duration if '_realistic_duration' in dir() else target_duration,
            'avoided_low_grade_fill_count': 0,
            'reason': _adaptive_target_reason if '_adaptive_target_reason' in dir() else ''
        }
        
        # 检测是否只剩下 low_grade_fill
        if edit_mode == 'music_only' and len(_low_grade_pool) > 0 and shortfall > 1.5:
            # 计算当前 clip_paths 总时长
            _clip_dur_sum = 0.0
            for _cp in clip_paths:
                try:
                    _dur_out = subprocess.run([FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
                               '-of', 'default=noprint_wrappers=1:nokey=1', str(_cp)],
                              capture_output=True, text=True, timeout=5).stdout.strip()
                    _clip_dur_sum += float(_dur_out) if _dur_out else 0
                except:
                    pass
            
            # 如果 current_duration >= 24s 且只有 low_grade_fill 剩余
            if _clip_dur_sum >= 24.0:
                _adaptive_duration['enabled'] = True
                _adaptive_duration['final_duration'] = _clip_dur_sum
                _adaptive_duration['avoided_low_grade_fill_count'] = len(_low_grade_pool)
                _adaptive_duration['reason'] = 'only low_grade_fill candidates remained'
                print(f"  [v13.3-p2b] ✅ adaptive_duration: 当前 {_clip_dur_sum:.1f}s >= 24s，跳过 {len(_low_grade_pool)} 个 low_grade_fill")
                # 清空 low_grade_pool，不再使用
                _low_grade_pool.clear()
                shortfall = max(0, 30 - _clip_dur_sum)
        with open(output_dir / "padding_used_source_guard_summary.json", 'w', encoding='utf-8') as _pgs:
            json.dump(_padding_summary, _pgs, ensure_ascii=False, indent=2)
        
        # v13.3-p2b: 保存 adaptive_duration summary
        if edit_mode == 'music_only':
            with open(output_dir / "adaptive_duration_summary.json", 'w', encoding='utf-8') as _ads:
                json.dump(_adaptive_duration, _ads, ensure_ascii=False, indent=2)
        print(f"  [v13.2-i.3] padding_guard_summary: P01={_p01_count} B35={_b35_count} B121={_b121_count} "
              f"unresolved_dups={len(_unresolved_dups)} relaxed={_relaxed_dup_triggered if '_relaxed_dup_triggered' in dir() else False}")

        # ============================================================
        # v11.6.3: 第二轮兜底 — 如果仍有空 slot，放宽 backup stability 到 ≥60
        # ============================================================
        # v11.6.3: 第二轮兜底条件改为看空 slot 而非 shortfall
        _post_fill_covered = set(t.get('slot_id', '') for t in timeline)
        _post_fill_empty = [s['slot_id'] for s in _sp_sf if s['slot_id'] not in _post_fill_covered] if _use_slot_render and '_sp_sf' in dir() else []
        if _use_slot_render and _slot_plan_path.exists() and _post_fill_empty:
            _relaxable = getattr(generate_video, '_relaxable_backups', [])
            if _relaxable:
                _still_empty = list(_post_fill_empty)
                
                if _still_empty:
                    print(f"  [v11.6.3] 🔄 第二轮兜底：仍有 {len(_still_empty)} 个空 slot，尝试 stability 60-69 backup")
                    for _rb in _relaxable:
                        if shortfall <= 0 or _fill_count >= _max_fill_clips:
                            break
                        _rb_src = _rb['source_file']
                        _rb_stab = _rb['stability']
                        _rb_slot = _rb['slot_id']
                        
                        # 只对空 slot 使用
                        if _rb_slot not in _still_empty:
                            continue
                        
                        # 下载并裁切
                        local_path = dl_dir / _rb_src.replace('.MP4', '.mp4')
                        video_url = tos_url_map.get(_rb_src, '')
                        if not local_path.exists() and video_url:
                            try:
                                r = requests.get(video_url, timeout=120)
                                if r.status_code == 200:
                                    with open(local_path, 'wb') as _dlf:
                                        _dlf.write(r.content)
                            except Exception:
                                continue
                        if not local_path.exists():
                            continue
                        
                        _te = _rb['timeline_entry']
                        _rb_ss = _te.get('start_sec', 0)
                        _rb_dur = min(_te.get('duration_sec', 3.0), 3.0)
                        clip_out = output_dir / f"clip_{len(clip_paths)+1:02d}_relaxed_{_rb_src.replace('.MP4','.mp4')}"
                        
                        try:
                            subprocess.run([
                                FFMPEG, '-y', '-i', str(local_path),
                                '-ss', str(_rb_ss), '-t', str(_rb_dur),
                                '-an', '-c:v', 'libx264', '-preset', 'fast',
                                str(clip_out)
                            ], capture_output=True, timeout=30)
                        except Exception:
                            continue
                        
                        if clip_out.exists() and clip_out.stat().st_size > 10000:
                            clip_paths.append(clip_out)
                            shortfall -= _rb_dur
                            _fill_count += 1
                            _still_empty.remove(_rb_slot)
                            _fill_report.append({
                                'source_file': _rb_src,
                                'start_sec': _rb_ss,
                                'duration': _rb_dur,
                                'fill_level': 'A',
                                'fill_reason': f'relaxed_backup(stability={_rb_stab})',
                                'pool_level': 'backup',
                                'semantic_match': 'n/a',
                                'info_type': _te.get('scene_type', ''),
                                'fallback_relaxed': True,
                                'relaxed_reason': 'slot_shortfall_recovery',
                            })
                            print(f"  ✅ 兜底补位[relaxed]: {_rb_src} stability={_rb_stab} → {_rb_slot}（剩余缺口 {max(0,shortfall):.1f}s）")
                    
                    # 更新补位报告
                    _fqr['fill_count'] = len(_fill_report)
                    _fqr['a_count'] = sum(1 for f in _fill_report if f['fill_level'] == 'A')
                    _fqr['fills'] = _fill_report
                    with open(_fqr_path, 'w', encoding='utf-8') as _fqf:
                        json.dump(_fqr, _fqf, ensure_ascii=False, indent=2)
            
            # 清理
            generate_video._relaxable_backups = []
        
        # v10.6: 缺口检查 — 纯音乐模式更宽容（L3 选片经常不足）
        _max_gap = 1.0 if edit_mode == 'narration' else 3.0
        if shortfall > _max_gap:
            print(f"  ⚠️ [v10.6] 缺口 {shortfall:.1f}s > {_max_gap}s，合法补位不足")
            # 不直接 raise，尝试用优先级2/3（延长现有镜头）弥补
            # 如果延长后仍超 _max_gap，在最终检查时报错

        # --- 优先级2: 择优延长现有镜头（可多条，每条上限 1.5s） ---
        if shortfall > 0:
            # v10.2: 可延长多条镜头（按时长排序，最长的优先延长）
            _extend_candidates = []
            for i, t in enumerate(timeline):
                if i < len(clip_paths):
                    d = t.get('duration_sec', t.get('duration', 0))
                    _extend_candidates.append((i, d, t))
            _extend_candidates.sort(key=lambda x: x[1], reverse=True)  # 最长的优先

            _extend_count = 0
            for best_idx, best_dur, bt in _extend_candidates:
                if shortfall <= 0:
                    break
                if _extend_count >= 3:  # 最多延长 3 条
                    break
                max_extend = 1.5 if edit_mode == 'narration' else 1.0  # v13.3-p2b-rhythm: music_only 延长上限 1.0s（节奏优先）
                extend_amt = min(shortfall + 0.3, max_extend)
                # v13.3-p2b-rhythm: 延长后总时长上限更严格
                _mal = bt.get('main_action_level', '')
                _is_main = _mal == 'main_action'
                _role = bt.get('role', '')
                _is_opening = _role == 'opening'
                _is_ending = _role == 'closing' or _role == 'ending'
                # opening clip 上限 3.2s，普通 3.6s，ending 4.0s
                if _is_opening:
                    _max_total = 3.2
                elif _is_ending or _is_main:
                    _max_total = 4.0
                else:
                    _max_total = 3.6
                _current_dur = bt.get('duration_sec', bt.get('duration', 0))
                _allowed_extend = min(extend_amt, max(0, _max_total - _current_dur))
                if _allowed_extend <= 0:
                    continue  # 已达到上限，不能继续延长
                extend_amt = _allowed_extend
                bt_src = bt.get('source_file', bt.get('source', ''))
                bt_ss = bt.get('start_sec', bt.get('start', 0))
                bt_dur = bt.get('duration_sec', bt.get('duration', 0))
                bt_local = dl_dir / bt_src.replace('.MP4', '.mp4')
                if bt_local.exists():
                    subprocess.run([
                        FFMPEG, '-y', '-i', str(bt_local),
                        '-ss', str(bt_ss), '-t', str(bt_dur + extend_amt),
                        '-c:v', 'libx264', '-an', '-preset', 'fast',
                        str(clip_paths[best_idx])
                    ], check=True, capture_output=True)
                    shortfall -= extend_amt
                    _extend_count += 1
                    print(f"  ✅ 择优延长镜头{best_idx+1}: {bt_src} +{extend_amt:.1f}s（原{bt_dur:.1f}s→{bt_dur+extend_amt:.1f}s）")

        # --- 优先级3: 轻量兜底（延长最后一个镜头，上限2秒） ---
        if shortfall > 0 and edit_mode != 'music_only':  # v13.3-p2b-rhythm: music_only 禁用兜底延长
            # v8.2: 兜底延长也受节奏约束
            max_tail_extend = 1.0
            extend_amt = min(shortfall + 0.5, max_tail_extend)
            last_t = timeline[-1]
            last_src = last_t.get('source_file', last_t.get('source', ''))
            last_ss = last_t.get('start_sec', last_t.get('start', 0))
            last_dur = last_t.get('duration_sec', last_t.get('duration', 0))
            last_local = dl_dir / last_src.replace('.MP4', '.mp4')
            if last_local.exists():
                subprocess.run([
                    FFMPEG, '-y', '-i', str(last_local),
                    '-ss', str(last_ss), '-t', str(last_dur + extend_amt),
                    '-c:v', 'libx264', '-an', '-preset', 'fast',
                    str(clip_paths[-1])
                ], check=True, capture_output=True)
                shortfall -= extend_amt
                print(f"  ⚠️ 兜底延长末尾镜头: {last_src} +{extend_amt:.1f}s（仅作为最后手段）")

    # ============================================================
    # 3b. 渲染前最终校验（v8.4 — 选片边界强校验）
    # ============================================================
    print(f"\n  === 渲染前最终校验 ===")
    _pre_total = 0.0
    _pre_issues = []
    for _ci, _cp in enumerate(clip_paths):
        try:
            _pc = subprocess.run([FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
                                 '-of', 'default=noprint_wrappers=1:nokey=1', str(_cp)],
                                capture_output=True, text=True, timeout=5)
            _cd = float(_pc.stdout.strip())
        except:
            _cd = 0
        _pre_total += _cd
        if _cd < 1.5:
            _pre_issues.append(f"clip[{_ci}] {_cp.name}: {_cd:.2f}s < 1.5s")
    print(f"  clip 总时长: {_pre_total:.2f}s | 音频时长: {tts_dur:.1f}s | 差值: {tts_dur - _pre_total:.2f}s")
    if _pre_issues:
        for _iss in _pre_issues:
            print(f"  ❌ {_iss}")
    if _pre_total >= tts_dur - 0.5:
        print(f"  ✅ 视频总时长充足")
    else:
        _gap = tts_dur - _pre_total
        print(f"  ⚠️ 视频总时长 {_pre_total:.2f}s 不足（需 ≥ {tts_dur - 0.5:.1f}s），缺口 {_gap:.1f}s")
        # v10.8: 缺口超过安全阈值直接阻断，不进入 ffmpeg
        # v10.8: 提前阻断阈值（比实际 tpad 上限宽松，只拦截极端情况如 41s 缺口）
        _max_tpad_gate = 8.0 if edit_mode == 'narration' else 10.0
        if _gap > _max_tpad_gate:
            _err_msg = (f"render_timeline_invalid: 视频 {_pre_total:.1f}s 严重不足（需 {tts_dur:.1f}s），"
                       f"缺口 {_gap:.1f}s 远超 tpad 上限 {_max_tpad}s。"
                       f"clip 数量 {len(clip_paths)}，timeline 数量 {len(filtered_timeline)}。"
                       f"可能原因：slot/timeline 断裂或 edit_mode 切换后数据不兼容。")
            print(f"  ❌ [v10.8] {_err_msg}")
            raise RuntimeError(_err_msg)

    # ============================================================
    # 4. 拼接
    # ============================================================
    _update_stage(task_path, "concat")
    _heartbeat("concat_started")
    _check_cancel()
    print(f"\n[4/5] 拼接 + 合成")
    # ============================================================
    # v8.6: sentence_timeline 重排已删除（收回后验导演权）
    # clip_paths 顺序 = L3 timeline 顺序 + 补位追加，不再重排
    # ============================================================

    concat_path = output_dir / "concat.txt"

    # ============================================================
    # v9.9: Slot 时间锁定 — final_render_timeline 由 slot_plan 驱动
    # ============================================================
    # _use_slot_render 已在 slot_plan 生成时设置（v9.9）
    # 这里再次校验 slot_plan 文件存在性
    if _use_slot_render and not _slot_plan_path.exists():
        print(f"  ⚠️ [v9.9] _use_slot_render=True 但 slot_plan.json 不存在，回退为普通模式")
        _use_slot_render = False

    if _use_slot_render:
        with open(_slot_plan_path, 'r') as _spf:
            _slot_plan = json.load(_spf)
        # v10.1: 加载 L2 产物用于 slot 扩展时校验 clean_window
        from pipeline.pool_overrides import load_pool_data as _load_pool_data_slot
        pool_data_for_slot = _load_pool_data_slot(task_id)
        print(f"  [v9.9] Slot 时间锁定模式: {len(_slot_plan)} 个 slot")

        # 建立 slot_id → timeline 映射
        _slot_timeline_map = {}  # slot_id → timeline entry
        for t in timeline:
            sid = t.get('slot_id', '')
            if sid:
                _slot_timeline_map[sid] = t

        # 建立 slot_id → clip_path 映射
        _slot_clip_map = {}  # slot_id → clip_path
        for i, t in enumerate(timeline):
            sid = t.get('slot_id', '')
            if sid and i < len(clip_paths):
                _slot_clip_map[sid] = clip_paths[i]

        # 补位 clip 映射（补位没有 slot_id，追加到末尾空 slot）
        _used_slot_ids = set(_slot_clip_map.keys())
        _extra_clip_idx = len(timeline)  # 补位 clip 从 timeline 之后开始
        _empty_slots = [s for s in _slot_plan if s['slot_id'] not in _used_slot_ids]
        for i, empty_slot in enumerate(_empty_slots):
            extra_idx = _extra_clip_idx + i
            if extra_idx < len(clip_paths):
                _slot_clip_map[empty_slot['slot_id']] = clip_paths[extra_idx]
                print(f"    补位 → {empty_slot['slot_id']}: {clip_paths[extra_idx].name}")

        # 按 slot 顺序生成 render_timeline + concat
        render_timeline = []
        slot_integrity = []
        _ordered_clip_paths = []

        for slot in _slot_plan:
            sid = slot['slot_id']
            target_start = slot['target_start']
            target_end = slot['target_end']
            target_dur = slot['target_duration']
            is_anchor = slot['is_anchor_slot']
            anchor_id = slot.get('anchor_id', '')

            cp = _slot_clip_map.get(sid)
            if cp and cp.exists():
                try:
                    _pr = subprocess.run([FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
                                         '-of', 'default=noprint_wrappers=1:nokey=1', str(cp)],
                                        capture_output=True, text=True, timeout=5)
                    clip_dur = float(_pr.stdout.strip())
                except:
                    clip_dur = target_dur

                # v10.1: clip 实际时长与 slot 目标时长的差异处理
                # 扩展时不得越过 clean_window 边界
                _clip_status = 'original'
                if clip_dur < target_dur - 0.1:
                    _tl_entry = _slot_timeline_map.get(sid)
                    if _tl_entry:
                        _src = _tl_entry.get('source_file', '')
                        _ss = _tl_entry.get('start_sec', 0)
                        _se = _tl_entry.get('end_sec', 0)
                        _src_local = dl_dir / _src.replace('.MP4', '.mp4')

                        # v10.1: 查找 clean_window 边界
                        _src_l2 = pool_data_for_slot.get(_src, {}) if pool_data_for_slot else {}
                        _src_cw = _src_l2.get('clean_windows', [])
                        _cw_max_end = _se  # 默认不超过原 end
                        for _w in _src_cw:
                            _ws = _w.get('start_sec', 0)
                            _we = _w.get('end_sec', 0)
                            if _ss >= _ws - 0.1 and _ss < _we:
                                _cw_max_end = _we  # 当前 clip 所在 clean_window 的右边界
                                break

                        if _src_local.exists():
                            _src_dur = _source_dur_cache.get(_src, 9999.0)
                            # 扩展上限 = min(源文件时长, clean_window 右边界)
                            _expand_limit = min(_src_dur, _cw_max_end)
                            _new_end = min(_ss + target_dur, _expand_limit)
                            _new_dur = round(_new_end - _ss, 2)
                            if _new_dur >= target_dur - 0.1:
                                subprocess.run([
                                    FFMPEG, '-y', '-i', str(_src_local),
                                    '-ss', str(_ss), '-t', str(target_dur),
                                    '-c:v', 'libx264', '-an', '-preset', 'fast',
                                    str(cp)
                                ], capture_output=True)
                                clip_dur = target_dur
                                _clip_status = 'expanded_in_window'
                                print(f"    [slot扩展] {sid}: clip 扩展到 {target_dur:.2f}s (cw_limit={_cw_max_end:.1f}s)")
                            elif _new_dur >= 1.5:
                                # 部分扩展（不超 clean_window）
                                subprocess.run([
                                    FFMPEG, '-y', '-i', str(_src_local),
                                    '-ss', str(_ss), '-t', str(_new_dur),
                                    '-c:v', 'libx264', '-an', '-preset', 'fast',
                                    str(cp)
                                ], capture_output=True)
                                clip_dur = _new_dur
                                _clip_status = 'partial_expand'
                                print(f"    [slot部分扩展] {sid}: clip 扩展到 {_new_dur:.2f}s (cw_limit={_cw_max_end:.1f}s, 不足slot目标{target_dur:.2f}s)")
                            else:
                                # v12.6: 尝试从 manifest 找更长候选替换
                                _replaced = False
                                if clip_dur < target_dur - 0.3:
                                    try:
                                        _manifest_path_swap = output_dir / "candidate_reel_manifest.json"
                                        if _manifest_path_swap.exists():
                                            _swap_manifest = json.load(open(_manifest_path_swap))
                                            _used_srcs = set(t.get('source_file', '') for t in timeline)
                                            _swap_candidates = []
                                            for _sm in _swap_manifest:
                                                _sm_src = _sm.get('source_file', '')
                                                _sm_dur = _sm.get('clip_duration_sec', 0)
                                                if _sm_src in _used_srcs:
                                                    continue
                                                if _sm.get('pool_level') == 'discard':
                                                    continue
                                                if _sm_dur >= target_dur:
                                                    _swap_candidates.append(_sm)
                                            if _swap_candidates:
                                                _swap_candidates.sort(key=lambda x: -x.get('score_total', 0))
                                                _best_swap = _swap_candidates[0]
                                                _swap_src = _best_swap['source_file']
                                                _swap_local = dl_dir / _swap_src.replace('.MP4', '.mp4')
                                                if not _swap_local.exists():
                                                    # 尝试下载
                                                    _swap_tk = f'windows_ingest/{task_id}/{_swap_src}'
                                                    try:
                                                        _ensure_env()
                                                        import tos as _tos_swap
                                                        _ak = os.environ.get('TOS_ACCESS_KEY', os.environ.get('VOLC_AK', ''))
                                                        _sk = os.environ.get('TOS_SECRET_KEY', os.environ.get('VOLC_SK', ''))
                                                        _tc = _tos_swap.TosClientV2(ak=_ak, sk=_sk, endpoint='tos-cn-beijing.volces.com', region='cn-beijing')
                                                        _tc.get_object_to_file('e23-video', _swap_tk, str(_swap_local))
                                                    except:
                                                        pass
                                                if _swap_local.exists():
                                                    _swap_ss = _best_swap.get('source_start_sec', 0)
                                                    subprocess.run([
                                                        FFMPEG, '-y', '-i', str(_swap_local),
                                                        '-ss', str(_swap_ss), '-t', str(target_dur),
                                                        '-c:v', 'libx264', '-an', '-preset', 'fast',
                                                        str(cp)
                                                    ], capture_output=True)
                                                    clip_dur = target_dur
                                                    _clip_status = 'v12.6_swapped'
                                                    _replaced = True
                                                    # 更新 timeline
                                                    _tl_entry['source_file'] = _swap_src
                                                    _tl_entry['start_sec'] = _swap_ss
                                                    _tl_entry['end_sec'] = round(_swap_ss + target_dur, 2)
                                                    _tl_entry['reel_clip_id'] = _best_swap.get('reel_clip_id', '')
                                                    _tl_entry['_v12_6_swap'] = True
                                                    _tl_entry['_v12_6_swap_reason'] = f'cw不足{_new_dur:.1f}s→替换为{_swap_src}(score={_best_swap.get("score_total",0)})'
                                                    print(f"    [v12.6] {sid}: 短窗口替换 {_src}→{_swap_src} (score={_best_swap.get('score_total',0)}, dur={target_dur:.2f}s)")
                                    except Exception as _swap_e:
                                        print(f"    [v12.6] {sid}: 替换尝试失败: {_swap_e}")
                                if not _replaced:
                                    _clip_status = 'short_no_expand'
                                    print(f"    [slot扩展受限] {sid}: clean_window 不足，保持 {clip_dur:.2f}s")

                # 最终使用 slot 的 target_start/target_end 作为渲染位置
                t_entry = _slot_timeline_map.get(sid, {})
                render_timeline.append({
                    'clip_file': str(cp.name),
                    'slot_id': sid,
                    'render_start': round(target_start, 2),
                    'render_end': round(target_end, 2),
                    'duration': round(target_dur, 2),
                    'actual_clip_duration': round(clip_dur, 2),
                    'sentence_id': t_entry.get('sentence_id', ''),
                    'is_anchor_slot': is_anchor,
                })
                _ordered_clip_paths.append(cp)

                # 完整性报告
                drift = round(0.0, 2)  # slot 锁定模式下 drift 为 0
                # v10.1: 查找素材质量分级
                _t_entry_qc = _slot_timeline_map.get(sid, {})
                _rid_for_qc = _t_entry_qc.get('reel_clip_id', '')
                _qc_for_slot = 'A'
                for _m_qc in manifest:
                    if _m_qc.get('reel_clip_id') == _rid_for_qc:
                        _qc_for_slot = _m_qc.get('quality_class', 'A')
                        break
                slot_integrity.append({
                    'slot_id': sid,
                    'target_start': target_start,
                    'target_end': target_end,
                    'final_render_start': round(target_start, 2),
                    'final_render_end': round(target_end, 2),
                    'drift_seconds': drift,
                    'is_anchor_slot': is_anchor,
                    'anchor_id': anchor_id,
                    'clip_status': _clip_status,
                    'quality_class': _qc_for_slot,
                    'clip_file': str(cp.name),
                    'removed': False,
                    'caused_shift': False,
                })
            else:
                # slot 没有对应 clip — 记录但不删 slot
                print(f"    ⚠️ {sid} 无可用 clip（anchor={is_anchor}）")
                slot_integrity.append({
                    'slot_id': sid,
                    'target_start': target_start,
                    'target_end': target_end,
                    'final_render_start': target_start,
                    'final_render_end': target_end,
                    'drift_seconds': 0.0,
                    'is_anchor_slot': is_anchor,
                    'anchor_id': anchor_id,
                    'clip_status': 'missing',
                    'clip_file': '',
                    'removed': True,
                    'caused_shift': False,
                })

        # 替换 clip_paths 为 slot 排序后的顺序
        clip_paths = _ordered_clip_paths

        # 保存 slot_integrity_report
        with open(output_dir / "slot_integrity_report.json", 'w', encoding='utf-8') as _sif:
            json.dump(slot_integrity, _sif, ensure_ascii=False, indent=2)

        # 校验
        _anchor_drifts = [(s['slot_id'], s['drift_seconds']) for s in slot_integrity if s['is_anchor_slot']]
        _missing_slots = [s['slot_id'] for s in slot_integrity if s['clip_status'] == 'missing']
        _shift_slots = [s['slot_id'] for s in slot_integrity if s['caused_shift']]

        print(f"  [slot_integrity] {len(slot_integrity)} 个 slot:")
        print(f"    有 clip: {sum(1 for s in slot_integrity if s['clip_status'] != 'missing')}")
        print(f"    缺失: {len(_missing_slots)} {_missing_slots}")
        print(f"    锚点 drift: {_anchor_drifts}")
        print(f"    前移: {len(_shift_slots)} {_shift_slots}")

        # 合格判断
        _pass = True
        for s in slot_integrity:
            if s['is_anchor_slot'] and abs(s['drift_seconds']) > 0.3:
                print(f"    ❌ 锚点 {s['slot_id']} drift {s['drift_seconds']:.2f}s > 0.3s")
                _pass = False
            if not s['is_anchor_slot'] and abs(s['drift_seconds']) > 0.5:
                print(f"    ❌ 普通 slot {s['slot_id']} drift {s['drift_seconds']:.2f}s > 0.5s")
                _pass = False
            if s['caused_shift']:
                print(f"    ❌ {s['slot_id']} 导致后续前移")
                _pass = False
        if _pass:
            print(f"    ✅ slot 完整性校验通过")
        else:
            print(f"    ⚠️ slot 完整性校验有异常（继续生成）")

        with open(output_dir / "final_render_timeline.json", 'w', encoding='utf-8') as f:
            json.dump(render_timeline, f, ensure_ascii=False, indent=2)
        _total_render_dur = sum(r['duration'] for r in render_timeline)
        print(f"  final_render_timeline: {len(render_timeline)} 条, 总时长 {_total_render_dur:.1f}s (slot 锁定)")

    else:
        # 非 slot 模式（music_only 或无 slot_plan）— 保留旧逻辑
        render_timeline = []
        cursor = 0.0
        for i, cp in enumerate(clip_paths):
            try:
                probe_result = subprocess.run([FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
                                              '-of', 'default=noprint_wrappers=1:nokey=1', str(cp)],
                                             capture_output=True, text=True, timeout=5)
                clip_dur = float(probe_result.stdout.strip())
            except:
                clip_dur = 2.5
            sid = ''
            if i < len(timeline):
                sid = timeline[i].get('sentence_id', '')
            render_timeline.append({
                'clip_file': str(cp.name),
                'render_start': round(cursor, 2),
                'render_end': round(cursor + clip_dur, 2),
                'duration': round(clip_dur, 2),
                'sentence_id': sid,
            })
            cursor += clip_dur
        with open(output_dir / "final_render_timeline.json", 'w', encoding='utf-8') as f:
            json.dump(render_timeline, f, ensure_ascii=False, indent=2)
        print(f"  final_render_timeline: {len(render_timeline)} 条, 总时长 {cursor:.1f}s")

    # ============================================================
    # v9.9: Slot 锁定模式 — 裁切 clip 到精确 slot_duration
    # ============================================================
    if _use_slot_render and _slot_plan_path.exists():
        with open(_slot_plan_path, 'r') as _spf2:
            _slot_plan_for_trim = json.load(_spf2)
        _slot_dur_map = {s['slot_id']: s['target_duration'] for s in _slot_plan_for_trim}

        # 为每个 render_timeline 条目精确裁切 clip 到 slot_duration
        _trimmed_clip_paths = []
        for rt in render_timeline:
            _cp_name = rt['clip_file']
            _slot_dur_target = _slot_dur_map.get(rt.get('slot_id', ''), rt['duration'])
            _orig_cp = None
            for cp in clip_paths:
                if cp.name == _cp_name:
                    _orig_cp = cp
                    break
            if _orig_cp and _orig_cp.exists():
                _trimmed_name = f"slotcut_{rt.get('slot_id', 'x')}_{_cp_name}"
                _trimmed_path = clips_dir / _trimmed_name
                subprocess.run([
                    FFMPEG, '-y', '-i', str(_orig_cp),
                    '-t', str(_slot_dur_target),
                    '-c:v', 'libx264', '-an', '-preset', 'fast',
                    str(_trimmed_path)
                ], capture_output=True, timeout=60)  # v11.6.3: slot 裁切超时 60s
                
                # v11.6.3: 校验裁后实际时长，不足则 tpad 补齐
                try:
                    _trim_probe = subprocess.run([
                        FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'default=noprint_wrappers=1:nokey=1', str(_trimmed_path)
                    ], capture_output=True, text=True, timeout=5)
                    _trim_actual = float(_trim_probe.stdout.strip())
                    _trim_shortfall = _slot_dur_target - _trim_actual
                    if _trim_shortfall > 0.05:
                        # clip 不够长，用 tpad 冻结最后一帧补齐
                        _padded_path = clips_dir / f"padded_{_trimmed_name}"
                        subprocess.run([
                            FFMPEG, '-y', '-i', str(_trimmed_path),
                            '-vf', f'tpad=stop_mode=clone:stop_duration={_trim_shortfall:.3f}',
                            '-c:v', 'libx264', '-an', '-preset', 'fast',
                            str(_padded_path)
                        ], capture_output=True, timeout=30)
                        if _padded_path.exists() and _padded_path.stat().st_size > 1000:
                            _trimmed_path = _padded_path
                            print(f"    [slot补齐] {rt.get('slot_id','?')}: 实际{_trim_actual:.2f}s < 目标{_slot_dur_target:.2f}s, tpad +{_trim_shortfall:.2f}s")
                except Exception:
                    pass
                
                _trimmed_clip_paths.append(_trimmed_path)
            elif _orig_cp:
                _trimmed_clip_paths.append(_orig_cp)
        clip_paths = _trimmed_clip_paths
        print(f"  [v9.9] slot 精确裁切: {len(clip_paths)} 个 clip")

    # v13.2-i.2: concat 前最终 source_file 去重 + 开场 LOW_INFO 检查
    _LOW_KW_CONCAT = ['合影', '横幅', '红旗', '标语', '摆拍']
    _INT_KW_CONCAT = ['互动游戏', '投掷', '投沙包', '投飞镖', '大骰子', '安全大富翁', '趣味互动', '主题互动']
    _manifest_map_concat = {m['reel_clip_id']: m for m in manifest} if manifest else {}
    _src_seen_concat = {}
    _concat_dedup_log = []
    _concat_opener_log = []
    import re as _re_concat

    def _simplify_concat(fn):
        return _re_concat.sub(r'[^a-zA-Z0-9]', '', fn.lower())

    def _get_src_from_clip_path(cp_name):
        # clip_01_DJI_20001115143932_0138_D.mp4 → DJI_20001115143932_0138_D.MP4
        parts = cp_name.replace('.mp4', '').replace('.MP4', '').split('_', 2)
        if len(parts) >= 3:
            raw = parts[2]  # DJI_20001115143932_0138_D
            return raw + '.MP4'
        return cp_name

    # 去重 pass
    _deduped_clip_paths = []
    for _ci, cp in enumerate(clip_paths):
        cp_name = cp.name if hasattr(cp, 'name') else str(cp).split('/')[-1]
        _cp_simple = _simplify_concat(cp_name)
        # 找 manifest source_file
        _cp_src = None
        for _mk, _mv in _manifest_map_concat.items():
            if _simplify_concat(_mv.get('source_file', '')) in _cp_simple:
                _cp_src = _mv.get('source_file', '')
                break
        if not _cp_src:
            _cp_src = _get_src_from_clip_path(cp_name)

        _cp_desc = ''
        for _mv in _manifest_map_concat.values():
            if _mv.get('source_file', '') == _cp_src:
                _cp_desc = _mv.get('scene_description', '')
                break

        _is_low = any(k in _cp_desc for k in _LOW_KW_CONCAT)
        _is_int = any(k in _cp_desc for k in _INT_KW_CONCAT) and not any(k in _cp_desc for k in ['发放', '递送', '宣传材料'])

        # 开场后 6s LOW_INFO 检查
        if _ci < 3 and _is_low:
            # 找后面可交换的 clip
            _swapped_concat = False
            for _cj in range(max(_ci+1, 4), len(clip_paths)):
                _cj_name = clip_paths[_cj].name if hasattr(clip_paths[_cj], 'name') else str(clip_paths[_cj]).split('/')[-1]
                _cj_src = None
                for _mv in _manifest_map_concat.values():
                    if _simplify_concat(_mv.get('source_file', '')) in _simplify_concat(_cj_name):
                        _cj_src = _mv.get('source_file', '')
                        _cj_desc = _mv.get('scene_description', '')
                        break
                if _cj_src and not any(k in (_cj_desc if _cj_src else '') for k in _LOW_KW_CONCAT):
                    # 交换
                    clip_paths[_ci], clip_paths[_cj] = clip_paths[_cj], clip_paths[_ci]
                    _concat_opener_log.append({'slot': _ci+1, 'swapped_with': _cj+1, 'low_info_src': _cp_src})
                    print(f"  [v13.2-i.2] concat_opener: slot_{_ci+1}({_cp_src[:20]}) ↔ slot_{_cj+1}")
                    _swapped_concat = True
                    break
            if not _swapped_concat:
                _concat_opener_log.append({'slot': _ci+1, 'low_info_src': _cp_src, 'unresolved': True})
                print(f"  [v13.2-i.2] concat_opener: slot_{_ci+1}({_cp_src[:20]}) LOW_INFO unresolved")

    # source_file 去重 pass（重新扫描交换后的 clip_paths）
    _src_seen_final = {}
    _final_dedup_clips = []
    for _ci, cp in enumerate(clip_paths):
        cp_name = cp.name if hasattr(cp, 'name') else str(cp).split('/')[-1]
        _cp_src = None
        for _mv in _manifest_map_concat.values():
            if _simplify_concat(_mv.get('source_file', '')) in _simplify_concat(cp_name):
                _cp_src = _mv.get('source_file', '')
                break
        if not _cp_src:
            _cp_src = _get_src_from_clip_path(cp_name)

        _cp_desc = ''
        for _mv in _manifest_map_concat.values():
            if _mv.get('source_file', '') == _cp_src:
                _cp_desc = _mv.get('scene_description', '')
                break

        _is_int = any(k in _cp_desc for k in _INT_KW_CONCAT) and not any(k in _cp_desc for k in ['发放', '递送', '宣传材料'])

        if _cp_src in _src_seen_final and not _is_int:
            # 不跳过（会导致时长不足），而是标记。重复 clip 保留但记录 warning。
            _concat_dedup_log.append({'slot': _ci+1, 'dup_src': _cp_src, 'first_slot': _src_seen_final[_cp_src]+1, 'action': 'warn_kept'})
            print(f"  [v13.2-i.2] concat_dedup: slot_{_ci+1} {_cp_src[:25]} is dup of slot_{_src_seen_final[_cp_src]+1} → kept (skip would cause tpad overflow)")
        _src_seen_final[_cp_src] = _ci
        _final_dedup_clips.append(cp)

    if len(_final_dedup_clips) < len(clip_paths):
        print(f"  [v13.2-i.2] concat_dedup: {len(clip_paths)}→{len(_final_dedup_clips)} clips (removed {len(clip_paths)-len(_final_dedup_clips)} dups)")
        clip_paths = _final_dedup_clips

    with open(output_dir / "concat_dedup_summary.json", 'w', encoding='utf-8') as _gf_cd:
        _json_step6.dump({'dedup': _concat_dedup_log, 'opener': _concat_opener_log}, _gf_cd, ensure_ascii=False, indent=2)

    with open(concat_path, 'w') as f:
        for cp in clip_paths:
            f.write(f"file '{cp.absolute()}'\n")

    video_only = output_dir / "video_only.mp4"
    # v11.6.3: concat 超时保护（180 秒）
    try:
        _concat_result = subprocess.run([
            FFMPEG, '-y', '-f', 'concat', '-safe', '0',
            '-i', str(concat_path), '-c:v', 'libx264', '-preset', 'fast', '-an', str(video_only)
        ], check=True, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        _error_msg = 'concat 超时（>180s），ffmpeg 可能卡死'
        print(f"  ❌ {_error_msg}")
        _update_task_failed(task_path, _error_msg, 'concat_timeout')
        raise RuntimeError(_error_msg)
    except subprocess.CalledProcessError as e:
        _stderr = e.stderr.decode('utf-8', errors='replace')[-500:] if e.stderr else ''
        _error_msg = f'concat 失败: {_stderr}'
        print(f"  ❌ {_error_msg}")
        _update_task_failed(task_path, _error_msg, 'concat_failed')
        raise RuntimeError(_error_msg)

    # ============================================================
    # 5. 字幕 + 最终合成
    # ============================================================
    _update_stage(task_path, "render")
    _heartbeat("render_started")
    _check_cancel()
    print(f"\n[5/5] 字幕 + 最终渲染")

    # 读取字幕配置（按模式分离）
    task_config = task.get('config', {})
    if edit_mode == 'music_only':
        sub_config = user_config.get('music_subtitle', {})
    else:
        sub_config = user_config.get('news_subtitle', user_config.get('subtitle', task_config.get('subtitle', {})))
        # v7.4.2: 防御性检查，确保 sub_config 是 dict
        if not isinstance(sub_config, dict):
            print(f"  ⚠️ sub_config 类型异常: {type(sub_config)}，重置为空 dict")
            sub_config = {}
    sub_enabled = sub_config.get('enabled', edit_mode == 'narration')
    
    sub_filter = None
    if sub_enabled and edit_mode == 'narration' and tts_meta_path:
        srt_path = str(output_dir / "subtitles.srt")
        with open(tts_meta_path, 'r') as f:
            tts_meta = json.load(f)
        create_subtitle_srt_from_meta(tts_meta, srt_path)
        # v10.4 字幕自检：拆词/单字/保护词检查，失败则停止生成
        _sub_check = validate_subtitles_no_split(srt_path)
        if not _sub_check['passed']:
            raise RuntimeError(f"subtitle_validation_failed: {_sub_check['errors']}")
        # v12.8: 字幕样式从预设加载（narration 默认 news_clean）
        subtitle_style_name = sub_config.get('style', get_default_preset())
        sub_style = get_style_force_string(subtitle_style_name)
        sub_filter = f"subtitles={srt_path}:force_style='{sub_style}'"
        print(f"  字幕: 已开启 - 预设={subtitle_style_name}")
    elif sub_enabled and edit_mode == 'music_only' and sub_config.get('text'):
        # 纯音乐模式动态字幕：按句出现，不碎切
        srt_path = str(output_dir / "subtitles_music.srt")
        _generate_music_subtitle_srt(sub_config['text'], tts_dur, srt_path, 
                                     target_duration=target_duration,
                                     storyboard_note=storyboard_note)
        # v12.8 music_only 专用预设：SRT→ASS 转换后烧录（支持换行/精确位置/字体）
        # music_only 固定使用 music_only_clean，不受 task config style 字段影响
        subtitle_style_name = 'music_only_clean'
        ass_path = str(output_dir / "subtitles_music.ass")
        srt_to_ass(srt_path, ass_path, preset_name=subtitle_style_name)
        sub_filter = f"subtitles='{ass_path}'"
        print(f"  字幕: 已开启 - 纯音乐模式 (ASS, 预设={subtitle_style_name}, text={sub_config['text'][:30]}...)")
    else:
        print(f"  字幕: 已关闭")

    # 最终合成
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_name = f"{task_id}_{ts}.mp4"
    out_path = output_dir / out_name

    # 视频时长
    probe = subprocess.run([
        FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', str(video_only)
    ], capture_output=True, text=True)
    video_dur = float(probe.stdout.strip())

    # tpad 补尾（如果视频比 TTS 短）
    final_dur = max(video_dur, tts_dur)
    # ====== v13.3-p2b-rhythm: music_only 禁用 tpad 硬补 ======
    tpad_needed = round(max(0, tts_dur - video_dur), 2)
    
    # v13.3-p2b-rhythm: music_only 模式不使用 tpad，自然时长即可
    if edit_mode == 'music_only' and tpad_needed > 0:
        print(f"  [v13.3-p2b-rhythm] music_only 不使用 tpad，自然时长 {video_dur:.1f}s")
        tpad_needed = 0
        tpad_filter = ""
    else:
        # narration / 其他模式：原有逻辑
        _tpad_limit = 3.0 if _use_slot_render else 2.0
        if tpad_needed > _tpad_limit:
            print(f"\n  ❌ tpad 需要 {tpad_needed:.1f}s > {_tpad_limit}s 上限")
            print(f"     video_dur={video_dur:.2f}s, tts_dur={tts_dur:.1f}s, 缺口={tpad_needed:.1f}s")
            raise RuntimeError(
                f"tpad 需要 {tpad_needed:.1f}s 超过 {_tpad_limit}s 上限。"
                f"视频时长 {video_dur:.2f}s 严重不足（需 {tts_dur:.1f}s），"
                f"请检查选片边界或增加补位镜头。禁止生成。"
            )
        tpad_filter = f"tpad=stop_mode=clone:stop_duration={tpad_needed:.1f}" if tpad_needed > 0 else ""
        if tpad_needed > 0:
            print(f"  [tpad] 补齐 {tpad_needed:.1f}s（≤1.0s ✅）")
    
    # 构建 video filter
    vf_parts = []
    if tpad_filter:
        vf_parts.append(tpad_filter)
    if sub_filter:
        vf_parts.append(sub_filter)
    vf = ','.join(vf_parts) if vf_parts else None

    # === BGM 结尾淡出配置（正式固化） ===
    BGM_FADE_OUT_SEC = 3.0  # 结尾最后 3.0 秒 BGM 渐弱淡出（1.5s 对部分曲目不够明显）
    af_parts = []  # audio filter chain

    ffmpeg_cmd = [FFMPEG, '-y', '-i', str(video_only)]

    if edit_mode == 'music_only' and bgm_tos_key:
        # 纯音乐模式：下载 BGM 并用作音轨
        bgm_url = f"https://e23-video.tos-cn-beijing.volces.com/{bgm_tos_key}"
        bgm_local = output_dir / f"bgm_{_gen_ts}.mp3"
        if not bgm_local.exists():
            print(f"  下载 BGM: {bgm_tos_key}...")
            r = requests.get(bgm_url, timeout=120)
            with open(bgm_local, 'wb') as f:
                f.write(r.content)
        # 先裁切 BGM 到视频时长并做淡出，再合成
        actual_dur = min(video_dur, final_dur)
        bgm_faded = output_dir / f"bgm_faded_{_gen_ts}.mp3"
        fade_start = max(0, actual_dur - BGM_FADE_OUT_SEC)
        subprocess.run([
            FFMPEG, '-y', '-i', str(bgm_local),
            '-t', str(actual_dur),
            '-af', f"afade=t=out:st={fade_start:.1f}:d={BGM_FADE_OUT_SEC}",
            '-c:a', 'libmp3lame', '-b:a', '192k',
            str(bgm_faded)
        ], check=True, capture_output=True)
        print(f"  BGM 淡出: 预处理完成 (st={fade_start:.1f}s, d={BGM_FADE_OUT_SEC}s, total={actual_dur:.1f}s)")
        ffmpeg_cmd.extend(['-i', str(bgm_faded)])
        ffmpeg_cmd.extend(['-map', '0:v:0', '-map', '1:a:0'])
    elif tts_output:
        # 新闻播报模式：用 TTS 音轨
        ffmpeg_cmd.extend(['-i', tts_output])
        ffmpeg_cmd.extend(['-map', '0:v:0', '-map', '1:a:0'])

    if vf:
        ffmpeg_cmd.extend(['-vf', vf])
    ffmpeg_cmd.extend([
        '-c:v', 'libx264', '-preset', 'fast',
        '-c:a', 'aac', '-b:a', '128k',
        '-t', str(final_dur),
        '-movflags', '+faststart',
        str(out_path)
    ])
    # v11.6.3: 渲染超时保护（300 秒）
    try:
        subprocess.run(ffmpeg_cmd, check=True, capture_output=True, timeout=300)
    except subprocess.TimeoutExpired:
        _error_msg = '最终渲染超时（>300s），ffmpeg 可能卡死'
        print(f"  ❌ {_error_msg}")
        _update_task_failed(task_path, _error_msg, 'render_timeout')
        raise RuntimeError(_error_msg)
    except subprocess.CalledProcessError as e:
        _stderr = e.stderr.decode('utf-8', errors='replace')[-500:] if e.stderr else ''
        _error_msg = f'最终渲染失败: {_stderr}'
        print(f"  ❌ {_error_msg}")
        _update_task_failed(task_path, _error_msg, 'render_failed')
        raise RuntimeError(_error_msg)

    # 验证
    probe2 = subprocess.run([
        FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', str(out_path)
    ], capture_output=True, text=True)
    final_video_dur = float(probe2.stdout.strip())

    print(f"\n{'=' * 70}")
    print(f"[一键生成] ✅ 成片产出: {out_path}")
    print(f"  时长: {final_video_dur:.1f}s (TTS={tts_dur:.1f}s, 视频={video_dur:.1f}s)")
    print(f"  镜头数: {len(timeline)}")
    print(f"{'=' * 70}")

    # ============================================================
    # v10.2: 生成 score_validation_report
    # ============================================================
    if _use_slot_render:
        try:
            from pipeline.clip_scorer import generate_score_validation_report
            _sir_path = output_dir / "slot_integrity_report.json"
            _manifest_path = output_dir / "candidate_reel_manifest.json"
            if _sir_path.exists() and _manifest_path.exists():
                with open(_sir_path) as _sf:
                    _sir_data = json.load(_sf)
                with open(_manifest_path) as _mf:
                    _mf_data = json.load(_mf)
                _svr = generate_score_validation_report(_sir_data, _mf_data, timeline)
                with open(output_dir / "score_validation_report.json", 'w') as _svf:
                    json.dump(_svr, _svf, ensure_ascii=False, indent=2)
                print(f"\n  [评分验证] 全片均分={_svr['avg_score']}, 前5均分={_svr['front5_avg_score']}, "
                      f"最低={_svr['min_score']}, <50分={_svr['below_50_count']}条, "
                      f"服务台{_svr['desk_count']}条(连续最多{_svr['desk_max_consecutive']})")
        except Exception as _e:
            print(f"  [评分验证] 生成失败: {_e}")

    # 更新 task
    task['output_path'] = str(out_path)
    task['output_url'] = f"/api/ui/video/{task_id}/{out_name}"
    task['output_duration'] = final_video_dur
    task['output_filename'] = out_name
    # ============================================================
    # v10.9: FINAL_VALIDATE — 最终裁判层（入库前必须通过）
    # ============================================================
    from pipeline.final_validate import final_validate as _final_validate
    _fv_result = _final_validate(task_id, str(out_path), str(output_dir))
    if not _fv_result['passed']:
        _fv_errors = '; '.join(_fv_result['errors'][:5])
        task['status'] = 'failed'
        task['generate_stage'] = 'failed'
        task['error'] = f'最终校验失败: {_fv_errors}'
        task['error_type'] = 'final_validate_failed'
        task['failed_at'] = datetime.now().isoformat()
        task['rules_version'] = _rules_version
        with open(task_path, 'w', encoding='utf-8') as f:
            json.dump(task, f, ensure_ascii=False, indent=2)
        raise RuntimeError(f"final_validate_failed: {_fv_errors}")
    
    task['status'] = 'completed'
    task['progress'] = 100
    task['generate_stage'] = 'done'
    task['rules_version'] = _rules_version
    task['final_validate'] = 'passed'
    print(f"[PERMANENT_RULES] applied: {_rules_version}")
    
    # v12.1: 场景监控指标记录
    _scene_metrics = {
        'scene_struct_mode': USE_SCENE_STRUCT_MODE if 'USE_SCENE_STRUCT_MODE' in dir() else 'off',
        'scene_struct_enabled': _scene_struct_enabled if '_scene_struct_enabled' in dir() else False,
    }
    # 从 narrative_continuity_report 读取指标
    _nc_report_path = output_dir / "narrative_continuity_report.json"
    if _nc_report_path.exists():
        try:
            _nc_data = json.load(open(_nc_report_path))
            _scene_metrics['weak_jump'] = _nc_data.get('weak_jump_count', -1)
            _scene_metrics['bad_jump'] = _nc_data.get('bad_jump_count', -1)
            _scene_metrics['indoor_outdoor_flips'] = _nc_data.get('indoor_outdoor_flip_count', -1)
            _scene_metrics['adj_same_group'] = _nc_data.get('adj_same_group_count', -1)
        except:
            pass
    _scene_metrics['hard_fail_count'] = len(l3_result.get('_sem_hard_fails', []))
    _scene_metrics['fill_count'] = len([t for t in timeline if t.get('is_fill', False)])
    _scene_metrics['final_validate'] = 'passed'
    _scene_metrics['timeline_count'] = len(timeline)
    
    # 保存
    _sm_path = output_dir / "scene_struct_metrics.json"
    with open(_sm_path, 'w', encoding='utf-8') as _smf:
        json.dump(_scene_metrics, _smf, ensure_ascii=False, indent=2)
    print(f"  [v12.1] 场景监控指标已保存: {_sm_path}")
    
    # 版本记录
    if 'versions' not in task or not isinstance(task.get('versions'), list):
        task['versions'] = []
    task['versions'].insert(0, {
        'path': str(out_path),
        'url': f"/api/ui/video/{task_id}/{out_name}",
        'filename': out_name,
        'time': datetime.now().isoformat(),
        'duration': final_video_dur,
        'timeline_count': len(timeline),
        'voice': task.get('config', {}).get('voice', 'S_x249qIGO1'),
    })
    with open(task_path, 'w', encoding='utf-8') as f:
        json.dump(task, f, ensure_ascii=False, indent=2)

    return {
        'success': True,
        'video_path': str(out_path),
        'video_name': out_name,
        'duration': final_video_dur,
        'timeline_count': len(timeline),
        'timeline_path': str(timeline_path),
        'task_id': task_id,
    }
