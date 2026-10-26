# -*- coding: utf-8 -*-
"""语音消息转文字：QQ 的 record 段只有音频文件，转写走 SiliconFlow SenseVoice。

- 取音频顺序：NapCat get_record(out_format=mp3) 的本地路径 / base64 / 下载地址，
  再退回记录段自带的 path/url；下载地址一律先过公网校验（防 SSRF）。
- 转写结果按音频内容 md5 缓存 1 小时：同一条语音被引用展开时不会重复计费。
- 任何一步失败都返回空串，由调用方降级成「没听清」，不影响正常回复。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import threading
import time
from pathlib import Path

import httpx

from richmsg import url_is_public

logger = logging.getLogger("jinyu_memory")

MAX_AUDIO = 20 * 1024 * 1024
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_cache: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()
_CACHE_TTL = 3600.0


def _off(value: str | None) -> bool:
    return (value or "").strip().lower() in {"0", "false", "no", "off", "disabled"}


def enabled() -> bool:
    """有密钥且没被显式关闭才启用。"""
    if _off(os.environ.get("RP_ASR")):
        return False
    return bool((os.environ.get("RP_ASR_KEY") or "").strip())


def _base() -> str:
    return (os.environ.get("RP_ASR_BASE")
            or "https://api.siliconflow.cn/v1").rstrip("/")


def model_name() -> str:
    return (os.environ.get("RP_ASR_MODEL")
            or "FunAudioLLM/SenseVoiceSmall").strip()


def _timeout() -> float:
    try:
        return max(10.0, float(os.environ.get("RP_ASR_TIMEOUT") or 60))
    except ValueError:
        return 60.0


def transcribe(data: bytes) -> str:
    """音频字节 → 文本；未启用/失败返回空串。"""
    if not data or not enabled():
        return ""
    digest = hashlib.md5(data).hexdigest()
    now = time.time()
    with _lock:
        hit = _cache.get(digest)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]
    try:
        with httpx.Client(timeout=_timeout()) as client:
            resp = client.post(
                f"{_base()}/audio/transcriptions",
                headers={"Authorization":
                         f"Bearer {os.environ['RP_ASR_KEY'].strip()}"},
                files={"file": ("voice.mp3", data, "audio/mpeg")},
                data={"model": model_name()},
            )
    except Exception as exc:                      # noqa: BLE001
        logger.warning(f"[jinyu] 语音转写请求失败: {exc}")
        return ""
    if resp.status_code != 200:
        logger.warning(f"[jinyu] 语音转写失败 status={resp.status_code} "
                       f"model={model_name()}")
        return ""
    try:
        text = str((resp.json() or {}).get("text") or "").strip()
    except ValueError:
        return ""
    if text:
        with _lock:
            _cache[digest] = (now, text)
    return text


def _read_local(path: str) -> bytes:
    if not path:
        return b""
    try:
        p = Path(path)
        if p.is_file() and 0 < p.stat().st_size <= MAX_AUDIO:
            return p.read_bytes()
    except OSError:
        pass
    return b""


def _download(url: str) -> bytes:
    if not url.startswith(("http://", "https://")) or not url_is_public(url):
        return b""
    try:
        with httpx.stream("GET", url, headers={"User-Agent": UA},
                          timeout=30, follow_redirects=True) as resp:
            if resp.status_code != 200:
                return b""
            buf = bytearray()
            for chunk in resp.iter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_AUDIO:
                    return b""
            return bytes(buf)
    except Exception:                             # noqa: BLE001
        return b""


async def fetch_voice(bot, data: dict) -> bytes:
    """从 OneBot record 段取回音频字节（优先让 NapCat 转成 mp3）。"""
    data = data or {}
    file = str(data.get("file") or data.get("file_id") or "")
    if file:
        try:
            res = await bot.call_api("get_record", file=file, out_format="mp3")
        except Exception as exc:                  # noqa: BLE001  协议端不支持就退回直取
            logger.info(f"[jinyu] get_record 失败，改用记录段地址: {exc}")
            res = None
        payload = res.get("data") if isinstance(res, dict) else None
        if not isinstance(payload, dict):
            payload = res if isinstance(res, dict) else {}
        got = _read_local(str(payload.get("file") or ""))
        if got:
            return got
        b64 = str(payload.get("base64") or "")
        if b64:
            try:
                raw = base64.b64decode(b64.split(",")[-1])
            except (ValueError, TypeError):
                raw = b""
            if 0 < len(raw) <= MAX_AUDIO:
                return raw
        got = _download(str(payload.get("url") or ""))
        if got:
            return got
    got = _read_local(str(data.get("path") or ""))
    if got:
        return got
    return _download(str(data.get("url") or ""))
