# -*- coding: utf-8 -*-
"""QQ 插件适配层：OneBot v11 消息 → 记忆核心一轮对话。

本层不复制旧 196KB 的 NoneBot 入口，只保留稳定契约：
- 工具：qqbot.remember / qqbot.recall / qqbot.status
- 钩子：每轮对话前召回、对话后发事件
- 定时任务：心跳
- 可选消息入口：装 nonebot2 时由 nonebot_adapter 注册 matcher，否则降级
"""
from __future__ import annotations

import importlib.util
import threading
import time
from dataclasses import replace
from typing import Any

from jinyv_memory.contract import (
    Health, PluginContext, PluginManifest, ScheduleSpec, ToolSpec, TurnContext,
)


def create_plugin() -> "QQBotPlugin":
    return QQBotPlugin()


class QQBotPlugin:
    def setup(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        cfg = ctx.config
        self.default_account = str(cfg.get("default_account") or ctx.accounts.default_key)
        self.cooldown = float(cfg.get("reply_cooldown_seconds") or 0)
        self._last_reply_at: dict[str, float] = {}
        self._lock = threading.Lock()
        self.nonebot_ready = False

        ctx.tools.register(ToolSpec(
            name="qqbot.remember",
            description="把一条长期事实写进当前会话记忆。",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要记住的事实"},
                    "speaker": {"type": "string", "description": "事实归属人"},
                    "importance": {"type": "number", "minimum": 0, "maximum": 1},
                    "account": {"type": "string", "description": "账号车道名"},
                },
                "required": ["text"],
            },
            handler=self._tool_remember,
        ), ctx.plugin_id)
        ctx.tools.register(ToolSpec(
            name="qqbot.recall",
            description="按当前问题召回相关长期事实与事件卡。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    "account": {"type": "string"},
                },
                "required": ["query"],
            },
            handler=self._tool_recall,
        ), ctx.plugin_id)
        ctx.tools.register(ToolSpec(
            name="qqbot.status",
            description="查看当前账号车道的记忆计数与归档游标。",
            parameters={"type": "object", "properties": {"account": {"type": "string"}}},
            handler=self._tool_status,
        ), ctx.plugin_id)

        ctx.hooks.register_before(ctx.plugin_id, self._before_turn, priority=10)
        ctx.hooks.register_after(ctx.plugin_id, self._after_turn, priority=10)
        ctx.schedules.register(ScheduleSpec(
            job_id="qqbot.heartbeat",
            interval_seconds=300,
            handler=self._heartbeat,
            plugin_id=ctx.plugin_id,
            run_on_start=False,
        ), ctx.plugin_id)

        mode = str(cfg.get("nonebot") or "auto").lower()
        if mode != "off":
            try:
                from . import nonebot_adapter

                nonebot_adapter.install(ctx, self)
                self.nonebot_ready = True
            except ImportError as exc:
                ctx.degrade(f"nonebot2 未安装，消息入口降级为工具/钩子：{exc}")
            except Exception as exc:  # noqa: BLE001 适配层失败不影响核心功能
                ctx.degrade(f"nonebot 消息入口注册失败：{type(exc).__name__}: {exc}")

    def start(self, ctx: PluginContext) -> None:
        ctx.logger.info("QQ 插件适配层就绪：nonebot=%s default_account=%s",
                        self.nonebot_ready, self.default_account)

    def health(self, ctx: PluginContext) -> Health:
        if ctx.degraded_reason:
            return Health.degraded(ctx.degraded_reason, nonebot=self.nonebot_ready)
        return Health.ok(nonebot=self.nonebot_ready, default_account=self.default_account)

    # ------------------------------------------------------------ 消息入口
    def handle_message(self, text: str, *, account: str | None = None,
                       session_id: str | None = None, speaker: str = "",
                       source: str = "qq") -> str:
        """供 NoneBot matcher 或其他传输层调用的最小闭环。"""
        ctx = self.ctx
        lane = ctx.accounts.get(account or self.default_account)
        if session_id:
            matched = ctx.accounts.match_session(lane.platform, session_id)
            lane = matched or replace(lane, session_id=session_id)
        if self.cooldown > 0:
            now = time.monotonic()
            with self._lock:
                last = self._last_reply_at.get(lane.key, 0.0)
                if now - last < self.cooldown:
                    return ""
                self._last_reply_at[lane.key] = now
        turn = TurnContext.from_lane(
            lane, str(text or "").strip(), speaker=speaker, source=source,
            metadata={"lane": lane.key},
        )
        ctx.hooks.run_before(turn)
        reply = ctx.memory.chat(lane, turn.text, speaker=speaker, source=source)
        ctx.hooks.run_after(turn, reply)
        ctx.events.emit("qqbot.turn.completed", {
            "lane": lane.key,
            "session_id": lane.session_id,
            "speaker": speaker,
            "text": turn.text,
            "reply": reply,
            "metadata": turn.metadata,
        })
        return reply

    # ------------------------------------------------------------ 钩子
    def _before_turn(self, turn: TurnContext) -> None:
        ctx = self.ctx
        if not turn.text:
            return
        lane_key = str(turn.metadata.get("lane") or self.default_account)
        try:
            result = ctx.memory.recall(lane_key, turn.text, limit=4, speaker=turn.speaker)
            turn.metadata["memory_text"] = result.text
            turn.metadata["memory_recall"] = {
                "cards": [hit.__dict__ for hit in result.cards],
                "facts": [hit.__dict__ for hit in result.facts],
            }
        except Exception as exc:  # noqa: BLE001 召回失败降级为无记忆上下文
            turn.metadata["memory_recall_error"] = f"{type(exc).__name__}: {exc}"
            ctx.logger.warning("QQ 钩子召回失败 lane=%s: %s", lane_key, exc)

    def _after_turn(self, turn: TurnContext, reply: str) -> None:
        self.ctx.events.emit("qqbot.turn.after", {
            "lane": turn.metadata.get("lane", ""),
            "reply_length": len(reply or ""),
            "hook_errors": list(turn.metadata.get("hook_errors") or []),
        })

    # ------------------------------------------------------------ 工具
    def _tool_remember(self, args: Any, turn: TurnContext | None) -> dict:
        text = str((args or {}).get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "text 不能为空"}
        lane_key = str((args or {}).get("account") or self.default_account)
        fact_id = self.ctx.memory.remember(
            lane_key,
            text,
            speaker=str((args or {}).get("speaker") or (turn.speaker if turn else "")),
            importance=float((args or {}).get("importance") or 0.7),
        )
        return {"ok": bool(fact_id), "fact_id": fact_id, "duplicate": not bool(fact_id)}

    def _tool_recall(self, args: Any, turn: TurnContext | None) -> dict:
        query = str((args or {}).get("query") or (turn.text if turn else "")).strip()
        lane_key = str((args or {}).get("account") or self.default_account)
        result = self.ctx.memory.recall(
            lane_key, query,
            limit=int((args or {}).get("limit") or 6),
            speaker=turn.speaker if turn else "",
        )
        return result.to_dict()

    def _tool_status(self, args: Any, turn: TurnContext | None) -> dict:
        lane_key = str((args or {}).get("account") or self.default_account)
        return self.ctx.memory.status(lane_key)

    # ------------------------------------------------------------ 定时任务
    def _heartbeat(self) -> None:
        self.ctx.events.emit("qqbot.heartbeat", {
            "default_account": self.default_account,
            "nonebot": self.nonebot_ready,
        })
