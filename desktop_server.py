# -*- coding: utf-8 -*-
"""desktop-v1 最小服务端适配器。

用途：把 `jinyu_memory/core` 的对话能力接到本地桌宠客户端，先跑通协议与气泡链路。
没有配置 LLM key 时自动使用 Mock 模型；配置了 RP_FRONT_KEY / RP_BACK_KEY 后走真实模型。

运行：
  set RP_DESKTOP_TOKEN=dev-token
  python desktop_server.py
  或从密钥文件读（不覆盖已有环境变量）：
  python desktop_server.py --env-file %USERPROFILE%/.rp-pet/desktop.env
客户端连接：
  ws://127.0.0.1:8787/desktop/v1/ws?token=dev-token
"""
import asyncio
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _cli(flag, default):
    try:
        return sys.argv[sys.argv.index(flag) + 1]
    except (ValueError, IndexError):
        return default


CORE_DIR = Path(_cli("--core-dir", os.environ.get("RP_CORE_DIR", str(ROOT / "jinyu_memory" / "core"))))
sys.path.insert(0, str(CORE_DIR))

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Query  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from persona import PERSONA  # noqa: E402
from rp_skeleton import DB, RPContextMiddleware  # noqa: E402


def load_env_file(path):
    """读 KEY=VALUE 形式的密钥文件；已在环境里的变量不覆盖。

    密钥只留在调用方的文件与环境变量里，不写进仓库、不打印。
    """
    if not path:
        return
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key and key not in os.environ:
                    os.environ[key] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        print(f"[desktop] env file not found: {path}")


load_env_file(_cli("--env-file", os.environ.get("RP_DESKTOP_ENV", "")))

HOST = _cli("--host", os.environ.get("RP_DESKTOP_HOST", "127.0.0.1"))
PORT = int(_cli("--port", os.environ.get("RP_DESKTOP_PORT", "8787")))
TOKEN = _cli("--token", os.environ.get("RP_DESKTOP_TOKEN", "dev-token"))
# 服务只监听回环、客户端经 SSH 隧道进来时，可不开令牌（隧道本身即认证）
TRUST_LOCAL = os.environ.get("RP_DESKTOP_TRUST_LOCAL", "") in ("1", "true", "yes")
DB_PATH = os.environ.get("RP_DB", str(ROOT / "desktop_memory.db"))
# 与 QQ 插件共用同一条记忆：默认接主私聊的会话（可用 RP_SESSION 换成别的 qq-u/qq-g）
CID = "jinyu"
BASE_SESSION = os.environ.get("RP_SESSION", "qq-u3521544885").strip() or "qq-u3521544885"
# 设了 RP_HOST_BASE + RP_HOST_TOKEN 就走 QQ 进程里的宿主接口，本进程不再碰 SQLite
HOST_BASE = (os.environ.get("RP_HOST_BASE") or "").rstrip("/")
HOST_TOKEN = (os.environ.get("RP_HOST_TOKEN") or "").strip()
# 跨窗口推送：QQ 插件经本机 POST /desktop/v1/say 让瑾瑜在桌宠气泡里说话
WIN_TOKEN = str(os.environ.get("RP_WIN_TOKEN") or "").strip()


def window_authed(host, supplied):
    """跨窗口端点鉴权：配了 RP_WIN_TOKEN 就比 token，否则只收本机回环。"""
    if WIN_TOKEN:
        return supplied == WIN_TOKEN
    return host in ("127.0.0.1", "::1")


def _brief(text, limit=300):
    """一行日志：折叠换行，避免把气泡结构写进日志。"""
    return " ".join(str(text or "").split())[:limit]


def host_configured() -> bool:
    return bool(HOST_BASE and HOST_TOKEN)


async def host_chat(text, source, hint=""):
    import httpx

    headers = {"Authorization": "Bearer " + HOST_TOKEN}
    payload = {"text": text, "prompt": text, "source": source}
    if hint:
        payload["hint"] = hint
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(HOST_BASE + "/jinyu/chat", json=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"host HTTP {resp.status_code}")
    # 空回复 = 这一轮瑾瑜把话说到别的窗口了（[[pass]]），不是错误
    return str((resp.json() or {}).get("reply") or "").strip()


