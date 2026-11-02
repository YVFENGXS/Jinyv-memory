# -*- coding: utf-8 -*-
"""桌宠服务端适配层（desktop-v1）。

边界：
- 客户端是独立仓库 YVFENGXS/Jinyu-pet，源码与 Electron 资产不进入主仓库。
- 本插件只负责协议入口、lane 绑定、健康检查与推送桥；记忆仍走 MemoryAPI。
- 本轮只落结构与接口；真正的 WebSocket 服务在后续阶段包装 desktop_server.py。
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

from jinyv_memory.contract import Health, PluginContext, ToolSpec, TurnContext


def create_plugin() -> "DesktopPetPlugin":
    return DesktopPetPlugin()


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError):
        return False


class DesktopPetPlugin:
    CLIENT_REPO = "https://github.com/YVFENGXS/Jinyu-pet"

    def setup(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.account = str(ctx.config.get("account") or "desktop")
        self.service_url = str(ctx.config.get("service_url") or "")
        self.serve = bool(ctx.config.get("serve", False))
        self.connected = False
        ctx.tools.register(ToolSpec(
            name="desktop.status",
            description="查看桌宠适配层与客户端连接状态。",
            parameters={"type": "object", "properties": {}},
            handler=self._tool_status,
        ), ctx.plugin_id)
        ctx.tools.register(ToolSpec(
            name="desktop.say",
            description="向已连接的桌宠设备推送一句话。",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            handler=self._tool_say,
        ), ctx.plugin_id)

    def start(self, ctx: PluginContext) -> None:
        missing = [name for name in ("fastapi", "uvicorn", "websockets") if not _has_module(name)]
        client_dir = str(ctx.config.get("client_dir") or os.environ.get("JINYV_DESKTOP_PET_DIR") or "")
        if missing and self.serve:
            ctx.degrade(f"desktop-v1 依赖缺失：{', '.join(missing)}")
            return
        if not client_dir:
            ctx.degrade(f"未配置客户端目录；客户端请独立安装 {self.CLIENT_REPO}")
            return
        if not Path(client_dir).exists():
            ctx.degrade(f"客户端目录不存在：{client_dir}")
            return
        ctx.logger.info("桌宠适配层就绪：client_dir=%s", client_dir)

    def health(self, ctx: PluginContext) -> Health:
        if ctx.degraded_reason:
            return Health.degraded(ctx.degraded_reason, connected=self.connected,
                                   service_url=self.service_url)
        return Health.ok(connected=self.connected, service_url=self.service_url,
                         account=self.account)

    # ------------------------------------------------------------ 协议入口
    def handle_hello(self, payload: dict[str, Any]) -> dict[str, Any]:
        """desktop-v1 hello 的最小处理：未来接入令牌与 capability 协商。"""
        self.connected = True
        return {
            "type": "hello.ok",
            "protocol": "desktop-v1",
            "capabilities": ["bubble.v1", "presence.report"],
            "server": "jinyv-memory",
        }

    def handle_text(self, text: str, *, speaker: str = "") -> str:
        """设备上行文字 → 核心一轮对话；与 QQ 共享同一条 lane 记忆。"""
        lane = self.ctx.accounts.get(self.account)
        return self.ctx.memory.chat(lane, text, speaker=speaker, source="desktop")

    # ------------------------------------------------------------ 工具
    def _tool_status(self, args: Any, turn: TurnContext | None) -> dict:
        return {
            "ok": True,
            "connected": self.connected,
            "service_url": self.service_url,
            "account": self.account,
            "degraded": self.ctx.degraded_reason,
        }

    def _tool_say(self, args: Any, turn: TurnContext | None) -> dict:
        text = str((args or {}).get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "text 不能为空"}
        # 推送通道尚未落地：明确返回未连接，避免假装成功
        return {"ok": False, "queued": False,
                "error": "desktop-v1 推送通道本轮未实现" if not self.connected else "设备未订阅"}
