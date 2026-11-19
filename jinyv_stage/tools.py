"""服务器 AI 能看到的「身体」工具子集。

暴露身体本身（移动 / 视线 / 姿态 / 动作 / 表情 / 手势基元 / 分层权重 / 取消）、
能力枚举（listAbilities）、一次编排多步的 `perform`，以及灯与门这类会被模型
主动使用的房间状态。昼夜与屏幕仍不在工具面里：它们属于玩家存档面，没有模型场景。
玩家角色不在工具面里，床的双人例外仍走 `sit`。

基元（arm_raise 等）没有独立的桥方法：白名单只有 `perform`，所以 `play_gesture`
在适配层组装成 `perform` 的单步 `{kind:"primitive"}`，不另造方法名。

用法：
    from jinyv_stage.tools import openai_tools, call
    tools = openai_tools()                       # 直接塞进 function-calling 的 tools
    result = await call(bridge, "move_to", {"target": "seat.her.window"})

页面侧仍是唯一裁判：参数不合法时原样返回 `{ok:false, code:"invalid_argument", detail:{...}}`，
适配层不复制校验规则，避免两套口径。
"""

from __future__ import annotations

import asyncio
from typing import Any

from .bridge import StageBridge

# name -> (页面方法, 参数改名表, 必填参数)
_SPECS: list[dict[str, Any]] = [
    {
        "name": "get_state",
        "method": "getState",
        "rename": {},
        "description": "读房间当前状态（时间、灯、门、座位、角色是否在忙）。无副作用。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_targets",
        "method": "listTargets",
        "rename": {},
        "description": "列出可走到的目标 id（物体 / 坐点 / 门 / 角色）。无副作用。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_abilities",
        "method": "listAbilities",
        "rename": {},
        "description": "列出身体能力：可用动作名、手势基元、座位与支持的姿态、灯、门、表情。无副作用。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_clip_state",
        "method": "getClipState",
        "rename": {},
        "description": "读当前动作 clip 名、相位与所有可播名字。无副作用。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "move_to",
        "method": "moveTo",
        "rename": {},
        "description": "目标式寻路走到指定 id 或坐标。速度固定，不需要拆步；坐着时先 stand。",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "description": "目标 id（如 seat.her.window）或坐标 {x,z} 世界毫米 / [x,y] 平面毫米",
                    "type": ["string", "object", "array"],
                }
            },
            "required": ["target"],
            "additionalProperties": False,
        },
    },
    {
        "name": "look_at",
        "method": "lookAt",
        "rename": {},
        "description": "把身体朝向与俯仰对准目标 id 或世界坐标点；坐着也能转头。",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "description": "目标 id 或 {x,y,z} 世界坐标（米）",
                    "type": ["string", "object"],
                }
            },
            "required": ["target"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sit",
        "method": "sit",
        "rename": {"seat_id": "seatId"},
        "description": "坐到座位上（不会自己走过去：距座位站起点超过 1.5m 会回 too_far，先 move_to 该座位 id）。不传 posture 用座位默认姿态（椅/沙发坐下、daybed 仰卧、双人床侧躺）。lie 只适用于 daybed 与双人床。",
        "parameters": {
            "type": "object",
            "properties": {
                "seat_id": {
                    "type": "string",
                    "enum": [
                        "seat.dining.chair",
                        "seat.desk.chair",
                        "seat.her.window",
                        "seat.her.armchair",
                        "seat.living.sofa",
                        "seat.living.daybed",
                        "seat.her.bed.left",
                        "seat.her.bed.right",
                    ],
                },
                "posture": {
                    "type": "string",
                    "enum": ["sit", "lie"],
                    "description": "姿态；省略则用座位默认姿态",
                },
                "facing": {
                    "type": ["string", "object"],
                    "description": (
                        "朝向：\"player\"=面向主人；{yawDeg} 绝对朝向；{x,z} 世界毫米坐标点。"
                        "主人说「朝向我/面向我/对着我」坐或躺时必须传 \"player\"；"
                        "床有固定床头、玩家在脚侧时可能只能转头，回执会带 facing.degraded。"
                    ),
                },
            },
            "required": ["seat_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "stand",
        "method": "stand",
        "rename": {},
        "description": "从坐姿 / 躺姿站起来，回到站立待机。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "follow",
        "method": "follow",
        "rename": {},
        "description": (
            "开始跟随主人：每 0.8 秒检查一次距离，落后超过 1.1 米就走到主人身边的合法落点。"
            "这是持续模式，会一直跟到主人说停下、或调用 sit/stand/move_to/perform 等动作自动结束。"
            "不要用 move_to 冒充跟随。"
        ),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "unfollow",
        "method": "unfollow",
        "rename": {},
        "description": "停止跟随。主人说「别跟了/停下/不用跟着了」时调用。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "play_clip",
        "method": "playClip",
        "rename": {},
        "description": "播一个动作。非循环动作（wave / pick_up / turn_* / sit_down 等）要 hold=true，否则会被移动层顶回 idle。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "动作名，如 wave / idle / walk / type"},
                "timeScale": {"type": "number", "description": "播放倍速，默认 1"},
                "fadeS": {"type": "number", "description": "淡入秒数，默认 0"},
                "hold": {"type": "boolean", "description": "是否压住动作层；非循环动作建议 true"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_expression",
        "method": "setExpression",
        "rename": {},
        "description": "命名表情：neutral / happy / smile / sad / angry / surprised / shy / thinking。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": [
                        "neutral",
                        "happy",
                        "smile",
                        "sad",
                        "angry",
                        "surprised",
                        "shy",
                        "thinking",
                    ],
                },
                "intensity": {"type": "number", "description": "0–1，默认 1"},
                "fadeS": {"type": "number", "description": "淡入秒数，默认 0"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_expression",
        "method": "getExpression",
        "rename": {},
        "description": "读当前表情名、强度与本帧生效的权重。无副作用。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "start_speaking",
        "method": "startSpeaking",
        "rename": {},
        "description": "开始说话并驱动口型。有 TTS 音频时走音频链路（适配层的 speak_audio），纯文本走伪包络。",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要说的文本；没有音频时按它生成口型包络"},
                "url": {"type": "string", "description": "已可播放的音频地址（页面只记录，不播放）"},
                "durationMs": {"type": "number", "description": "预计时长毫秒，仅文本时用于包络长度"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "stop_speaking",
        "method": "stopSpeaking",
        "rename": {},
        "description": "立刻停止口型与音频，嘴回到闭合。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "set_layer",
        "method": "setLayer",
        "rename": {},
        "description": "设某层的混合权重（绝对值 0–1）：posture / look / face。立即生效，不进队列。",
        "parameters": {
            "type": "object",
            "properties": {
                "layer": {"type": "string", "enum": ["posture", "look", "face"]},
                "weight": {"type": "number", "description": "0–1"},
                "fadeS": {"type": "number", "description": "过渡秒数，默认 0"},
            },
            "required": ["layer", "weight"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_layers",
        "method": "getLayers",
        "rename": {},
        "description": "读三层权重与写入顺序。无副作用。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "set_light",
        "method": "setLight",
        "rename": {},
        "description": "开/关一盏灯。objectId 用 list_abilities 或 list_targets 里的灯 id。",
        "parameters": {
            "type": "object",
            "properties": {
                "objectId": {"type": "string", "description": "灯的对象 id，如 obj.living.ceiling-light"},
                "on": {"type": "boolean", "description": "true 开灯，false 关灯"},
            },
            "required": ["objectId", "on"],
            "additionalProperties": False,
        },
    },
    {
        "name": "open_door",
        "method": "openDoor",
        "rename": {},
        "description": "把一扇门（房间门或冰箱/衣柜门）打开。被家具挡住时回 blocked。",
        "parameters": {
            "type": "object",
            "properties": {
                "objectId": {"type": "string", "description": "门 id，如 door.her.living / obj.kitchen.fridge"},
            },
            "required": ["objectId"],
            "additionalProperties": False,
        },
    },
    {
        "name": "close_door",
        "method": "closeDoor",
        "rename": {},
        "description": "把一扇门（房间门或冰箱/衣柜门）关上。被挡住时回 blocked。",
        "parameters": {
            "type": "object",
            "properties": {
                "objectId": {"type": "string", "description": "门 id，如 door.her.living / obj.kitchen.fridge"},
            },
            "required": ["objectId"],
            "additionalProperties": False,
        },
    },
    {
        "name": "perform",
        "method": "perform",
        "rename": {},
        "description": (
            "一次编排多步：按顺序执行 move/look/sit/stand/clip/primitive/expression/light/door/wait。"
            "多个动作要一起做时优先用它，只有一步时用对应单工具。"
            "任何一步失败会停下并回 failedAt/lastCode，之前的步骤已经发生。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "description": "按顺序执行的步骤数组",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": [
                                    "move",
                                    "look",
                                    "sit",
                                    "stand",
                                    "clip",
                                    "primitive",
                                    "expression",
                                    "light",
                                    "door",
                                    "wait",
                                ],
                            },
                            "target": {
                                "type": ["string", "object", "array"],
                                "description": "move/look 的目标 id 或坐标",
                            },
                            "seatId": {"type": "string", "description": "sit 的座位 id"},
                            "posture": {"type": "string", "enum": ["sit", "lie"]},
                            "facing": {
                                "type": ["string", "object"],
                                "description": "sit 的朝向：\"player\" 或 {yawDeg} / {x,z}",
                            },
                            "name": {"type": "string", "description": "clip/primitive/expression 的名字"},
                            "hold": {"type": "boolean", "description": "clip 是否压住动作层"},
                            "timeScale": {"type": "number"},
                            "fadeS": {"type": "number"},
                            "amplitude": {"type": "number", "description": "primitive 强度 0–1"},
                            "durationS": {"type": "number", "description": "primitive 时长秒"},
                            "repeat": {"type": "number", "description": "primitive 重复次数"},
                            "side": {"type": "string", "enum": ["L", "R", "both"]},
                            "intensity": {"type": "number", "description": "expression 强度 0–1"},
                            "objectId": {"type": "string", "description": "light/door 的对象 id"},
                            "on": {"type": "boolean", "description": "light 开关"},
                            "open": {"type": "boolean", "description": "door 开合"},
                            "ms": {"type": "number", "description": "wait 毫秒，0–120000"},
                        },
                        "required": ["kind"],
                    },
                }
            },
            "required": ["steps"],
            "additionalProperties": False,
        },
    },
    {
        "name": "play_gesture",
        "method": "perform",
        "rename": {},
        "description": (
            "做一个手势基元（arm_raise 抬手 / shrug 耸肩 / head_tilt 歪头 / stretch 伸懒腰 / "
            "body_twist 扭身 / lean_in_out 前倾 / tail_wag 摇尾巴 / idle_breath 呼吸）。"
            "底层走 perform 的单步 primitive；被占用或冷却时回 started=false。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": [
                        "tail_wag",
                        "body_twist",
                        "shrug",
                        "head_tilt",
                        "arm_raise",
                        "stretch",
                        "lean_in_out",
                        "idle_breath",
                    ],
                },
                "amplitude": {"type": "number", "description": "强度 0–1，默认 1"},
                "durationS": {"type": "number", "description": "覆盖时长（秒）"},
                "repeat": {"type": "number", "description": "重复次数"},
                "side": {"type": "string", "enum": ["L", "R", "both"]},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cancel",
        "method": "cancel",
        "rename": {},
        "description": "取消当前正在跑的长命令（moveTo / 等待类）。排队项不受影响。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]

_BY_NAME = {spec["name"]: spec for spec in _SPECS}
TOOL_NAMES = tuple(spec["name"] for spec in _SPECS)

# 只读工具不占动作步数；仍由各进程单独计上限，防止刷查询拖长回复。
READONLY = frozenset(
    {"get_state", "list_targets", "list_abilities", "get_clip_state", "get_layers", "get_expression"}
)

PERFORM_TOOLS = ("perform", "play_gesture")


def _ids(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    return [str(item["id"]) for item in items if isinstance(item, dict) and item.get("id")]


def _summarize_perform(result: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    """perform 的回执压成「失败在哪一步 / 哪些步骤真的发生了」，别被 600 字截掉。"""
    out: dict[str, Any] = {
        "ok": bool(result.get("ok")),
        "code": result.get("code") or ("ok" if result.get("ok") else "internal"),
    }
    # hint 是「这一步没有发生」的硬约束，压缩时绝不能丢
    if result.get("hint"):
        out["hint"] = result["hint"]
    if detail.get("message"):
        out["message"] = detail["message"]
    for key in ("failedAt", "lastCode", "failedStep"):
        if detail.get(key) is not None:
            out[key] = detail[key]
    failed_step = detail.get("failedStep") if isinstance(detail.get("failedStep"), dict) else {}
    if failed_step.get("kind"):
        out["failedKind"] = failed_step["kind"]

    done: list[dict[str, Any]] = []
    for item in detail.get("done") or []:
        if not isinstance(item, dict):
            continue
        row: dict[str, Any] = {
            "i": item.get("index"),
            "kind": item.get("kind"),
            "ok": bool(item.get("ok")),
        }
        if item.get("code"):
            row["code"] = item["code"]
        step = item.get("detail") if isinstance(item.get("detail"), dict) else {}
        if "started" in step:
            row["started"] = bool(step.get("started"))
        if step.get("reason"):
            row["reason"] = step["reason"]
        done.append(row)
    if done:
        out["done"] = done
        skipped = [
            {"i": row["i"], "reason": row.get("reason") or "skipped"}
            for row in done
            if row.get("started") is False
        ]
        if skipped:
            out["skipped"] = skipped
    return out


def summarize(name: str, result: Any) -> Any:
    """只压缩已知会撑爆上下文的回执；其余原样返回，不改变页面语义。"""
    if not isinstance(result, dict):
        return result
    detail = result.get("detail") if isinstance(result.get("detail"), dict) else {}
    if name == "list_targets" and result.get("ok"):
        ids = _ids(detail.get("targets"))
        ids.sort(key=lambda i: (0 if i.startswith("seat.") else
                                1 if i.startswith("door.") else 2, i))
        return {"ok": True, "count": len(ids), "ids": ids}
    if name == "list_abilities" and result.get("ok"):
        clips = detail.get("clips") if isinstance(detail.get("clips"), list) else []
        return {
            "ok": True,
            "primitives": detail.get("primitives") or [],
            "expressions": detail.get("expressions") or [],
            "seats": [str(seat.get("id")) for seat in detail.get("seats") or []
                      if isinstance(seat, dict) and seat.get("id")],
            "lights": [{"id": light.get("id"), "on": light.get("on")}
                       for light in detail.get("lights") or [] if isinstance(light, dict)],
            "doors": [{"id": door.get("id"), "open": door.get("open")}
                      for door in detail.get("doors") or [] if isinstance(door, dict)],
            "clips": len(clips),
        }
    if name == "get_clip_state" and result.get("ok"):
        return {"ok": True, "clip": detail.get("clip"), "names": detail.get("names") or []}
    if name in PERFORM_TOOLS:
        return _summarize_perform(result, detail)
    return result


def failure_payload(name: str, code: str, result: Any = None) -> dict[str, Any]:
    """任何没做成的动作都带一句「这一步没有发生」，防止模型把失败叙述成完成。"""
    payload = dict(result) if isinstance(result, dict) else {"ok": False, "code": code}
    payload["ok"] = False
    payload.setdefault("code", code)
    detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else {}
    where = detail.get("current")
    state = f"你当前还在{where}。" if where else "你的位置、姿态和房间状态都没有因此改变。"
    failed_step = detail.get("failedStep") if isinstance(detail.get("failedStep"), dict) else {}
    failed_kind = failed_step.get("kind")
    done_kinds = [str(item.get("kind")) for item in detail.get("done") or []
                  if isinstance(item, dict) and item.get("ok") and item.get("kind")]
    if failed_kind:
        head = f"{name} 的 {failed_kind} 这一步没有发生（{code}）"
    else:
        head = f"{name} 没有成功（{code}）：这一步没有发生"
    done_note = f"已完成的是：{'、'.join(done_kinds)}；" if done_kinds else ""
    payload["hint"] = (
        f"{head}，{state}{done_note}"
        "不要描写这个动作已经完成，也不要说自己已经在那儿或已经做了；"
        "只说你尝试了但没做到，或说打算换个方式；想重试就再调一次工具。"
        "照常把话说下去，不要向主人报告工具错误。"
    )
    return payload


def success_hint(name: str, result: Any) -> str | None:
    """成功回执里的例外：基元被通道占用 / 冷却 / 说话打断时 started=false，也算没发生。"""
    if name not in PERFORM_TOOLS or not isinstance(result, dict) or not result.get("ok"):
        return None
    skipped = summarize(name, result).get("skipped") or []
    if not skipped:
        return None
    steps = "、".join(f"第{int(item.get('i', 0)) + 1}步" for item in skipped)
    reasons = "、".join(sorted({str(item.get("reason") or "skipped") for item in skipped}))
    return (f"{steps}手势没有发生（{reasons}）：不要说那个手势已经做了，"
            "照常用文字回应主人。")


def openai_tools() -> list[dict[str, Any]]:
    """OpenAI function-calling 形状的工具清单，可直接放进请求的 `tools`。"""
    return [
        {
            "type": "function",
            "function": {
                "name": spec["name"],
                "description": spec["description"],
                "parameters": spec["parameters"],
            },
        }
        for spec in _SPECS
    ]


def _check_types(spec: dict[str, Any], args: dict[str, Any]) -> str | None:
    props = spec["parameters"].get("properties", {})
    for key, value in args.items():
        if key not in props:
            return f"不认识的参数：{key}"
    for key in spec["parameters"].get("required", []):
        if key not in args:
            return f"缺少参数：{key}"
    _py_types = {"string": str, "number": (int, float), "boolean": bool, "object": dict, "array": list}
    for key, value in args.items():
        declared = props[key]["type"]
        allowed = declared if isinstance(declared, list) else [declared]
        if key == "target" and isinstance(value, (str, dict, list)):
            continue
        if not any(isinstance(value, _py_types[item]) for item in allowed if item in _py_types):
            return f"参数类型不对：{key} 需要 {'/'.join(allowed)}"
    return None


_ACTION_METHODS = {"moveTo", "sit", "stand", "perform", "playClip"}
_PREEMPT_OPTIONS: dict[str, Any] = {"interrupt": "now", "ttlMs": 8000}
_MODE_OPTIONS: dict[str, Any] = {"interrupt": "now", "ttlMs": 3000}


async def call(
    bridge: StageBridge,
    name: str,
    args: dict[str, Any] | None = None,
    *,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把一条工具调用翻成页面方法并等回执。

    动作类（走 / 坐 / 站 / perform / 播 clip）默认 `interrupt:"now"` + 8s ttl：
    新指令抢占旧动作，旧动作不会留到下一轮再执行（这正是「她自己又走回去」的来源）。
    跟随 / 停跟随也抢占；读状态、灯、门保持默认队列。
    """
    spec = _BY_NAME.get(name)
    if spec is None:
        return {
            "ok": False,
            "code": "unknown_tool",
            "detail": {"tool": name, "available": list(TOOL_NAMES)},
        }
    payload = dict(args or {})
    problem = _check_types(spec, payload)
    if problem:
        return {"ok": False, "code": "invalid_argument", "detail": {"message": problem, "tool": name}}
    if name == "play_gesture":
        # 桥白名单没有 aiPrimitive：基元统一走 perform 的单步 primitive。
        step: dict[str, Any] = {"kind": "primitive", "name": payload.pop("name")}
        for key in ("amplitude", "durationS", "repeat", "side"):
            if key in payload:
                step[key] = payload[key]
        return await bridge.invoke(
            "perform", {"steps": [step]}, options or dict(_PREEMPT_OPTIONS)
        )
    params = {spec["rename"].get(key, key): value for key, value in payload.items()}
    if options is None:
        if spec["method"] in _ACTION_METHODS:
            options = dict(_PREEMPT_OPTIONS)
        elif spec["method"] in {"follow", "unfollow"}:
            options = dict(_MODE_OPTIONS)
    result = await bridge.invoke(spec["method"], params, options)
    # 躺着/坐着时页面会直接拒绝 moveTo，perform 也会在起身过渡还没结束时被判 seated。
    # 先起身、等过渡走完，再重试一次，避免这一轮空手而归（13:08、13:18 实测）。
    if not result.get("ok") and result.get("code") == "seated" and spec["method"] in {"moveTo", "perform"}:
        stood = await bridge.invoke("stand", {}, dict(_PREEMPT_OPTIONS))
        if stood.get("ok"):
            await asyncio.sleep(1.7)
            retried = await bridge.invoke(spec["method"], params, options)
            detail = retried.get("detail")
            if isinstance(detail, dict):
                detail["autoStood"] = True
            return retried
    return result


async def speak_audio(
    bridge: StageBridge,
    audio_base64: str,
    *,
    mime: str = "audio/mpeg",
    text: str | None = None,
    append: bool = False,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """带宽频的口型入口：给 TTS 产出的 base64 mp3，页面解码后按真实波形驱动口型。

    这条不进 LLM 工具面（避免模型自己拼几百 KB 的 base64），只由服务器的 TTS 链路调用。
    `append=True` 时页面把这一句排在当前 AudioContext 时间轴的末尾，不覆盖正在播的句子。
    """
    params: dict[str, Any] = {"audioBase64": audio_base64, "mime": mime}
    if text:
        params["text"] = text
    if append:
        params["append"] = True
    return await bridge.invoke("startSpeaking", params, options)
