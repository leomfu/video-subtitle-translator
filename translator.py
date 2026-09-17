"""翻译模块：translate_segments(texts, api_key, progress_cb)。

用 DeepSeek API 翻译（需要 API key，优先用请求传入的 key，其次回退 .env）。
"""
import json
import os
import re
import time

import requests

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"
BATCH_SIZE = 20
MAX_RETRIES = 3

SYSTEM_PROMPT = (
    "你是专业的影视字幕翻译。用户会给你若干条带编号的字幕原文，"
    "请把每条翻译成简体中文，风格口语自然、简洁，像电影字幕。"
    "以 JSON 对象返回：{\"translations\": [{\"i\": 编号, \"zh\": \"中文译文\"}, ...]}，"
    "translations 数组的数量和编号必须与输入一一对应，不要合并、拆分或遗漏任何一条，"
    "不要输出 JSON 以外的任何内容。"
)

SINGLE_PROMPT = (
    "把下面这句字幕翻译成简体中文，口语自然简洁，像电影字幕。只输出译文本身，"
    "不要任何解释或引号。"
)


class TranslationError(Exception):
    pass


def server_key():
    """.env 里的站长 key。设置了 ACCESS_PASSWORD 即视为共享部署，不回退，防止访客花站长的额度。"""
    if os.environ.get("ACCESS_PASSWORD", "").strip():
        return ""
    return os.environ.get("DEEPSEEK_API_KEY", "").strip()


def _resolve_key(api_key):
    key = (api_key or "").strip() or server_key()
    if not key:
        raise TranslationError(
            "没有提供 DeepSeek API key。请在页面上填写 key。"
            "key 可在 https://platform.deepseek.com 申请。"
        )
    return key


def _chat(messages, key, timeout=120, json_mode=True):
    payload = {"model": MODEL, "messages": messages, "temperature": 0.2,
               "max_tokens": 4000, "stream": False}
    if json_mode:
        # DeepSeek 的 JSON 严格模式：强制返回合法 JSON，避免夹带说明文字
        payload["response_format"] = {"type": "json_object"}
    resp = requests.post(
        DEEPSEEK_URL,
        headers={"Authorization": f"Bearer {key}"},
        json=payload,
        timeout=timeout,
    )
    if resp.status_code == 401:
        raise TranslationError("DeepSeek API key 无效（401），请检查填写的 key 是否正确。")
    if resp.status_code == 402:
        raise TranslationError("DeepSeek 账户余额不足（402），请到官网充值后再试。")
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _parse_items(text):
    """从模型响应中尽量宽容地取出 [{i, zh}, ...] 列表。"""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())  # 剥掉代码围栏
    try:
        data = json.loads(text)
    except ValueError:
        m = re.search(r"\[.*\]", text, re.S)  # 退而求其次：抓文本里的数组
        if not m:
            raise ValueError("响应中未找到 JSON 数组")
        data = json.loads(m.group(0))
    if isinstance(data, dict):
        # JSON 模式返回对象：取 translations 键，或对象里第一个列表值
        data = data.get("translations") or next(
            (v for v in data.values() if isinstance(v, list)), None)
    if not isinstance(data, list):
        raise ValueError("响应 JSON 里没有翻译列表")
    return data


def _translate_single(text, key):
    """逐句降级翻译：普通文本模式，不要求 JSON。失败则返回英文原文兜底。"""
    for attempt in range(2):
        try:
            content = _chat([
                {"role": "system", "content": SINGLE_PROMPT},
                {"role": "user", "content": text},
            ], key, json_mode=False)
            result = content.strip().strip('"').strip()
            if result:
                return result
        except TranslationError:
            raise  # key/余额问题降级也救不了
        except Exception:
            time.sleep(1 + attempt)
    return text  # 实在翻不出来就保留原文，绝不让整个任务失败


def _translate_batch(texts, start_index, key):
    numbered = "\n".join(f"{start_index + i}. {t}" for i, t in enumerate(texts))
    content = None
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            content = _chat([
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": numbered},
            ], key)
            items = _parse_items(content)
            by_index = {int(it["i"]): str(it["zh"]).strip() for it in items}
            return [by_index[start_index + i] for i in range(len(texts))]
        except TranslationError:
            raise  # key/余额问题重试无意义
        except Exception as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    # 批量重试均失败：降级为逐句翻译，保证长视频任务不会因一批格式问题整体中断
    print(f"[translator] 批量翻译失败（{last_err}），降级为逐句翻译。"
          f"原始响应片段：{(content or '')[:200]!r}", flush=True)
    return [_translate_single(t, key) for t in texts]


def translate_segments(texts, api_key=None, progress_cb=None):
    """把英文文本列表翻译成中文列表，顺序一一对应。

    api_key: DeepSeek API key（None 则回退 .env）。
    progress_cb(done, total): 每完成一批回调一次。
    """
    if not texts:
        return []
    key = _resolve_key(api_key)
    results = []
    total = len(texts)
    for start in range(0, total, BATCH_SIZE):
        batch = texts[start:start + BATCH_SIZE]
        results.extend(_translate_batch(batch, start + 1, key))
        if progress_cb:
            progress_cb(min(start + BATCH_SIZE, total), total)
    return results
