# -*- coding: utf-8 -*-
"""插件契约（签名级真源）。

设计原则：
1. 插件只依赖本文件暴露的类型与 PluginContext，不直接依赖 core 内部模块。
2. 注册动作都带 plugin_id，加载器可在插件启动失败时整体回滚。
3. 钩子、工具、定时任务、事件、记忆、配置六类能力都由 ctx 提供。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol, Sequence

if TYPE_CHECKING:  # 仅用于类型提示，避免 contract 与实现模块循环导入
    from .accounts import AccountRegistry, Lane
    from .config import PluginConfig
    from .core.lease import LeaseManager
    from .core.memory import MemoryAPI
    from .events import EventBus, HookRegistry
    from .paths import Paths
    from .schedules import ScheduleRegistry
    from .tools import ToolRegistry


class PluginState(str, Enum):
    DISCOVERED = "discovered"
    LOADED = "loaded"
    SETUP = "setup"
    RUNNING = "running"
    DEGRADED = "degraded"
    FAILED = "failed"
    STOPPED = "stopped"
    DISABLED = "disabled"


@dataclass(frozen=True)
class PluginManifest:
    """plugin.toml 的 [plugin] 段。"""

    id: str
    name: str
    version: str
    entry: str = "__init__.py"
    description: str = ""
    kind: str = "extension"
    requires: tuple[str, ...] = ()
    optional_modules: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()
    default_enabled: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], *, fallback_id: str = "") -> "PluginManifest":
        def as_tuple(value: Any) -> tuple[str, ...]:
            if value is None:
                return ()
            if isinstance(value, str):
                return (value,) if value else ()
            return tuple(str(item) for item in value)

        plugin_id = str(raw.get("id") or fallback_id).strip()
        if not plugin_id:
            raise ValueError("plugin.toml 缺少 [plugin].id")
        return cls(
            id=plugin_id,
            name=str(raw.get("name") or plugin_id),
            version=str(raw.get("version") or "0.0.0"),
            entry=str(raw.get("entry") or "__init__.py"),
            description=str(raw.get("description") or ""),
            kind=str(raw.get("kind") or "extension"),
            requires=as_tuple(raw.get("requires")),
            optional_modules=as_tuple(raw.get("optional_modules")),
            provides=as_tuple(raw.get("provides")),
            default_enabled=bool(raw.get("default_enabled", True)),
        )


@dataclass(frozen=True)
class Health:
    """插件健康状态；state 取 ok / degraded / failed。"""

    state: str = "ok"
    reason: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, **details: Any) -> "Health":
        return cls(state="ok", details=details)

    @classmethod
    def degraded(cls, reason: str, **details: Any) -> "Health":
        return cls(state="degraded", reason=reason, details=details)

    @classmethod
    def failed(cls, reason: str, **details: Any) -> "Health":
        return cls(state="failed", reason=reason, details=details)


@dataclass
class TurnContext:
    """一轮对话的跨插件上下文。钩子可在 metadata 中附加只读观察结果。"""

    platform: str = ""
    account: str = ""
    bot_id: str = ""
    session_id: str = ""
    character_id: str = ""
    speaker: str = ""
    text: str = ""
    source: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_lane(cls, lane: "Lane", text: str, **kwargs: Any) -> "TurnContext":
        return cls(
            platform=lane.platform,
            account=lane.account,
            bot_id=lane.bot_id,
            session_id=lane.session_id,
            character_id=lane.character_id,
            text=text,
            **kwargs,
        )


@dataclass(frozen=True)
class ToolSpec:
    """模型可见工具的注册描述。handler(args, turn) 返回值会交给调用方。"""

    name: str
    description: str
    parameters: Mapping[str, Any]
    handler: Callable[[Mapping[str, Any], TurnContext | None], Any]
    plugin_id: str = ""
    timeout_seconds: float = 15.0


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    value: Any = None
    error: str = ""


@dataclass(frozen=True)
class ScheduleSpec:
    """定时任务描述。interval_seconds 最小 1 秒；后续可扩展 cron。"""

    job_id: str
    interval_seconds: float
    handler: Callable[[], Any]
    plugin_id: str = ""
    run_on_start: bool = False
    lease_ttl: float = 60.0


class Plugin(Protocol):
    """插件对象协议。setup 必选；start/stop/health 可选。"""

    def setup(self, ctx: "PluginContext") -> None: ...

    def start(self, ctx: "PluginContext") -> None: ...

    def stop(self, ctx: "PluginContext") -> None: ...

    def health(self, ctx: "PluginContext") -> Health: ...


@dataclass
class PluginContext:
    """加载器注入给每个插件的唯一入口。"""

    plugin_id: str
    manifest: PluginManifest
    config: "PluginConfig"
    paths: "Paths"
    memory: "MemoryAPI"
    accounts: "AccountRegistry"
    leases: "LeaseManager"
    events: "EventBus"
    hooks: "HookRegistry"
    tools: "ToolRegistry"
    schedules: "ScheduleRegistry"
    logger: logging.Logger
    runtime: Any = None
    _degrade_reason: str = ""

    def degrade(self, reason: str) -> None:
        """标记插件降级：仍然完成注册，但健康检查会报告原因。"""
        self._degrade_reason = str(reason or "未知原因").strip()

    @property
    def degraded_reason(self) -> str:
        return self._degrade_reason

    @property
    def data_dir(self) -> str:
        return str(self.config.data_dir)


@dataclass
class PluginRecord:
    """加载器内部的插件记录。"""

    manifest: PluginManifest
    path: str
    state: PluginState = PluginState.DISCOVERED
    plugin: Any = None
    context: PluginContext | None = None
    error: str = ""
    health: Health = field(default_factory=Health)
    enabled: bool = True


def as_tuple(value: Sequence[str] | str | None) -> tuple[str, ...]:
    """配置里既允许字符串也允许数组。"""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    return tuple(str(item) for item in value)
