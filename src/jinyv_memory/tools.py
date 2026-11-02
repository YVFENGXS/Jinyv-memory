# -*- coding: utf-8 -*-
"""工具注册表：插件注册模型可调用的工具，名称全局唯一，按插件整体回滚。"""
from __future__ import annotations

import asyncio
import inspect
import logging
import re
from typing import Any, Mapping

from .contract import ToolResult, ToolSpec, TurnContext
from .errors import PluginError

_NAME = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")


class ToolRegistry:
    def __init__(self, logger: logging.Logger | None = None):
        self._logger = logger or logging.getLogger("jinyv.tools")
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec, plugin_id: str = "") -> None:
        name = str(spec.name or "").strip()
        if not _NAME.match(name):
            raise PluginError(f"工具名不合法：{name!r}（要求小写字母开头、2-64 字符）")
        if name in self._tools:
            owner = self._tools[name].plugin_id
            raise PluginError(f"工具名冲突：{name} 已被插件 {owner or 'core'} 注册")
        if not callable(spec.handler):
            raise PluginError(f"工具 {name} 的 handler 不可调用")
        self._tools[name] = ToolSpec(
            name=name,
            description=spec.description,
            parameters=dict(spec.parameters),
            handler=spec.handler,
            plugin_id=plugin_id or spec.plugin_id,
            timeout_seconds=float(spec.timeout_seconds),
        )

    def remove_plugin(self, plugin_id: str) -> int:
        names = [name for name, spec in self._tools.items() if spec.plugin_id == plugin_id]
        for name in names:
            self._tools.pop(name, None)
        return len(names)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def list(self) -> list[ToolSpec]:
        return sorted(self._tools.values(), key=lambda spec: spec.name)

    def call(self, name: str, args: Mapping[str, Any] | None = None,
             turn: TurnContext | None = None) -> ToolResult:
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(False, error=f"未知工具：{name}")
        try:
            value = spec.handler(dict(args or {}), turn)
            if inspect.isawaitable(value):
                try:
                    value = asyncio.run(value)
                except RuntimeError as exc:
                    return ToolResult(False, error=f"异步工具需在无事件循环的线程调用：{exc}")
            return ToolResult(True, value=value)
        except Exception as exc:  # noqa: BLE001 工具失败返回结构化错误，不打断对话
            self._logger.exception("工具执行失败 name=%s", name)
            return ToolResult(False, error=f"{type(exc).__name__}: {exc}")
