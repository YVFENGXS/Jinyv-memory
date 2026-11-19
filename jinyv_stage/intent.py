"""前台表达意图 → 后台解码成身体工具并执行。

分工（出错时按层归因，日志里写 layer=）：
- front：只负责说话与说「想做什么动作」，用一句自然语言调 `body_intent`，
  不选工具、不拼参数。
- back：后台模型拿到当前身体快照（get_state / list_targets / list_abilities）
  与工具表，决定工具与参数；它不负责说话。
- page / bridge：真正执行仍走 `jinyv_stage.tools.call` → 同一条 WebSocket 桥，
  不新增协议、不复制页面校验规则；页面业务码与桥层失败原样归因。

失败一律回填具体原因（目标不可达 / 参数非法 / 超时 / 房间没连上），
返回 `ok:false` + `hint`（含「这一步没有发生」），不允许当成功。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Any

from . import tools as stage_tools
from .bridge import StageBridge, StageBridgeError

LOG = logging.getLogger("jinyv_stage.intent")

TOOL_NAME = "body_intent"
SNAPSHOT_TIMEOUT = max(1.0, float(os.environ.get("RP_BODY_SNAPSHOT_TIMEOUT") or 4))
INTENT_BUDGET = max(5.0, float(os.environ.get("RP_BODY_BUDGET") or 45))
INTENT_MAX_STEPS = max(1, int(float(os.environ.get("RP_BODY_MAX_STEPS") or 3)))
INTENT_MAX_CALLS = max(1, int(float(os.environ.get("RP_BODY_MAX_CALLS") or 6)))

# 说话是前台的事：后台解码器不给 start_speaking / stop_speaking，避免两层都管嘴。
_SPEAKING_TOOLS = frozenset({"start_speaking", "stop_speaking"})

# 一次只让一段身体动作在跑：桥和房间都按顺序执行，并发只会让回执互相打断。
_BUSY = threading.Lock()

_REASONS = {
    "stage_unavailable": "房间页面没有连上，这具身体现在动不了",
    "stage_budget": "这一轮的动作名额已经用完",
    "stage_deadline": "这一轮时间不够，动作没来得及执行",
    "body_busy": "上一段身体动作还没结束",
    "call_in_progress": "主人正在和你通话，这次没有动身体",
    "client_timeout": "身体没有在规定时间内回执，动作可能没有做完",
    "invalid_argument": "动作参数不合法，房间拒绝了这次动作",
    "unknown_tool": "后台选了一个身体不认识的工具",
    "blocked": "去路或目标被挡住了",
    "too_far": "目标太远，走不过去",
    "target_not_found": "房间里找不到这个目标",
    "not_seated": "当前姿态不允许这个动作",
    "no_action": "后台没能把这个意图解析成可执行动作",
    "back_unavailable": "后台决策模型没配好或调用失败",
    "stage_error": "身体动作链路异常",
    "internal": "房间返回了未说明的失败",
}

_SYSTEM = (
    "你是瑾瑜的「身体动作解码器」，只做一件事：把前台用自然语言说出的意图，"
    "翻译成可执行的身体工具调用。你不是在聊天。\n"
    "纪律：\n"
    "1. 只输出工具调用，不回应情绪、不复述意图、不写对话内容。\n"
    "2. 目标 id、座位、灯、门、动作名必须来自用户消息里的当前身体快照；"
    "快照里没有的东西不许臆造。\n"
    "3. 参数名用工具 schema 里的写法（如 seat_id、objectId）；不知道就少给可选参数，别乱填。\n"
    "4. 能一步完成的用单工具；需要连续动作（走过去再坐下、坐下再挥手）优先用 perform "
    "一次编排，steps 按时间顺序排。\n"
    "5. 只做意图里要求的事，不额外加动作；不说的话、眨眼、呼吸这类填充动作不要自己加。\n"
    "6. 如果现有工具做不到（目标不在房间、能力不支持、意图本身不是身体动作），"
    "不要调用任何工具，直接用一句中文说明具体原因，例如「房间里没有浴缸」。\n"
)


def tool_schema() -> dict[str, Any]:
    """前台唯一的身体工具：只说意图，不选工具、不拼参数。"""
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "用自然语言说出你想做的身体动作：走过去、坐下/躺下、站起来、看向某处、"
                "做动作或手势、换表情、开灯、开关门。只要说清楚目的和顺序"
                "（例如「去窗边坐下，然后朝主人挥挥手」），不用选工具、不用拼参数；"
                "后台会结合房间当前状态与可用目标自己决定并真的执行。"
                "回执 ok=false 表示动作没有做成，里面写了具体原因（目标不可达 / 参数不合法 / 超时等），"
                "这时不要说你已经做到，只能照实说没做成或打算换个方式。"
                "普通聊天不需要身体动作时不要调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": "想做的一件事或一串动作，用自然语言写清楚，可带地点/对象",
                    }
                },
                "required": ["intent"],
            },
        },
    }


def acquire() -> bool:
    """拿身体锁；拿不到说明上一段动作还在跑。"""
    return _BUSY.acquire(blocking=False)


def release() -> None:
    try:
        _BUSY.release()
    except RuntimeError:
        pass


def explain(code: str, detail: Any = None) -> str:
    """把页面/桥的失败码翻成人话；页面给了 message 就附在后面。"""
    code = str(code or "internal")
    if _is_bridge_duplicate(detail):
        return "身体桥把这条指令当成重复请求丢掉了（同一个请求 id 还在等回执）"
    text = _REASONS.get(code) or f"房间返回了未说明的失败（{code}）"
    message = ""
    if isinstance(detail, dict):
        message = str(detail.get("message") or detail.get("reason") or "").strip()
        step = detail.get("failedStep") if isinstance(detail.get("failedStep"), dict) else {}
        if step.get("kind"):
            message = f"{message}（卡在 {step['kind']} 这一步）" if message \
                else f"卡在 {step['kind']} 这一步"
    if message and message not in text:
        text = f"{text}：{message}"
    return text


def _is_bridge_duplicate(detail: Any) -> bool:
    """桥端 pending 按 id 全局去重，撞车时会回 invalid_argument；那不是参数错误。"""
    if not isinstance(detail, dict):
        return False
    text = f"{detail.get('message') or ''}{detail.get('reason') or ''}"
    return "请求 id 重复" in text or "重复请求" in text


def layer_of(code: str, detail: Any = None) -> str:
    """这个失败码该记在哪一层：back（决策）/ bridge（连接）/ host（预算）/ page（执行）。"""
    code = str(code or "")
    if code == "invalid_argument" and _is_bridge_duplicate(detail):
        return "bridge"
    if code in {"no_action", "back_unavailable", "unknown_tool", "invalid_argument"}:
        return "back"
    if code in {"stage_unavailable", "client_timeout", "stage_error"}:
        return "bridge"
    if code in {"stage_budget", "stage_deadline", "body_busy", "call_in_progress"}:
        return "host"
    return "page"


def failure(code: str, layer: str, message: str, intent: str,
            did: list | None = None, origin: str = "user") -> dict[str, Any]:
    """失败回执：原因可读、层次明确、带「这一步没有发生」的硬约束。"""
    did = list(did or [])
    done = [f"{row.get('tool')}" for row in did if row.get("ok")]
    done_note = f"已经发生的是：{'、'.join(done)}；" if done else ""
    return {
        "ok": False,
        "intent": intent,
        "code": code,
        "layer": layer,
        "origin": origin,
        "message": message,
        "did": did,
        "hint": (
            f"body_intent 没有做成（{code}：{message}）。{done_note}"
            "没列进去的动作这一步没有发生，不要说你已经做了或已经在那儿；"
            "照实说没做到、或打算换个方式；需要重试就再调一次。"
            "不要向主人报告工具错误。"
        ),
    }


def success(intent: str, did: list, origin: str = "user") -> dict[str, Any]:
    names = "、".join(str(row.get("tool")) for row in did if row.get("ok"))
    return {
        "ok": True,
        "intent": intent,
        "did": did,
        "origin": origin,
        "message": f"已完成：{names}",
        "hint": ("动作已完成。用一两句话回应主人，不要只描述动作，"
                 "也不要把已经做完的说成还没做。"),
    }


def _left(deadline: float) -> float:
    return float(deadline) - time.monotonic()


def _parse_call(call: Any) -> tuple[str, dict, str]:
    fn = (call or {}).get("function") if isinstance(call, dict) else {}
    fn = fn if isinstance(fn, dict) else {}
    name = str(fn.get("name") or "").strip()
    raw = fn.get("arguments")
    if isinstance(raw, dict):
        args = raw
    else:
        try:
            args = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return name, {}, "后台给的参数不是合法 JSON"
    if not isinstance(args, dict):
        return name, {}, "后台给的参数不是对象"
    if not name:
        return "", {}, "后台没有给出工具名"
    return name, args, ""


def _backend_tools() -> list[dict[str, Any]]:
    return [item for item in stage_tools.openai_tools()
            if item["function"]["name"] not in _SPEAKING_TOOLS]


def _compact(value: Any, limit: int = 500) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _intent_message(intent: str, snap: dict[str, Any]) -> str:
    return (
        f"前台想做的动作（自然语言）：{intent}\n\n"
        f"当前身体快照：\n"
        f"- 状态：{_compact(snap.get('get_state'), 700)}\n"
        f"- 可走目标：{_compact(snap.get('list_targets'), 900)}\n"
        f"- 可用能力：{_compact(snap.get('list_abilities'), 900)}\n\n"
        "请给出工具调用；做不到就不要调用工具，直接说明原因。"
    )


async def _snapshot(bridge: StageBridge, deadline: float) -> dict[str, Any]:
    """执行前先读一次身体状态与可用目标；读不到就不让后台瞎猜。"""
    out: dict[str, Any] = {}
    for name in ("get_state", "list_targets", "list_abilities"):
        left = _left(deadline)
        if left <= 0.5:
            return {"ok": False, "code": "stage_deadline",
                    "message": explain("stage_deadline")}
        timeout = max(0.5, min(SNAPSHOT_TIMEOUT, left))
        try:
            result = await asyncio.wait_for(
                stage_tools.call(bridge, name, {}), timeout=timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "code": "client_timeout",
                    "message": f"读取 {name} 超时，身体没有回执"}
        if not result.get("ok"):
            code = str(result.get("code") or "internal")
            return {"ok": False, "code": code,
                    "message": f"读取 {name} 失败（{code}）"}
        out[name] = stage_tools.summarize(name, result)
    return {"ok": True, **out}


async def act(bridge: StageBridge, llm: Any, intent: str, *,
              max_steps: int = INTENT_MAX_STEPS,
              max_calls: int = INTENT_MAX_CALLS,
              deadline: float | None = None,
              origin: str = "user") -> dict[str, Any]:
    """在当前桥上完成一次意图：快照 → 后台选工具 → 逐条执行 → 汇总。"""
    started = time.monotonic()
    deadline = float(deadline or (started + INTENT_BUDGET))
    intent = " ".join(str(intent or "").split())
    if not intent:
        return failure("invalid_argument", "front", "意图是空的，没有可执行的动作", intent,
                       origin=origin)

    snap = await _snapshot(bridge, deadline)
    if not snap.get("ok"):
        code = str(snap.get("code") or "stage_unavailable")
        return failure(code, layer_of(code), str(snap.get("message") or explain(code)),
                       intent, origin=origin)
    LOG.info("[body] 快照完成 targets=%s seats=%s primitives=%s",
             snap.get("list_targets", {}).get("count"),
             len(snap.get("list_abilities", {}).get("seats") or []),
             len(snap.get("list_abilities", {}).get("primitives") or []))

    tools = _backend_tools()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _intent_message(intent, snap)},
    ]
    did: list[dict[str, Any]] = []
    used = 0
    said = ""
    for rnd in range(1, max_steps + 1):
        if _left(deadline) <= 0.5:
            return failure("stage_deadline", "host", explain("stage_deadline"), intent, did,
                           origin=origin)
        try:
            content, calls = await asyncio.to_thread(
                llm.chat_tools, messages, tools, 0.2, 700)
        except Exception as exc:                 # noqa: BLE001 后台模型失败如实回填
            LOG.warning("[body] 后台解码失败 round=%d %s: %s",
                        rnd, type(exc).__name__, exc)
            return failure("back_unavailable", "back",
                           f"{explain('back_unavailable')}（{type(exc).__name__}）",
                           intent, did, origin=origin)
        said = str(content or "").strip()
        if not calls:
            break
        LOG.info("[body] 后台决策 round=%d %s", rnd, _compact(
            [{"tool": (c.get("function") or {}).get("name"),
              "args": (c.get("function") or {}).get("arguments")} for c in calls], 600))
        messages.append({"role": "assistant", "content": content or "",
                         "tool_calls": calls})
        room = max(0, max_calls - used)
        if room <= 0:
            break
        failed = None
        for call in calls[:room]:
            name, args, problem = _parse_call(call)
            if problem:
                failed = ("invalid_argument", "back", problem, "")
                break
            result = await stage_tools.call(bridge, name, args)
            used += 1
            ok = bool(result.get("ok"))
            code = str(result.get("code") or ("ok" if ok else "internal"))
            readonly = name in stage_tools.READONLY
            if not readonly:                 # 只读查询不算「做过」，只给后台补资料
                did.append({"tool": name, "args": args, "ok": ok, "code": code})
            LOG.info("[body] 执行 tool=%s code=%s layer=%s origin=%s args=%s",
                     name, code, "-" if ok else layer_of(code, result.get("detail")),
                     origin, _compact(args, 200))
            messages.append({
                "role": "tool",
                "tool_call_id": str(call.get("id") or ""),
                "name": name,
                "content": _compact(stage_tools.summarize(name, result)),
            })
            if not ok and readonly:
                continue                     # 查资料失败就把错误交给后台重判
            if not ok:
                failed = (code, layer_of(code, result.get("detail")),
                          explain(code, result.get("detail")), name)
                break
            skipped = stage_tools.success_hint(name, result)
            if skipped:                      # perform 里被跳过的基元：同样算没发生
                reason = str((stage_tools.summarize(name, result).get("skipped") or [{}])[0]
                             .get("reason") or "skipped")
                failed = (reason, "page", skipped, name)
                break
        if failed:
            code, layer, message, _tool = failed
            return failure(code, layer, message, intent, did, origin=origin)
        if used >= max_calls:
            break

    if not did:
        return failure("no_action", "back",
                       said or str(explain("no_action")), intent, origin=origin)
    LOG.info("[body] 完成 steps=%d 耗时=%dms origin=%s", len(did),
             int((time.monotonic() - started) * 1000), origin)
    return success(intent, did, origin)


async def standalone(intent: str, llm: Any, *, url: str, token: str | None = None,
                     timeout: float = 15.0, deadline: float | None = None,
                     origin: str = "user", **kwargs: Any) -> dict[str, Any]:
    """没有现成桥时（QQ / 桌宠回合）临时连一次，做完就关。"""
    bridge = StageBridge(url, token=token or None, timeout=timeout)
    connect_timeout = min(8.0, max(2.0, timeout))
    try:
        await asyncio.wait_for(bridge.connect(), timeout=connect_timeout + 2)
        if not bridge.room_connected:
            return failure("stage_unavailable", "bridge",
                           "房间页面没有连上，这具身体现在动不了", intent, origin=origin)
        return await act(bridge, llm, intent, deadline=deadline, origin=origin, **kwargs)
    except asyncio.TimeoutError:
        return failure("client_timeout", "bridge", explain("client_timeout"), intent,
                       origin=origin)
    except (StageBridgeError, OSError) as exc:
        LOG.warning("[body] 桥连接失败: %s", exc)
        return failure("stage_unavailable", "bridge",
                       f"{explain('stage_unavailable')}（{type(exc).__name__}）", intent,
                       origin=origin)
    except Exception as exc:                     # noqa: BLE001 链路异常也要有可读原因
        LOG.warning("[body] 意图链路异常 %s: %s", type(exc).__name__, exc)
        return failure("stage_error", "bridge",
                       f"{explain('stage_error')}（{type(exc).__name__}）", intent,
                       origin=origin)
    finally:
        try:
            await bridge.close()
        except Exception:                        # noqa: BLE001
            pass
