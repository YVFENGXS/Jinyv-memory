# -*- coding: utf-8 -*-
"""视频通话服务适配层。

边界：
- 服务源码在独立仓库 YVFENGXS/jinyu-video-call，主仓库不复制其 Live2D 舞台、
  ASR/TTS 或画面判读代码。
- 本插件负责：lane 绑定、服务发现、健康检查、跨窗口发话桥（后续阶段实现）。
- 记忆写入统一走 MemoryAPI；通话服务通过宿主 API 或本插件桥接。
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

from jinyv_memory.contract import Health, PluginContext, ToolSpec, TurnContext


def create_plugin() -> "VideoCallPlugin":
    return VideoCallPlugin()


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError):
        return False


class VideoCallPlugin:
    REPO = "https://github.com/YVFENGXS/jinyu-video-call"

    def setup(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.account = str(ctx.config.get("account") or "video_call")
        self.service_url = str(ctx.config.get("service_url") or "")
        self.repo_dir = ""
        self.serve = bool(ctx.config.get("serve", False))
        ctx.tools.register(ToolSpec(
            name="video_call.status",
            description="查看视频通话服务与适配层状态。",
            parameters={"type": "object", "properties": {}},
            handler=self._tool_status,
        ), ctx.plugin_id)
        ctx.tools.register(ToolSpec(
            name="video_call.say",
            description="把一句话推到通话页播放（跨窗口发话）。",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            handler=self._tool_say,
        ), ctx.plugin_id)

    def start(self, ctx: PluginContext) -> None:
        self.repo_dir = str(ctx.config.get("repo_dir") or os.environ.get("JINYV_VIDEO_CALL_DIR") or "")
        if not self.repo_dir:
            ctx.degrade(f"未配置服务目录；请独立安装 {self.REPO}")
            return
        if not Path(self.repo_dir).exists():
            ctx.degrade(f"服务目录不存在：{self.repo_dir}")
            return
        if self.serve and not _has_module("fastapi"):
            ctx.degrade("fastapi 未安装，无法在本进程拉起通话服务")
            return
        ctx.logger.info("视频通话适配层就绪：repo_dir=%s", self.repo_dir)

    def health(self, ctx: PluginContext) -> Health:
        if ctx.degraded_reason:
            return Health.degraded(ctx.degraded_reason, service_url=self.service_url)
        return Health.ok(service_url=self.service_url, repo_dir=self.repo_dir,
                         account=self.account)

    # ------------------------------------------------------------ 桥接接口
    def memory_context(self, query: str, *, speaker: str = "") -> str:
        """给通话服务拼一段可注入的记忆文本（只读，不写）。"""
        lane = self.ctx.accounts.get(self.account)
        result = self.ctx.memory.recall(lane, query, limit=4, speaker=speaker)
        return result.text

    def record_turn(self, text: str, reply: str, *, speaker: str = "") -> None:
        """通话产生的一轮写入共享记忆；store_reply 保持通话原文。"""
        lane = self.ctx.accounts.get(self.account)
        self.ctx.memory.chat(lane, text, speaker=speaker, source="video-call",
                             store_reply=reply)

    # ------------------------------------------------------------ 工具
    def _tool_status(self, args: Any, turn: TurnContext | None) -> dict:
        return {
            "ok": True,
            "service_url": self.service_url,
            "repo_dir": self.repo_dir,
            "account": self.account,
            "degraded": self.ctx.degraded_reason,
        }

    def _tool_say(self, args: Any, turn: TurnContext | None) -> dict:
        text = str((args or {}).get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "text 不能为空"}
        return {"ok": False, "queued": False,
                "error": "视频通话跨窗口发话本轮未实现"}
