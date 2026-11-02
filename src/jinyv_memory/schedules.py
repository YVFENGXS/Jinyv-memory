# -*- coding: utf-8 -*-
"""定时任务注册与调度。

跨进程单例由 core 的 LeaseManager 保证：同一 job_id + 同一时间窗口只允许
一个执行者领到租约。调度线程只负责到点触发，业务幂等仍由任务自身负责。
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any

from .contract import ScheduleSpec
from .errors import PluginError

_INTERVAL_EPS = 0.05


@dataclass
class _State:
    spec: ScheduleSpec
    next_run: float = 0.0
    running: bool = False
    runs: int = 0
    failures: int = 0
    last_error: str = ""


class ScheduleRegistry:
    def __init__(self, logger: logging.Logger | None = None):
        self._logger = logger or logging.getLogger("jinyv.schedules")
        self._specs: dict[str, ScheduleSpec] = {}

    def register(self, spec: ScheduleSpec, plugin_id: str = "") -> None:
        job_id = str(spec.job_id or "").strip()
        if not job_id:
            raise PluginError("定时任务缺少 job_id")
        if job_id in self._specs:
            owner = self._specs[job_id].plugin_id
            raise PluginError(f"定时任务冲突：{job_id} 已被插件 {owner or 'core'} 注册")
        interval = float(spec.interval_seconds)
        if interval < 1.0:
            raise PluginError(f"定时任务 {job_id} 的 interval_seconds 不能小于 1")
        if not callable(spec.handler):
            raise PluginError(f"定时任务 {job_id} 的 handler 不可调用")
        self._specs[job_id] = ScheduleSpec(
            job_id=job_id,
            interval_seconds=interval,
            handler=spec.handler,
            plugin_id=plugin_id or spec.plugin_id,
            run_on_start=bool(spec.run_on_start),
            lease_ttl=float(spec.lease_ttl),
        )

    def remove_plugin(self, plugin_id: str) -> int:
        job_ids = [job for job, spec in self._specs.items() if spec.plugin_id == plugin_id]
        for job in job_ids:
            self._specs.pop(job, None)
        return len(job_ids)

    def get(self, job_id: str) -> ScheduleSpec | None:
        return self._specs.get(job_id)

    def list(self) -> list[ScheduleSpec]:
        return sorted(self._specs.values(), key=lambda spec: spec.job_id)


class Scheduler:
    """轻量调度器：默认每秒 tick，测试可直接调用 tick(now)。"""

    def __init__(self, registry: ScheduleRegistry, leases: Any,
                 logger: logging.Logger | None = None, tick_seconds: float = 1.0):
        self._registry = registry
        self._leases = leases
        self._logger = logger or logging.getLogger("jinyv.scheduler")
        self._tick_seconds = max(0.2, float(tick_seconds))
        self._states: dict[str, _State] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._owner = f"{socket.gethostname()}:{os.getpid()}:scheduler"
        self._started_at = time.time()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        now = time.time()
        with self._lock:
            for spec in self._registry.list():
                state = self._states.setdefault(spec.job_id, _State(spec))
                state.spec = spec
                state.next_run = now + (0.0 if spec.run_on_start else spec.interval_seconds)
        self._thread = threading.Thread(target=self._loop, name="jinyv-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self._tick_seconds):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 调度器自身不能死
                self._logger.exception("调度 tick 失败")

    def tick(self, now: float | None = None) -> list[str]:
        current = time.time() if now is None else float(now)
        executed: list[str] = []
        with self._lock:
            for spec in self._registry.list():
                state = self._states.setdefault(spec.job_id, _State(spec))
                state.spec = spec
                if state.running or current + _INTERVAL_EPS < state.next_run:
                    continue
                run_key = str(int(current // spec.interval_seconds))
                if not self._leases.claim_job(spec.job_id, run_key, self._owner,
                                              ttl=max(spec.lease_ttl, spec.interval_seconds)):
                    # 另一个进程已经领走这个窗口；直接推进到下一个窗口
                    state.next_run = current + spec.interval_seconds
                    continue
                state.running = True
                state.runs += 1
                state.next_run = current + spec.interval_seconds
                executed.append(spec.job_id)
                threading.Thread(
                    target=self._run, args=(state, run_key, current),
                    name=f"jinyv-job-{spec.job_id}", daemon=True,
                ).start()
        return executed

    def _run(self, state: _State, run_key: str, started: float) -> None:
        ok = False
        error = ""
        try:
            state.spec.handler()
            ok = True
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            state.failures += 1
            state.last_error = error
            self._logger.exception("定时任务失败 %s", state.spec.job_id)
        finally:
            try:
                self._leases.finish_job(state.spec.job_id, run_key, ok, error)
            except Exception:  # noqa: BLE001
                self._logger.exception("写入任务结果失败 %s", state.spec.job_id)
            with self._lock:
                state.running = False

    def stats(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "job_id": state.spec.job_id,
                    "plugin_id": state.spec.plugin_id,
                    "interval_seconds": state.spec.interval_seconds,
                    "runs": state.runs,
                    "failures": state.failures,
                    "last_error": state.last_error,
                    "next_run": state.next_run,
                }
                for state in self._states.values()
            ]
