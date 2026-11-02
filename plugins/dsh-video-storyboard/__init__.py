# -*- coding: utf-8 -*-
"""DSH 改造版 + 第三方视频分镜的兼容适配层。

现状（勘察结论）：
- 用户提供的 zip 包是 `jinyu-memory-dsh`，即 DSH 宿主的本地记忆插件，
  不是 dsh-video-storyboard 的分镜源码；两者按不同能力分别检测。
- 按仓库约定，第三方与改造版源码都不提交进主仓库：本插件只做检测、读取与导入；
  安装由 scripts/install_dsh_plugin.py / scripts/install_storyboard.sh 完成。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from jinyv_memory.contract import Health, PluginContext, ToolSpec, TurnContext

from . import store


def create_plugin() -> "DshPlugin":
    return DshPlugin()


class DshPlugin:
    def setup(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.account = str(ctx.config.get("account") or "desktop")
        self.dsh_home = Path(
            str(ctx.config.get("dsh_home") or os.environ.get("DSH_HOME") or Path.home() / ".dsh")
        )
        self.sessions_dir = store.memory_root(self.dsh_home)
        self.install_dir = Path(
            str(ctx.config.get("install_dir") or self.dsh_home / "plugins" / "jinyu-memory-dsh")
        )
        self.storyboard_dir = Path(
            str(ctx.config.get("storyboard_dir") or os.environ.get("RP_STORYBOARD_DIR")
                or ctx.paths.root / "vendor" / "dsh-video-storyboard")
        )
        self.has_dsh_plugin = False
        self.has_storyboard = False
        self.session_count = 0
        ctx.tools.register(ToolSpec(
            name="dsh.status",
            description="查看 DSH 改造版、本地记忆仓与第三方分镜 CLI 的安装状态。",
            parameters={"type": "object", "properties": {}},
            handler=self._tool_status,
        ), ctx.plugin_id)
        ctx.tools.register(ToolSpec(
            name="dsh.sessions",
            description="列出 DSH 本地记忆仓中的会话及卡片/事实数量。",
            parameters={"type": "object", "properties": {}},
            handler=self._tool_sessions,
        ), ctx.plugin_id)
        ctx.tools.register(ToolSpec(
            name="dsh.import_session",
            description="把一个 DSH 会话的事件卡与事实导入核心记忆（按内容幂等）。",
            parameters={
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "DSH 会话目录名或会话 id"},
                    "account": {"type": "string", "description": "导入到哪个车道"},
                },
                "required": ["session"],
            },
            handler=self._tool_import,
        ), ctx.plugin_id)

    def start(self, ctx: PluginContext) -> None:
        self.session_count = len(store.list_sessions(self.dsh_home))
        package = self.install_dir / "package.json"
        self.has_dsh_plugin = package.exists()
        if self.has_dsh_plugin:
            try:
                import json

                data = json.loads(package.read_text(encoding="utf-8"))
                self.has_dsh_plugin = str(data.get("name") or "") == "jinyu-memory-dsh"
            except (OSError, ValueError):
                self.has_dsh_plugin = False
        self.has_storyboard = (self.storyboard_dir / "bin" / "storyboard.mjs").exists()
        if not (self.has_dsh_plugin or self.has_storyboard or self.session_count):
            ctx.degrade(
                "未检测到 DSH 改造版/分镜 CLI/本地记忆仓；"
                "请运行 scripts/install_dsh_plugin.py --source <zip|dir> "
                "或 scripts/install_storyboard.sh"
            )
            return
        ctx.logger.info("DSH 适配层就绪：plugin=%s storyboard=%s sessions=%d",
                        self.has_dsh_plugin, self.has_storyboard, self.session_count)

    def health(self, ctx: PluginContext) -> Health:
        details = {
            "dsh_home": str(self.dsh_home),
            "install_dir": str(self.install_dir),
            "dsh_plugin": self.has_dsh_plugin,
            "storyboard": self.has_storyboard,
            "sessions": self.session_count,
        }
        if ctx.degraded_reason:
            return Health.degraded(ctx.degraded_reason, **details)
        return Health.ok(**details)

    # ------------------------------------------------------------ 工具
    def _tool_status(self, args: Any, turn: TurnContext | None) -> dict:
        return {
            "ok": True,
            "dsh_home": str(self.dsh_home),
            "sessions_dir": str(self.sessions_dir),
            "install_dir": str(self.install_dir),
            "dsh_plugin": self.has_dsh_plugin,
            "storyboard_dir": str(self.storyboard_dir),
            "storyboard": self.has_storyboard,
            "sessions": self.session_count,
            "degraded": self.ctx.degraded_reason,
        }

    def _tool_sessions(self, args: Any, turn: TurnContext | None) -> dict:
        items = []
        root = store.memory_root(self.dsh_home)
        for name in store.list_sessions(self.dsh_home):
            try:
                session = store.read_session(root / name)
            except OSError:
                continue
            items.append({
                "dir": name,
                "turns": len(session.turns),
                "cards": len(session.cards),
                "facts": len(session.facts),
            })
        return {"ok": True, "sessions": items}

    def _tool_import(self, args: Any, turn: TurnContext | None) -> dict:
        session_id = str((args or {}).get("session") or "").strip()
        if not session_id:
            return {"ok": False, "error": "session 不能为空"}
        account = str((args or {}).get("account") or self.account)
        try:
            session = store.find_session(session_id, self.dsh_home)
        except FileNotFoundError as exc:
            return {"ok": False, "error": str(exc)}
        result = self.ctx.memory.import_external(account, session.cards, session.facts)
        return {"ok": True, "source_session": session.session_id, **result}
