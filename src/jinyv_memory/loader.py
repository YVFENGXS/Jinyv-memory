# -*- coding: utf-8 -*-
"""插件发现、依赖解析、生命周期与整体回滚。

约定：
- 每个插件目录必须有 plugin.toml；入口默认 __init__.py，按 Python 包加载，
  因此插件内部可以使用相对导入（如 `from . import nonebot_adapter`）。
- 插件模块暴露 `PLUGIN` 对象、`create_plugin()` 工厂，或模块级 setup/start/stop。
- 单个插件 setup/start 失败只影响它自己；注册的工具、钩子、事件、定时任务
  会按 plugin_id 整体撤销。
"""
from __future__ import annotations

import importlib.util
import logging
import re
import sys
import tomllib
from pathlib import Path
from typing import Any, Iterable

from . import __version__
from .config import Config
from .contract import (
    Health, PluginContext, PluginManifest, PluginRecord, PluginState,
)
from .errors import PluginDependencyError, PluginLoadError
from .events import EventBus, HookRegistry
from .schedules import ScheduleRegistry, Scheduler
from .tools import ToolRegistry

_PLUGIN_NAME = re.compile(r"[^a-zA-Z0-9_]")


class _ModulePlugin:
    """兼容只提供模块级函数的插件。"""

    def __init__(self, module: Any, manifest: PluginManifest):
        self._module = module
        self.manifest = manifest

    def setup(self, ctx: PluginContext) -> None:
        if hasattr(self._module, "setup"):
            self._module.setup(ctx)

    def start(self, ctx: PluginContext) -> None:
        if hasattr(self._module, "start"):
            self._module.start(ctx)

    def stop(self, ctx: PluginContext) -> None:
        if hasattr(self._module, "stop"):
            self._module.stop(ctx)

    def health(self, ctx: PluginContext) -> Health:
        if hasattr(self._module, "health"):
            return self._module.health(ctx)
        return Health.ok()


