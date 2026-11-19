"""端到端演示：服务器侧发指令 → 本地桥 → 房间页面 → 身体，然后回读状态。

    node tools/ai-bridge.mjs --token <token>              # 房间侧，另开一个终端
    python tools/stage_demo.py --token <token>             # 服务器侧

可选：
    --seat seat.her.window   走到并坐下的坐点
    --audio path/to.mp3      用真实 TTS 音频驱动口型（不给就用 --text 的伪包络）
    --text "晚上好"           说话内容
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jinyv_stage.bridge import StageBridge, health  # noqa: E402
from jinyv_stage.tools import TOOL_NAMES, call, speak_audio  # noqa: E402


def brief(detail: dict, limit: int = 110) -> str:
    text = " ".join(f"{key}={value}" for key, value in detail.items() if key not in ("names", "available"))
    return text[:limit] + ("…" if len(text) > limit else "")


def show(step: str, result: dict, ms: float, rows: list[tuple[str, str, bool, str, int]]) -> None:
    code = result.get("code") or "ok"
    mark = "OK " if result.get("ok") else "ERR"
    print(f"[{mark}] {step:<16} {code:<18} {ms:6.0f} ms  {brief(result.get('detail') or {})}")
    rows.append((step, code, bool(result.get("ok")), brief(result.get("detail") or {}), int(ms)))


async def run(args: argparse.Namespace) -> int:
    rows: list[tuple[str, str, bool, str, int]] = []
    failures = 0

    try:
        probe = health(args.bridge)
        print(f"[health] roomConnected={probe.get('roomConnected')} controllers={probe.get('controllers')}"
              f" pending={probe.get('pending')}")
        if not probe.get("roomConnected"):
            print("[fatal] 房间页面还没连上桥：先用带 ?aiToken= 的地址打开房间页。")
            return 2
    except Exception as error:  # noqa: BLE001
        print(f"[fatal] 桥不可达：{error}")
        return 2

    bridge = StageBridge(args.bridge, token=args.token, timeout=args.timeout)
    try:
        await bridge.connect()
    except Exception as error:  # noqa: BLE001
        print(f"[fatal] 连接桥失败：{error}")
        return 2
    print(f"[bridge] hello protocol={bridge.hello.get('protocol')} roomConnected={bridge.room_connected}"
          f" methods={len(bridge.methods)} tools={len(TOOL_NAMES)}")

    async def step(name: str, tool: str, payload: dict | None = None) -> dict:
        nonlocal failures
        started = time.perf_counter()
        try:
            result = await call(bridge, tool, payload)
        except Exception as error:  # noqa: BLE001
            result = {"ok": False, "code": "transport_error", "detail": {"message": str(error)}}
        show(name, result, (time.perf_counter() - started) * 1000, rows)
        if not result.get("ok"):
            failures += 1
        return result

    try:
        state = await step("get_state", "get_state")
        room = (state.get("detail") or {}).get("room") or {}
        light_before = next((light.get("on") for light in room.get("lights") or []
                             if light.get("id") == args.light), None)
        door_before = next((door.get("open") for door in room.get("doors") or []
                            if door.get("id") == args.door), None)

        targets = await step("list_targets", "list_targets")
        target_count = len((targets.get("detail") or {}).get("targets") or [])
        await step("list_abilities", "list_abilities")

        await step("move_to", "move_to", {"target": args.seat})
        await step("sit", "sit", {"seat_id": args.seat})
        await step("stand", "stand")
        await step("play_clip", "play_clip", {"name": "wave", "hold": True})
        await step("set_expression", "set_expression", {"name": "happy", "intensity": 0.8})
        await step("play_gesture", "play_gesture",
                   {"name": "arm_raise", "side": "R", "amplitude": 0.9})

        started = time.perf_counter()
        if args.audio:
            data = Path(args.audio).read_bytes()
            result = await speak_audio(
                bridge, base64.b64encode(data).decode("ascii"), text=args.text, options=None
            )
            step_name = f"speak_audio({len(data) // 1024}KB)"
        else:
            result = await call(bridge, "start_speaking", {"text": args.text, "durationMs": 4000})
            step_name = "start_speaking"
        show(step_name, result, (time.perf_counter() - started) * 1000, rows)
        if not result.get("ok"):
            failures += 1

        # 多步编排：走位 → 坐下 → 抬手 → 表情 → 等待 → 站起
        await step("perform", "perform", {"steps": [
            {"kind": "move", "target": args.seat},
            {"kind": "sit", "seatId": args.seat},
            {"kind": "primitive", "name": "arm_raise", "side": "R", "amplitude": 0.9},
            {"kind": "expression", "name": "happy", "intensity": 0.8},
            {"kind": "wait", "ms": 600},
            {"kind": "stand"},
        ]})

        # 灯 / 门：各改一次再复原，别把房间留在测试状态
        if light_before is not None:
            await step("set_light", "set_light", {"objectId": args.light, "on": not light_before})
            await step("set_light#restore", "set_light",
                       {"objectId": args.light, "on": light_before})
        if door_before is not None:
            change = "close_door" if door_before else "open_door"
            restore = "open_door" if door_before else "close_door"
            await step(change, change, {"objectId": args.door})
            await step(restore + "#restore", restore, {"objectId": args.door})

        await asyncio.sleep(1.2)
        await step("get_clip_state", "get_clip_state")
        await step("get_state#2", "get_state")

        # 反例 1：页面自己的校验（未知动作名）
        started = time.perf_counter()
        bad = await call(bridge, "play_clip", {"name": "not_a_clip"})
        print(f"[{'ERR' if not bad.get('ok') else 'OK '}] {'play_clip(bad)':<16} "
              f"{bad.get('code') or 'ok':<18} {(time.perf_counter() - started) * 1000:6.0f} ms  "
              f"{brief(bad.get('detail') or {})}")
        rows.append(("play_clip(bad)", bad.get("code") or "ok", bool(bad.get("ok")), brief(bad.get("detail") or {}),
                     int((time.perf_counter() - started) * 1000)))
        expect_invalid = bad.get("code") == "invalid_argument"
        print(f"[check] 未知动作被拒（invalid_argument）：{'PASS' if expect_invalid else 'FAIL'}")
        failures += 0 if expect_invalid else 1

        # 反例 2：桥的方法白名单挡掉存档写入口
        started = time.perf_counter()
        blocked = await bridge.invoke("saveState", {})
        print(f"[{'ERR' if not blocked.get('ok') else 'OK '}] {'saveState(blocked)':<16} "
              f"{blocked.get('code') or 'ok':<18} {(time.perf_counter() - started) * 1000:6.0f} ms")
        rows.append(("saveState(blocked)", blocked.get("code") or "ok", bool(blocked.get("ok")), "", 0))
        expect_block = blocked.get("code") == "unknown_method"
        print(f"[check] 存档写入口被白名单拒绝（unknown_method）：{'PASS' if expect_block else 'FAIL'}")
        failures += 0 if expect_block else 1

        await step("stop_speaking", "stop_speaking")
    finally:
        await bridge.close()

    print("\n目标数：", target_count)
    print(f"{'step':<22}{'code':<20}{'ms':>7}  detail")
    for name, code, ok, detail, ms in rows:
        print(f"{name:<22}{code:<20}{ms:>7}  {detail}")

    print(f"\n结果：{len(rows)} 步，非预期失败 {failures} 项")
    return 0 if failures == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="房间身体桥端到端演示")
    parser.add_argument("--bridge", default=os.environ.get("JINYV_AI_BRIDGE", "ws://127.0.0.1:8765"))
    parser.add_argument("--token", default=os.environ.get("JINYV_AI_TOKEN"))
    parser.add_argument("--seat", default="seat.her.window")
    parser.add_argument("--light", default="obj.living.ceiling-light")
    parser.add_argument("--door", default="door.her.living")
    parser.add_argument("--audio", default=None, help="mp3 路径；给了就用真实波形驱动口型")
    parser.add_argument("--text", default="晚上好，我过来找你了。")
    parser.add_argument("--timeout", type=float, default=25.0)
    args = parser.parse_args()
    if not args.token:
        parser.error("需要 --token（或 JINYV_AI_TOKEN）")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
