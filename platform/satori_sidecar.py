#!/usr/bin/env python3
"""Satori sidecar：把 KOOK（经 Koishi/Satori）的消息转给宿主接口。

一条消息只走三步：
    Satori message-created -> POST /jinyu/chat -> Satori message.create

本进程不碰数据库、不做记忆、不做提示词，重启不丢数据。
缺凭据时启动即退出，并打印缺哪一项；加 --check 只校验配置后退出。

运行：
    python platform/satori_sidecar.py --check
    python platform/satori_sidecar.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from typing import Any

import capabilities as caps

DEFAULT_SATORI_HOST = "127.0.0.1"
DEFAULT_SATORI_PORT = "5140"
# legacy 契约，勿改：JINYU_HOST / JINYU_TOKEN / JINYU_SOURCE 是对外环境变量名
DEFAULT_JINYU_HOST = "http://127.0.0.1:8080"
DEFAULT_SOURCE = "qq"  # 宿主接口只认 qq/desk/video，KOOK 先借用 qq 标签

REQUIRED = ("JINYU_TOKEN",)

MAX_IMAGES = 4                 # 一条消息最多带几张图；宿主侧同样按这个上限截断
IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)
IMG_SRC = re.compile(r"""src\s*=\s*["']([^"']+)["']""", re.I)
EVENT_STAMP = os.environ.get("KOOK_EVENT_STAMP") or "/tmp/sidecar_last_event"


def _mark_event() -> None:
    """记下最近一条入站消息的时间，watchdog 靠它判断网关是不是「假在线」。"""
    try:
        with open(EVENT_STAMP, "w", encoding="utf-8") as fh:
            fh.write(str(int(time.time())))
    except OSError:
        pass


def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name) or default).strip()


def _mask(value: str, keep: int = 4) -> str:
    """日志里只留前几位，避免把凭据写进 journal。"""
    if not value:
        return "(空)"
    return value[:keep] + "*" * max(0, len(value) - keep)


def _log(msg: str) -> None:
    print(time.strftime("[%H:%M:%S]"), "[kook-sidecar]", msg, flush=True)


def _collect_images(node: Any, out: list[str], depth: int = 0) -> None:
    """深度优先找 <img> 元素，取 src；只认 http(s)，不做平台特判。"""
    if depth > 6 or len(out) >= MAX_IMAGES:
        return
    tag = str(getattr(node, "tag", "") or "").lower()
    src = getattr(node, "src", None)
    if tag == "img" and isinstance(src, str):
        if src.startswith(("http://", "https://")):
            out.append(src)
        return
    for child in getattr(node, "_children", None) or []:
        _collect_images(child, out, depth + 1)


def extract_images(message: Any) -> list[str]:
    """从 satori 消息里取图片地址。

    优先用 MessageObject.message（元素表）；取不到时退回扫 content 里的
    <img src="..."/> 标记——KOOK 侧的 content 就是这种字符串。
    """
    urls: list[str] = []
    try:
        elements = getattr(message, "message", None)
    except Exception:                              # noqa: BLE001 解析失败就退回正则
        elements = None
    if isinstance(elements, list):
        for element in elements:
            _collect_images(element, urls)
    if not urls:
        content = getattr(message, "content", None)
        if isinstance(content, str) and "<img" in content.lower():
            for tag in IMG_TAG.finditer(content):
                found = IMG_SRC.search(tag.group(0))
                if found and found.group(1).startswith(("http://", "https://")):
                    urls.append(found.group(1))
    return urls[:MAX_IMAGES]


def load_config() -> dict:
    return {
        "satori_host": _env("SATORI_HOST", DEFAULT_SATORI_HOST),
        "satori_port": int(_env("SATORI_PORT", DEFAULT_SATORI_PORT)),
        "satori_token": _env("SATORI_TOKEN"),
        "satori_platform": _env("SATORI_PLATFORM", "kook"),
        "jinyu_host": _env("JINYU_HOST", DEFAULT_JINYU_HOST).rstrip("/"),
        "jinyu_token": _env("JINYU_TOKEN"),
        "jinyu_source": _env("JINYU_SOURCE", DEFAULT_SOURCE),
        "allow_users": [x.strip() for x in _env("KOOK_ALLOW_USERS").split(",") if x.strip()],
        # QQ 掉线时宿主会 POST /send 到这里，再由本进程发到 KOOK 的固定频道
        "kook_channel": _env("KOOK_FALLBACK_CHANNEL").strip(),
        "kook_token": _env("KOOK_TOKEN").strip(),
        "send_port": int(_env("KOOK_SEND_PORT", "8099")),
    }


def check_config(cfg: dict) -> list[str]:
    """返回缺失/非法的配置项说明；空列表表示可以启动。"""
    problems: list[str] = []
    if not cfg["jinyu_token"]:
        problems.append("JINYU_TOKEN 未设置：宿主接口 /jinyu/chat 的 Bearer 令牌（复用 RP_HOST_TOKEN）")
    if not (1 <= cfg["satori_port"] <= 65535):
        problems.append(f"SATORI_PORT 非法：{cfg['satori_port']}")
    if not cfg["jinyu_host"].startswith(("http://", "https://")):
        problems.append(f"JINYU_HOST 必须以 http:// 或 https:// 开头：{cfg['jinyu_host']}")
    if cfg["jinyu_source"] not in ("qq", "desk", "video"):
        problems.append(f"JINYU_SOURCE 只能是 qq/desk/video：{cfg['jinyu_source']}")
    return problems


def print_status(cfg: dict, problems: list[str]) -> None:
    _log("配置清单：")
    _log(f"  Satori   ws://{cfg['satori_host']}:{cfg['satori_port']}/v1/events"
         f"  token={_mask(cfg['satori_token'])}")
    _log(f"  宿主接口  {cfg['jinyu_host']}/jinyu/chat  token={_mask(cfg['jinyu_token'])}"
         f"  source={cfg['jinyu_source']}")
    _log(f"  KOOK 平台 {cfg['satori_platform']}"
         f"  允许用户={cfg['allow_users'] or '不限'}")
    if problems:
        _log("缺少以下配置，进程不会启动：")
        for p in problems:
            _log(f"  - {p}")
        _log("参考 platform/config.example.env；KOOK Bot Token 配在 Koishi 的 adapter-kook 里。")


async def call_host(cfg: dict, text: str, images: list[str] | None = None) -> str:
    import httpx

    payload = {"text": text, "prompt": text, "source": cfg["jinyu_source"]}
    if images:
        payload["images"] = images
    headers = {"Authorization": "Bearer " + cfg["jinyu_token"]}
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(cfg["jinyu_host"] + "/jinyu/chat", json=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"host HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json() or {}
    reply = str(data.get("reply") or "").strip()
    if not reply:
        _log(f"宿主返回空 reply，原始响应：{str(data)[:300]}")
    return reply


async def preflight_host(cfg: dict) -> None:
    """宿主接口连通性只告警不阻断：主进程可能比侧车晚起。"""
    import httpx

    url = cfg["jinyu_host"] + "/jinyu/health"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
        _log(f"宿主接口预检 {url} -> {resp.status_code}")
    except Exception as exc:  # noqa: BLE001
        _log(f"宿主接口暂不可达（{type(exc).__name__}: {exc}），收到消息时会重试")


async def _upload_kook_image(cfg: dict, data_uri: str) -> str:
    """把 data:image/... 传到 KOOK 图床，返回可直接放进 <img src> 的地址。

    KOOK 不认 data URI（会渲染成空白框），所以贴纸要先换成一个真实 URL。
    """
    import base64 as _b64

    import httpx

    if not cfg["kook_token"]:
        return ""
    head, _, payload = data_uri.partition(",")
    try:
        raw = _b64.b64decode(payload)
    except Exception:                    # noqa: BLE001
        return ""
    if not raw or len(raw) > 8 * 1024 * 1024:
        return ""
    mime = "image/png"
    if head.startswith("data:") and ";" in head:
        mime = head[5:head.index(";")] or mime
    ext = {"image/jpeg": "jpg", "image/gif": "gif",
           "image/webp": "webp"}.get(mime, "png")
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://www.kookapp.cn/api/v3/asset/create",
                headers={"Authorization": "Bot " + cfg["kook_token"]},
                files={"file": (f"kook-fallback.{ext}", raw, mime)})
        data = resp.json() or {}
    except Exception as exc:             # noqa: BLE001
        _log(f"KOOK 图床请求失败：{type(exc).__name__}: {str(exc)[:100]}")
        return ""
    if resp.status_code == 200 and data.get("code") == 0:
        return str((data.get("data") or {}).get("url") or "")
    _log(f"KOOK 图床上传失败 {resp.status_code}: {str(data)[:120]}")
    return ""


async def start_sender(cfg: dict, app):
    """宿主主动消息入口：QQ 掉线时 POST /send，转成 KOOK 消息发出去。

    文字和图片分两条发：图片坏了不影响文字送达；
    频道固定用 KOOK_FALLBACK_CHANNEL，避免主动消息散落到别的会话。
    """
    try:
        from aiohttp import web
    except ImportError:
        _log("缺少 aiohttp，KOOK 兜底发送口未启动")
        return None

    def _bad(msg: str, code: int = 400):
        return web.json_response({"ok": False, "error": msg}, status=code)

    async def handle(request):
        if request.headers.get("Authorization", "") != "Bearer " + cfg["jinyu_token"]:
            return _bad("unauthorized", 401)
        if not cfg["kook_channel"]:
            return _bad("KOOK_FALLBACK_CHANNEL 未配置", 503)
        accounts = list(app.accounts.values())
        if not accounts:
            return _bad("KOOK 账号还没连接", 503)
        try:
            body = await request.json()
        except Exception:                    # noqa: BLE001
            return _bad("bad json")
        text = str(body.get("text") or "")[:4000]
        images = [u for u in (body.get("images") or []) if isinstance(u, str)][:4]
        account = accounts[0]
        sent = False
        if text:
            try:
                await account.message_create(cfg["kook_channel"], text)
                sent = True
            except Exception as exc:         # noqa: BLE001
                _log(f"兜底文字发送失败：{type(exc).__name__}: {exc}")
        for src in images:                   # 逐张发，坏一张不拖累已发内容
            if src.startswith("data:"):
                src = await _upload_kook_image(cfg, src)
                if not src:
                    continue
            try:
                await account.message_create(
                    cfg["kook_channel"], f'<img src="{src}"/>')
                sent = True
            except Exception as exc:         # noqa: BLE001
                _log(f"兜底图片发送失败：{type(exc).__name__}: {str(exc)[:100]}")
        _log(f"兜底投递 {'成功' if sent else '失败'}：{len(text)}字 图{len(images)}张")
        return web.json_response({"ok": sent})

    application = web.Application()
    application.router.add_post("/send", handle)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", cfg["send_port"])
    await site.start()
    _log(f"兜底发送口已就绪 http://127.0.0.1:{cfg['send_port']}/send"
         f"  频道={cfg['kook_channel'] or '未配置'}")
    return runner


async def run(cfg: dict) -> int:
    try:
        from satori import EventType
        from satori.client import App, WebsocketsInfo
    except ImportError:
        _log("缺少依赖 satori-python：pip install satori-python httpx")
        return 3

    app = App(
        WebsocketsInfo(
            host=cfg["satori_host"],
            port=cfg["satori_port"],
            token=cfg["satori_token"] or None,
        )
    )
    lock = asyncio.Lock()

    @app.register
    async def on_event(account, event):
        if event.type != EventType.MESSAGE_CREATED:
            return
        user_id = getattr(getattr(event, "user", None), "id", "") or ""
        if user_id and user_id == account.self_id:
            return  # 自己发的消息不回声
        _mark_event()
        if cfg["allow_users"] and user_id not in cfg["allow_users"]:
            _log(f"忽略非白名单用户 {user_id}")
            return
        text = caps.to_text(event.message, account.platform)
        images = extract_images(event.message)
        if images:  # 图片标记不该出现在给模型的正文里
            text = " ".join(IMG_TAG.sub(" ", text).split())
        if not text and not images:
            _log("消息里没有可读文本，跳过")
            return
        _log(f"收到 {user_id}: {text[:60] or '(无文字)'} 图{len(images)}张")
        async with lock:  # 单用户场景串行，保证记忆轮次顺序
            try:
                reply = await call_host(cfg, text, images)
            except Exception as exc:  # noqa: BLE001
                _log(f"调宿主失败：{type(exc).__name__}: {exc}")
                return
            if not reply:
                _log("宿主返回空回复（可能说到别的窗口了），不发")
                return
            for chunk in caps.clamp_outbound(reply, account.platform):
                try:
                    await account.send(event, chunk)
                except Exception as exc:  # noqa: BLE001
                    _log(f"发送失败：{type(exc).__name__}: {exc}")
                    return
        _log(f"已回复 {len(reply)} 字")

    await preflight_host(cfg)
    _log("已连接到 Satori，等待消息")
    _mark_event()                    # 刚起进程算「有活动」，别被 watchdog 立刻重连
    try:
        sender = await start_sender(cfg, app)
    except Exception as exc:             # noqa: BLE001 兜底口起不来不该拦住收消息
        _log(f"兜底发送口启动失败：{type(exc).__name__}: {exc}")
        sender = None
    try:
        await app.run_async()
    finally:
        if sender is not None:
            await sender.cleanup()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Satori -> /jinyu/chat 侧车")
    parser.add_argument("--check", action="store_true", help="只校验配置，不连接")
    args = parser.parse_args()

    cfg = load_config()
    problems = check_config(cfg)
    print_status(cfg, problems)
    if problems:
        return 2
    if args.check:
        _log("配置校验通过")
        return 0
    try:
        return asyncio.run(run(cfg))
    except KeyboardInterrupt:
        _log("收到中断，退出")
        return 0


if __name__ == "__main__":
    sys.exit(main())
