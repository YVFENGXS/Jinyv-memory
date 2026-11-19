# -*- coding: utf-8 -*-
"""QQ 掉线兜底：把主动消息、提醒、作息发文改投 KOOK。

调用方是 `__init__._send_to`：`get_bot()` 取不到机器人（账号离线 / 未连接），
或单条消息发送时抛异常，就把这一条交给这里。

对端是 `platform/satori_sidecar.py` 的 `/send`（默认 127.0.0.1:8099），
鉴权复用宿主接口那把令牌（RP_HOST_TOKEN = JINYU_TOKEN），不额外开密钥。

边界：
- 文字和图片分开发，图片坏了不拖累文字；
- 图片只认 http(s) 链接和贴纸库的 base64://，最多 4 张；
- 8 秒超时，失败只记日志，绝不把异常抛回调度方。
"""
from __future__ import annotations

import logging
import os
from typing import Iterable, Optional

import httpx

logger = logging.getLogger("jinyv_memory.kook")

MAX_IMAGES = 4
TIMEOUT = 8.0
MAX_IMAGE_CHARS = 8_000_000      # base64 文本上限，约 6MB 原图


def enabled() -> bool:
    return (os.environ.get("RP_KOOK_FALLBACK") or "1").strip() not in {
        "0", "false", "no", "off"}


def _url() -> str:
    return (os.environ.get("RP_KOOK_SEND_URL")
            or "http://127.0.0.1:8099/send").strip()


def _token() -> str:
    return (os.environ.get("RP_KOOK_SEND_TOKEN")
            or os.environ.get("RP_HOST_TOKEN") or "").strip()


def message_parts(msg) -> tuple[str, list[str]]:
    """把 OneBot 消息拆成（可读文字, 图片源）。

    贴纸/图片在 richmsg 里是 `base64://`，这里转成 data URI 才能给 KOOK；
    网络图片直接透传 http(s) 链接。
    """
    text: list[str] = []
    images: list[str] = []
    try:
        segs = list(msg)
    except TypeError:
        return str(msg or "").strip(), []
    for seg in segs:
        kind = getattr(seg, "type", "")
        data = getattr(seg, "data", {}) or {}
        if kind == "text":
            text.append(str(data.get("text") or ""))
        elif kind == "image":
            raw = str(data.get("file") or "")
            link = str(data.get("url") or "")
            if raw.startswith("base64://"):
                if len(raw) <= MAX_IMAGE_CHARS:
                    images.append("data:image/png;base64," + raw[len("base64://"):])
            elif link.startswith(("http://", "https://")):
                images.append(link)
            elif raw.startswith(("http://", "https://")):
                images.append(raw)
    return "".join(text).strip(), images[:MAX_IMAGES]


async def send(text: str, images: Optional[Iterable[str]] = None) -> bool:
    """投递一条到 KOOK；文字发出去就算成功（图片由对端单独发）。"""
    body = str(text or "").strip()
    imgs = [u for u in (images or []) if isinstance(u, str) and u][:MAX_IMAGES]
    if not body and not imgs:
        return False
    if not enabled():
        return False
    token = _token()
    if not token:
        logger.info("[jinyu] KOOK 兜底跳过：没有 RP_HOST_TOKEN / RP_KOOK_SEND_TOKEN")
        return False
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                _url(), json={"text": body, "images": imgs},
                headers={"Authorization": "Bearer " + token})
        data = {}
        try:
            data = resp.json() or {}
        except ValueError:
            pass
        ok = resp.status_code == 200 and bool(data.get("ok", True))
        if ok:
            logger.warning(f"[jinyu] KOOK 兜底已投递：{len(body)}字 图{len(imgs)}张")
        else:
            logger.warning(f"[jinyu] KOOK 兜底失败 {resp.status_code}: {resp.text[:160]}")
        return ok
    except Exception as exc:                       # noqa: BLE001
        logger.warning(f"[jinyu] KOOK 兜底异常：{type(exc).__name__}: {str(exc)[:120]}")
        return False