EPOCH_SECONDS = max(0, int(float(os.environ.get("RP_SESSION_EPOCH_HOURS") or 48) * 3600))
EPOCH_FLUSH = max(1.0, float(os.environ.get("RP_EPOCH_FLUSH_SECONDS") or 8))

# 渠道标注只在适配器这一侧追加：QQ 插件仍然用干净的 PERSONA，
# 因此模型在 QQ 里看不到这些标记，也不会学着写出来。
# 词表与客户端 body.js 的 KIT_MOTIONS / KIT_EXPRESSIONS 保持一致。
MOTIONS = ("stand", "jump", "hop", "look", "turn", "nod", "shake", "spin", "sit", "sleep",
           "dizzy", "walk", "run", "wave", "bow", "shiver", "flap", "dance")
EXPRESSIONS = ("neutral", "happy", "wink", "love", "shy", "surprised", "angry", "sad",
               "sleepy", "thinking", "smug", "pout", "worried", "determined", "flustered",
               "scared", "excited", "cry", "confused")

CHANNEL_NOTE = """
【当前渠道：桌面桌宠】主人正看着电脑桌面上的你，用键盘打字跟你说话，你说的每句话都会以气泡出现在他桌面上。这个渠道里，你可以让自己的身体跟着说话动起来：
- 一条回复最多带一个动作标记 [[act:动作词]] 和一个表情标记 [[mood:表情词]]，放在整条回复的最前面；
- 动作词只能从这里选：{motions}；
- 表情词只能从这里选：{faces}；
- 不要输出 QQ 里的 [[表情:…]] / [[动作:…]] 这类标记，桌面上只认 [[act:…]] 和 [[mood:…]]；
- 标记只给桌面上的你自己的身体看，主人只会看到你的文字。不要念出标记，不要在正文里解释，拿不准就省略。
""".format(motions="、".join(MOTIONS), faces="、".join(EXPRESSIONS))

DESK_PERSONA = {**PERSONA, "core": PERSONA["core"] + CHANNEL_NOTE}

TAG_RE = re.compile(r"\[\[\s*(act|mood)\s*[：:]\s*([a-z_]+)\s*\]\]", re.I)


class MockFront:
    """没有 LLM key 时的本地假模型；用于验证协议和客户端。"""

    name = "瑾瑜"

    def chat(self, messages):
        user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        if re.search(r"饿|饭|米|吃", user):
            return "瑾瑜也饿了。主人要给瑾瑜吃米饭吗？"
        if re.search(r"累|困|加班|忙", user):
            return "那就先歇一会儿吧，瑾瑜在这里。"
        if re.search(r"你好|在吗|嗨|hello", user, re.I):
            return "在呢。瑾瑜一直在听。"
        if re.search(r"喜欢|可爱|漂亮", user):
            return "唔……瑾瑜才没有很高兴。"
        return "嗯，瑾瑜记住了。主人再说说？"


class MockBack:
    """后台归档用的假模型：返回合法事件卡 JSON，避免归档线程报错。"""

    def chat(self, messages):
        body = messages[-1]["content"].split("对话片段：\n")[-1].strip()
        first = body.splitlines()[0][:40] if body else "(empty)"
        hooks = list(dict.fromkeys(re.findall(r"[\u4e00-\u9fa5]{2}", body)))[:4]
        return json.dumps({
            "summary": first,
            "state_changes": {"mood": "平静", "relationship": {"trust": 0.05}},
            "entity_hooks": hooks,
        }, ensure_ascii=False)


def build_middleware():
    if host_configured():
        return None, "remote"
    keys = os.environ.get("RP_FRONT_KEY") and os.environ.get("RP_BACK_KEY")
    if keys:
        from llm import from_env
        front, back, mode = from_env("front"), from_env("back"), "real"
    else:
        front, back, mode = MockFront(), MockBack(), "mock"
    mw = RPContextMiddleware(front, back, DB(DB_PATH),
                             max_tokens=64000, output_reserve=4096, profile="cl100k")
    return mw, mode


MW, MODE = build_middleware()


def session_for_chat():
    """与 QQ 插件同规则：闲置超过阈值就轮换 epoch，返回本轮该写入的 sid。

    continuity 固定为 BASE_SESSION，这样桌宠与 QQ 端共用热窗口、事件卡和长期事实。
    remote 模式下轮换由 QQ 进程负责，这里只回基键。
    """
    if MW is None:
        return BASE_SESSION
    return MW.session_identity(BASE_SESSION, CID, EPOCH_SECONDS, flush_timeout=EPOCH_FLUSH)


