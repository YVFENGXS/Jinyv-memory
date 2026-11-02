# -*- coding: utf-8 -*-
"""事件总线与对话钩子。

- 事件总线：topic 支持 fnmatch 通配（如 `turn.*`），处理异常只记录不扩散。
- 钩子：before / after 两段按优先级顺序执行；before 处理器可返回新的 TurnContext，
  after 处理器只观察，不改变已生成的回复。
"""
from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .contract import TurnContext

Handler = Callable[..., Any]


@dataclass(frozen=True)
class EventResult:
    topic: str
    plugin_id: str
    ok: bool
    value: Any = None
    error: str = ""


@dataclass
class _Subscription:
    pattern: str
    handler: Handler
    plugin_id: str
    priority: int


class EventBus:
    def __init__(self, logger: logging.Logger | None = None):
        self._logger = logger or logging.getLogger("jinyv.events")
        self._subs: list[_Subscription] = []

    def subscribe(self, topic: str, handler: Handler, plugin_id: str = "",
                  priority: int = 0) -> Callable[[], None]:
        sub = _Subscription(str(topic), handler, plugin_id, int(priority))
        self._subs.append(sub)
        self._subs.sort(key=lambda item: item.priority, reverse=True)

        def unsubscribe() -> None:
            if sub in self._subs:
                self._subs.remove(sub)

        return unsubscribe

    def remove_plugin(self, plugin_id: str) -> int:
        before = len(self._subs)
        self._subs = [sub for sub in self._subs if sub.plugin_id != plugin_id]
        return before - len(self._subs)

    def emit(self, topic: str, payload: Any = None) -> list[EventResult]:
        results: list[EventResult] = []
        for sub in list(self._subs):
            if not fnmatch.fnmatchcase(topic, sub.pattern):
                continue
            try:
                value = sub.handler(payload)
                results.append(EventResult(topic, sub.plugin_id, True, value))
            except Exception as exc:  # noqa: BLE001 单个订阅者失败不能影响其他插件
                self._logger.exception("事件处理器失败 topic=%s plugin=%s",
                                       topic, sub.plugin_id)
                results.append(EventResult(topic, sub.plugin_id, False,
                                           error=f"{type(exc).__name__}: {exc}"))
        return results


@dataclass
class _Hook:
    plugin_id: str
    handler: Callable[[TurnContext], Any]
    priority: int


class HookRegistry:
    """每轮对话前后的生命周期钩子。"""

    def __init__(self, logger: logging.Logger | None = None):
        self._logger = logger or logging.getLogger("jinyv.hooks")
        self._before: list[_Hook] = []
        self._after: list[_Hook] = []

    def register_before(self, plugin_id: str, handler: Callable[[TurnContext], Any],
                         priority: int = 0) -> None:
        self._before.append(_Hook(plugin_id, handler, int(priority)))
        self._before.sort(key=lambda hook: hook.priority, reverse=True)

    def register_after(self, plugin_id: str, handler: Callable[[TurnContext], Any],
                        priority: int = 0) -> None:
        self._after.append(_Hook(plugin_id, handler, int(priority)))
        self._after.sort(key=lambda hook: hook.priority, reverse=True)

    def remove_plugin(self, plugin_id: str) -> int:
        before = len(self._before) + len(self._after)
        self._before = [hook for hook in self._before if hook.plugin_id != plugin_id]
        self._after = [hook for hook in self._after if hook.plugin_id != plugin_id]
        return before - len(self._before) - len(self._after)

    def run_before(self, turn: TurnContext) -> TurnContext:
        errors: list[str] = []
        current = turn
        for hook in list(self._before):
            try:
                result = hook.handler(current)
                if isinstance(result, TurnContext):
                    current = result
            except Exception as exc:  # noqa: BLE001
                message = f"{hook.plugin_id}: {type(exc).__name__}: {exc}"
                errors.append(message)
                self._logger.exception("before 钩子失败 %s", message)
        if errors:
            current.metadata.setdefault("hook_errors", []).extend(errors)
        return current

    def run_after(self, turn: TurnContext, reply: str) -> list[str]:
        errors: list[str] = []
        for hook in list(self._after):
            try:
                hook.handler(turn, reply)
            except Exception as exc:  # noqa: BLE001
                message = f"{hook.plugin_id}: {type(exc).__name__}: {exc}"
                errors.append(message)
                self._logger.exception("after 钩子失败 %s", message)
        if errors:
            turn.metadata.setdefault("hook_errors", []).extend(errors)
        return errors
