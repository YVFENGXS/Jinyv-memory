# -*- coding: utf-8 -*-
"""M1 语音通话：一句录音 → ASR → 文本 → TTS（mp3 bytes）。

只处理音频这一层，不碰记忆与人设：
- ASR 复用 asr.py 的 SiliconFlow 配置（RP_ASR_KEY / RP_ASR_BASE / RP_ASR_MODEL），
  但按浏览器实际录到的格式（webm/ogg/mp4）命名上传，避免被当成 mp3 拒收；
- TTS 走 edge-tts（RP_TTS_VOICE / RP_TTS_DIR），按内容 sha1 缓存，失败返回 None；
- 没有 ASR 密钥时明确返回 mode="off" 和空文本，绝不编造识别结果。

环境变量：
  RP_ASR_KEY / RP_ASR_BASE / RP_ASR_MODEL / RP_ASR_TIMEOUT   语音识别
  RP_TTS_VOICE（默认 zh-CN-XiaoyiNeural）/ RP_TTS_DIR          语音合成
  RP_CALL_TTS_LIMIT（默认 300 字）                              单轮朗读上限
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import time
from pathlib import Path

import httpx

logger = logging.getLogger("jinyu.call")

ROOT = Path(__file__).resolve().parent
MAX_AUDIO = 20 * 1024 * 1024

_EXT = {
    "audio/webm": ("webm", "audio/webm"),
    "audio/ogg": ("ogg", "audio/ogg"),
    "audio/mp4": ("m4a", "audio/mp4"),
    "audio/aac": ("aac", "audio/aac"),
    "audio/mpeg": ("mp3", "audio/mpeg"),
    "audio/wav": ("wav", "audio/wav"),
    "audio/x-wav": ("wav", "audio/wav"),
    "audio/flac": ("flac", "audio/flac"),
}


# ---------------------------------------------------------------- ASR

def asr_ready() -> bool:
    return bool((os.environ.get("RP_ASR_KEY") or "").strip())


def _asr_base() -> str:
    return (os.environ.get("RP_ASR_BASE")
            or "https://api.siliconflow.cn/v1").rstrip("/")


def _asr_model() -> str:
    return (os.environ.get("RP_ASR_MODEL")
            or "FunAudioLLM/SenseVoiceSmall").strip()


def _asr_timeout() -> float:
    try:
        return max(10.0, float(os.environ.get("RP_ASR_TIMEOUT") or 45))
    except ValueError:
        return 45.0


def _file_for(mime: str) -> tuple[str, str]:
    base = (mime or "").split(";")[0].strip().lower()
    ext, real = _EXT.get(base, ("webm", "audio/webm"))
    return f"voice.{ext}", real


def transcribe(data: bytes, mime: str = "audio/webm") -> dict:
    """音频字节 → {"text": str, "mode": "real"|"off", "ms": int}。

    没有密钥或调用失败都返回空文本：由前端提示「没听清」，不编内容。
    """
    started = time.perf_counter()
    if not data:
        return {"text": "", "mode": "off", "ms": 0, "error": "空音频"}
    if not asr_ready():
        return {"text": "", "mode": "off", "ms": 0,
                "error": "未配置 RP_ASR_KEY，语音识别不可用；可以直接打字"}
    if len(data) > MAX_AUDIO:
        return {"text": "", "mode": "off", "ms": 0, "error": "音频太大"}
    name, real_mime = _file_for(mime)
    try:
        with httpx.Client(timeout=_asr_timeout()) as client:
            resp = client.post(
                f"{_asr_base()}/audio/transcriptions",
                headers={"Authorization": f"Bearer {os.environ['RP_ASR_KEY'].strip()}"},
                files={"file": (name, data, real_mime)},
                data={"model": _asr_model()},
            )
    except Exception as exc:                    # noqa: BLE001
        logger.warning("[call] asr 请求失败: %s", exc)
        return {"text": "", "mode": "off", "ms": _ms(started),
                "error": f"语音识别请求失败：{type(exc).__name__}"}
    if resp.status_code != 200:
        logger.warning("[call] asr HTTP %s", resp.status_code)
        return {"text": "", "mode": "off", "ms": _ms(started),
                "error": f"语音识别失败：HTTP {resp.status_code}"}
    try:
        text = str((resp.json() or {}).get("text") or "").strip()
    except ValueError:
        text = ""
    return {"text": text, "mode": "real", "ms": _ms(started)}


# ---------------------------------------------------------------- TTS

def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def tts_ready() -> bool:
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        return False
    return True


def tts_voice() -> str:
    return (os.environ.get("RP_TTS_VOICE") or "zh-CN-XiaoyiNeural").strip()


def tts_dir() -> Path:
    return Path(os.environ.get("RP_TTS_DIR") or str(ROOT / "tts_cache")).expanduser()


def _tts_limit() -> int:
    try:
        return max(60, int(os.environ.get("RP_CALL_TTS_LIMIT") or 300))
    except ValueError:
        return 300


_STRIP_RE = re.compile(r"[*#`>]|\[\[.*?\]\]|（[^（）]{1,24}）|\([^()]{1,24}\)")


def speakable(text: str) -> str:
    """去掉不适合朗读的标记（星号动作、方括号指令、括号旁白），限长。"""
    out = _STRIP_RE.sub("", str(text or ""))
    out = re.sub(r"\s+", " ", out).strip()
    limit = _tts_limit()
    if len(out) > limit:
        cut = max(out.rfind(ch, 0, limit) for ch in "。！？；，")
        out = out[:cut + 1] if cut > limit * 0.6 else out[:limit] + "。"
    return out


def split_sentences(text: str, first_limit: int = 40) -> tuple[str, str]:
    """拆成「首句」与「剩余」：首句尽量短，先把声音放出来。"""
    text = str(text or "").strip()
    if not text:
        return "", ""
    match = re.search(r"[。！？!?\n]", text[:first_limit + 10])
    if match and match.end() >= 4:
        return text[:match.end()].strip(), text[match.end():].strip()
    if len(text) <= first_limit:
        return text, ""
    return text[:first_limit].strip(), text[first_limit:].strip()


async def tts_bytes(text: str) -> dict:
    """文本 → {"audio": bytes|None, "mode": "real"|"off", "ms": int}。"""
    started = time.perf_counter()
    text = speakable(text)
    if not text:
        return {"audio": None, "mode": "off", "ms": 0, "error": "没有可朗读的文字"}
    try:
        import edge_tts
    except ImportError:
        return {"audio": None, "mode": "off", "ms": 0, "error": "缺少 edge-tts"}
    cache = tts_dir()
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        cache = Path(os.environ.get("TEMP") or str(ROOT)) / "jinyu_tts"
        cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{tts_voice()}|{text}".encode()).hexdigest()[:16]
    out = cache / f"{key}.mp3"
    if out.exists() and out.stat().st_size > 512:
        try:
            return {"audio": out.read_bytes(), "mode": "real", "ms": _ms(started)}
        except OSError:
            pass
    tmp = out.with_suffix(".tmp")
    try:
        await edge_tts.Communicate(text, tts_voice()).save(str(tmp))
        if tmp.exists() and tmp.stat().st_size > 512:
            shutil.move(str(tmp), str(out))
            return {"audio": out.read_bytes(), "mode": "real", "ms": _ms(started)}
    except Exception as exc:                    # noqa: BLE001
        logger.warning("[call] tts 失败: %s", exc)
        try:
            tmp.unlink()
        except OSError:
            pass
        return {"audio": None, "mode": "off", "ms": _ms(started),
                "error": f"语音合成失败：{type(exc).__name__}"}
    return {"audio": None, "mode": "off", "ms": _ms(started), "error": "语音合成无输出"}