APP = FastAPI()
# 在线桌宠连接：ws -> {"device_id", "seq"}；跨窗口 say 直接往这些连接推 pet.say
ACTIVE = {}


def now_ms():
    return int(time.time() * 1000)


def new_id():
    return uuid.uuid4().hex


def envelope(msg_type, payload, device_id=None, seq=0):
    body = {
        "v": 1,
        "id": new_id(),
        "type": msg_type,
        "ts": now_ms(),
        "seq": seq,
        "payload": payload,
    }
    if device_id:
        body["device_id"] = device_id
    return body


def take_tags(reply):
    """取出 [[act:..]] / [[mood:..]]，返回 (干净正文, 动作, 表情)。

    只认词表里的词；其余标记（比如模型写坏的）在正文里一并清掉，免得漏到气泡里。
    """
    actions, moods = [], []
    for kind, word in TAG_RE.findall(reply or ""):
        word = word.lower()
        if kind.lower() == "act" and word in MOTIONS and word not in actions:
            actions.append(word)
        elif kind.lower() == "mood" and word in EXPRESSIONS and word not in moods:
            moods.append(word)
    clean = TAG_RE.sub("", reply or "")
    clean = re.sub(r"\[\[[^\]]*\]\]", "", clean).strip()
    clean = re.sub(r"[ \t]{2,}", " ", clean)
    return clean, actions[:1], moods[:1]


def split_bubbles(reply, mood="neutral"):
    text = re.sub(r"\[\[[^\]]*\]\]", "", reply or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in re.split(r"(?<=[。！？!?])", text) if p.strip()]
    parts = parts[:3]
    if not parts:
        parts = [text]
    return [
        {"text": p, "emotion": mood if i == 0 else "neutral", "delay_ms": 0 if i == 0 else 650, "tts_url": None}
        for i, p in enumerate(parts)
    ]


def actions_for(reply):
    if re.search(r"米饭|吃|饿|喂", reply):
        return ["eat"]
    return []


@APP.get("/desktop/v1/health")
async def health():
    return JSONResponse({"ok": True, "mode": MODE, "protocol": "desktop-v1",
                         "devices": len(ACTIVE)})


@APP.post("/desktop/v1/say")
async def desktop_say(request: Request):
    """跨窗口发话：QQ/通话那边让瑾瑜在桌宠气泡里说一句。"""
    supplied = request.headers.get("authorization", "").replace("Bearer ", "").strip()
    remote = (request.client.host if request.client else "") or ""
    if not window_authed(remote, supplied):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:                     # noqa: BLE001
        return JSONResponse({"ok": False, "error": "请求不是 JSON"}, status_code=400)
    text = str(body.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "缺少 text"}, status_code=400)
    clean, actions, moods = take_tags(text)
    bubbles = split_bubbles(clean, moods[0] if moods else "neutral")
    if not bubbles:
        return JSONResponse({"ok": False, "error": "没有可说的正文"}, status_code=400)
    sent = 0
    for ws, state in list(ACTIVE.items()):
        try:
            state["seq"] += 1
            await ws.send_json(envelope("pet.say", {
                "bubbles": bubbles,
                "actions": actions or actions_for(clean),
                "priority": 1,
                "reply_to": "",
                "ttl_ms": 300000,
            }, device_id=state["device_id"], seq=state["seq"]))
            sent += 1
        except Exception:                 # noqa: BLE001 连接断了就清掉，不拖累其他设备
            ACTIVE.pop(ws, None)
    print(f"[desktop] 瑾瑜（来自{str(body.get('source') or 'qq')}）：{_brief(clean)}",
          flush=True)
    if not sent:
        return JSONResponse({"ok": False, "error": "现在没有在线的桌宠窗口"},
                            status_code=503)
    return JSONResponse({"ok": True, "sent": sent})