class PluginLoader:
    def __init__(self, *, root: Path, config: Config, runtime: Any,
                 logger: logging.Logger | None = None):
        self.root = Path(root)
        self.config = config
        self.runtime = runtime
        self.logger = logger or logging.getLogger("jinyv.loader")
        self.events = EventBus(self.logger)
        self.hooks = HookRegistry(self.logger)
        self.tools = ToolRegistry(self.logger)
        self.schedules = ScheduleRegistry(self.logger)
        self.scheduler = Scheduler(self.schedules, runtime.leases, self.logger)
        self.records: dict[str, PluginRecord] = {}
        self._order: list[str] = []
        self._discovery_errors: list[str] = []

    # ------------------------------------------------------------ 发现
    def discover(self, plugin_dirs: Iterable[str | Path] | None = None) -> list[PluginManifest]:
        dirs = [Path(path) for path in (plugin_dirs or [self.root / "plugins"])]
        for plugin_dir in dirs:
            if not plugin_dir.exists():
                self.logger.warning("插件目录不存在：%s", plugin_dir)
                continue
            for child in sorted(plugin_dir.iterdir()):
                if not child.is_dir() or child.name.startswith((".", "_")):
                    continue
                manifest_file = child / "plugin.toml"
                if not manifest_file.exists():
                    continue
                try:
                    with manifest_file.open("rb") as fh:
                        raw = tomllib.load(fh)
                except Exception as exc:  # noqa: BLE001
                    message = f"{child.name}: plugin.toml 解析失败 {exc}"
                    self._discovery_errors.append(message)
                    self.logger.error(message)
                    continue
                section = raw.get("plugin", raw)
                try:
                    manifest = PluginManifest.from_mapping(section, fallback_id=child.name)
                except Exception as exc:  # noqa: BLE001
                    message = f"{child.name}: {exc}"
                    self._discovery_errors.append(message)
                    self.logger.error(message)
                    continue
                if manifest.id in self.records:
                    message = f"插件 id 重复：{manifest.id}（{child}）"
                    self._discovery_errors.append(message)
                    self.logger.error(message)
                    continue
                enabled = bool(self.config.plugin_raw(manifest.id).get(
                    "enabled", manifest.default_enabled
                ))
                record = PluginRecord(
                    manifest=manifest,
                    path=str(child),
                    state=PluginState.DISCOVERED,
                    enabled=enabled,
                )
                self.records[manifest.id] = record
        return [record.manifest for record in self.records.values()]

    # ------------------------------------------------------------ 加载
    def load_all(self) -> list[PluginRecord]:
        self._order = self._resolve_order()
        for plugin_id in self._order:
            record = self.records[plugin_id]
            if not record.enabled:
                record.state = PluginState.DISABLED
                continue
            if record.state in {PluginState.FAILED, PluginState.DISABLED}:
                continue
            self._load_one(record)
        return list(self.records.values())

    def _resolve_order(self) -> list[str]:
        enabled = {pid for pid, rec in self.records.items() if rec.enabled}
        order: list[str] = []
        visiting: set[str] = set()
        done: set[str] = set()

        def visit(plugin_id: str, chain: tuple[str, ...]) -> None:
            if plugin_id in done:
                return
            if plugin_id in visiting:
                cycle = " -> ".join(chain + (plugin_id,))
                for member in chain + (plugin_id,):
                    rec = self.records.get(member)
                    if rec and rec.state != PluginState.DISABLED:
                        rec.state = PluginState.FAILED
                        rec.error = f"依赖成环：{cycle}"
                raise PluginDependencyError(f"依赖成环：{cycle}")
            rec = self.records[plugin_id]
            if not rec.enabled:
                raise PluginDependencyError(f"依赖插件被禁用：{plugin_id}")
            visiting.add(plugin_id)
            for dep in rec.manifest.requires:
                if dep not in self.records:
                    rec.state = PluginState.FAILED
                    rec.error = f"缺少依赖插件：{dep}"
                    raise PluginDependencyError(rec.error)
                visit(dep, chain + (plugin_id,))
            visiting.remove(plugin_id)
            done.add(plugin_id)
            order.append(plugin_id)

        for plugin_id in sorted(enabled):
            try:
                visit(plugin_id, ())
            except PluginDependencyError as exc:
                self.logger.error("插件依赖解析失败 %s: %s", plugin_id, exc)
        return order

    def _load_one(self, record: PluginRecord) -> None:
        manifest = record.manifest
        try:
            module = self._import_module(Path(record.path), manifest)
            plugin = self._plugin_from_module(module, manifest)
            context = PluginContext(
                plugin_id=manifest.id,
                manifest=manifest,
                config=self.config.plugin(manifest.id),
                paths=self.runtime.paths,
                memory=self.runtime.memory,
                accounts=self.runtime.accounts,
                leases=self.runtime.leases,
                events=self.events,
                hooks=self.hooks,
                tools=self.tools,
                schedules=self.schedules,
                logger=logging.getLogger(f"jinyv.plugin.{manifest.id}"),
                runtime={
                    "root": str(self.root),
                    "version": __version__,
                    "host": "jinyv-memory",
                },
            )
            record.plugin = plugin
            record.context = context
            record.state = PluginState.LOADED
            setup = getattr(plugin, "setup", None)
            if callable(setup):
                setup(context)
            record.state = PluginState.DEGRADED if context.degraded_reason else PluginState.SETUP
            if context.degraded_reason:
                self.logger.warning("插件降级 %s: %s", manifest.id, context.degraded_reason)
        except Exception as exc:  # noqa: BLE001 单插件失败不拖垮宿主
            self._rollback(manifest.id)
            record.state = PluginState.FAILED
            record.error = f"{type(exc).__name__}: {exc}"
            record.health = Health.failed(record.error)
            self.logger.exception("插件加载失败 %s", manifest.id)

    def _import_module(self, plugin_dir: Path, manifest: PluginManifest) -> Any:
        entry = plugin_dir / manifest.entry
        if not entry.exists():
            raise PluginLoadError(f"插件入口不存在：{entry}")
        module_name = "jinyv_plugin_" + _PLUGIN_NAME.sub("_", manifest.id)
        kwargs: dict[str, Any] = {}
        if entry.name == "__init__.py":
            kwargs["submodule_search_locations"] = [str(plugin_dir)]
        spec = importlib.util.spec_from_file_location(module_name, entry, **kwargs)
        if spec is None or spec.loader is None:
            raise PluginLoadError(f"无法为插件创建导入规格：{entry}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _plugin_from_module(module: Any, manifest: PluginManifest) -> Any:
        candidate = getattr(module, "PLUGIN", None)
        if candidate is None and callable(getattr(module, "create_plugin", None)):
            candidate = module.create_plugin()
        if candidate is None and callable(getattr(module, "setup", None)):
            candidate = _ModulePlugin(module, manifest)
        if candidate is None:
            raise PluginLoadError(
                f"插件 {manifest.id} 没有暴露 PLUGIN、create_plugin() 或 setup()"
            )
        if not callable(getattr(candidate, "setup", None)):
            raise PluginLoadError(f"插件 {manifest.id} 的对象缺少 setup(ctx)")
        return candidate

    # ------------------------------------------------------------ 启动 / 停止
    def start_all(self, *, start_scheduler: bool = True) -> None:
        for plugin_id in self._order:
            record = self.records[plugin_id]
            if record.state not in {PluginState.SETUP, PluginState.DEGRADED, PluginState.LOADED}:
                continue
            try:
                start = getattr(record.plugin, "start", None)
                if callable(start):
                    start(record.context)
                if record.context and record.context.degraded_reason:
                    record.state = PluginState.DEGRADED
                    record.health = Health.degraded(record.context.degraded_reason)
                else:
                    record.state = PluginState.RUNNING
                    record.health = Health.ok()
            except Exception as exc:  # noqa: BLE001
                self._rollback(plugin_id)
                record.state = PluginState.FAILED
                record.error = f"start: {type(exc).__name__}: {exc}"
                record.health = Health.failed(record.error)
                self.logger.exception("插件启动失败 %s", plugin_id)
        if start_scheduler:
            self.scheduler.start()

    def stop_all(self) -> None:
        self.scheduler.stop()
        for plugin_id in reversed(self._order):
            record = self.records.get(plugin_id)
            if record is None or record.plugin is None:
                continue
            if record.state in {PluginState.DISABLED, PluginState.FAILED}:
                continue
            try:
                stop = getattr(record.plugin, "stop", None)
                if callable(stop):
                    stop(record.context)
            except Exception:  # noqa: BLE001
                self.logger.exception("插件停止失败 %s", plugin_id)
            finally:
                self._rollback(plugin_id)
                record.state = PluginState.STOPPED

    def _rollback(self, plugin_id: str) -> None:
        self.tools.remove_plugin(plugin_id)
        self.hooks.remove_plugin(plugin_id)
        self.events.remove_plugin(plugin_id)
        self.schedules.remove_plugin(plugin_id)

    # ------------------------------------------------------------ 查询 / 健康
    def get(self, plugin_id: str) -> PluginRecord | None:
        return self.records.get(plugin_id)

    def health(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for plugin_id in self._order or list(self.records):
            record = self.records[plugin_id]
            health = record.health
            if record.state in {PluginState.RUNNING, PluginState.DEGRADED}:
                checker = getattr(record.plugin, "health", None)
                if callable(checker):
                    try:
                        result = checker(record.context)
                        if isinstance(result, Health):
                            health = result
                    except Exception as exc:  # noqa: BLE001
                        health = Health.failed(f"{type(exc).__name__}: {exc}")
            if record.state == PluginState.DEGRADED and health.state == "ok":
                health = Health.degraded(record.context.degraded_reason if record.context else "")
            if record.state == PluginState.FAILED and health.state == "ok":
                health = Health.failed(record.error)
            rows.append({
                "id": plugin_id,
                "name": record.manifest.name,
                "version": record.manifest.version,
                "kind": record.manifest.kind,
                "state": record.state.value,
                "enabled": record.enabled,
                "path": record.path,
                "health": health.state,
                "reason": health.reason,
                "details": dict(health.details),
                "error": record.error,
            })
        for message in self._discovery_errors:
            rows.append({
                "id": "?",
                "name": "发现失败",
                "version": "",
                "kind": "",
                "state": PluginState.FAILED.value,
                "enabled": False,
                "path": "",
                "health": "failed",
                "reason": message,
                "details": {},
                "error": message,
            })
        return rows