@APP.websocket("/desktop/v1/ws")
async def desktop_ws(ws: WebSocket, token: str = Query(default="")):
    supplied = token or ws.headers.get("authorization", "").replace("Bearer ", "").strip()
    remote = (ws.client.host if ws.client else "") or ""
    if not (TRUST_LOCAL and remote in ("127.0.0.1", "::1")) and supplied != TOKEN:
        await ws.close(code=4001)
        return

    await ws.accept()
    device_id = "dev_" + new_id()[:12]
    session_id = BASE_SESSION
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
        hello = json.loads(raw)
        if hello.get("type") != "hello":
            await ws.close(code=4006)
            return
        payload = hello.get("payload") or {}
        device_id = str(payload.get("device_id") or device_id)
        session_id = await asyncio.to_thread(session_for_chat)
        state = {"device_id": device_id, "seq": 0}
        ACTIVE[ws] = state
        print(f"[desktop] hello device={device_id} base={BASE_SESSION} session={session_id} mode={MODE}")

        state["seq"] += 1
        await ws.send_json(envelope("hello.ack", {
            "session": session_id,
            "primary": True,
            "server_time": now_ms(),
            "heartbeat_s": 30,
            "protocol": "desktop-v1",
            "limits": {"downlink_per_min": 60, "input_per_sec": 1, "proactive_per_hour": 6},
            "figures": ["whale"],
        }, device_id=device_id, seq=state["seq"]))

        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")
            if mtype in ("pong", "ack"):
                continue
            if mtype == "event.text":
                text = str((msg.get("payload") or {}).get("text") or "").strip()
                if not text:
                    continue
                print(f"[desktop] 主人：{_brief(text, 200)}", flush=True)
                session_id = await asyncio.to_thread(session_for_chat)
                tagged = {}

                def keep_clean(raw, _tag=tagged):
                    """落库前清洗：标记只留在本次回包里，QQ 看到的历史是纯文字。"""
                    clean, acts, moods = take_tags(raw)
                    _tag["text"] = clean or "……"
                    _tag["actions"] = acts
                    _tag["emotion"] = moods[0] if moods else "neutral"
                    return _tag["text"]

                if host_configured():
                    try:
                        raw = await host_chat(text, "desk", CHANNEL_NOTE)
                    except Exception as exc:          # noqa: BLE001  宿主抖动不打断连接
                        print(f"[desktop] host error: {type(exc).__name__}: {exc}")
                        continue
                    clean, acts, moods = take_tags(raw)
                    if not clean:                 # [[pass]]：这一轮在别的窗口说话
                        print("[desktop] 瑾瑜：这一轮在别的窗口说话", flush=True)
                        continue
                    tagged.update({"text": clean, "actions": acts,
                                   "emotion": moods[0] if moods else "neutral"})
                    reply = clean
                else:
                    reply = await asyncio.to_thread(
                        MW.chat, text, DESK_PERSONA, session_id, CID, reply_filter=keep_clean)
                clean = tagged.get("text") or reply
                bubbles = split_bubbles(clean, tagged.get("emotion") or "neutral")
                if not bubbles:
                    continue
                actions = tagged.get("actions") or actions_for(clean)
                print(f"[desktop] 瑾瑜：{_brief(clean)}", flush=True)
                print(f"[desktop] say actions={actions} emotion={tagged.get('emotion')} bubbles={len(bubbles)}")
                state["seq"] += 1
                await ws.send_json(envelope("pet.say", {
                    "bubbles": bubbles,
                    "actions": actions,
                    "priority": 1,
                    "reply_to": msg.get("id"),
                    "ttl_ms": 300000,
                }, device_id=device_id, seq=state["seq"]))
                continue
            if mtype == "event.interaction":
                # v1 只记录；互动回应留待后续
                continue
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"[desktop] error device={device_id}: {type(exc).__name__}: {exc}")
    finally:
        ACTIVE.pop(ws, None)
        # 共享会话属于 QQ 私聊本体，客户端断开时不能把它标成 closed
        print(f"[desktop] disconnected device={device_id}")


@APP.on_event("shutdown")
def shutdown():
    if MW is None:
        return
    try:
        MW.shutdown()
    except Exception:
        pass


if __name__ == "__main__":
    import uvicorn
    print(f"[desktop] desktop-v1 adapter mode={MODE} url=ws://{HOST}:{PORT}/desktop/v1/ws")
    uvicorn.run(APP, host=HOST, port=PORT, log_level="info")
