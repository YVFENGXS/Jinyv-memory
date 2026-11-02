# -*- coding: utf-8 -*-
"""Jinyv-memory 运行时入口：装配核心 → 发现并加载插件 → 启动 → 常驻。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .config import Config
from .core.runtime import build_core
from .loader import PluginLoader
from .paths import Paths


class JinyvRuntime:
    """一个进程 = 一套核心 + 一套插件。多账号部署时起多个进程。"""

    def __init__(self, root: str | Path, *, data_dir: str | Path | None = None,
                 plugin_dirs: list[str | Path] | None = None,
                 mock_llm: bool | None = None,
                 logger: logging.Logger | None = None):
        self.logger = logger or logging.getLogger("jinyv")
        self.config = Config.load(root, data_dir)
        self.paths: Paths = self.config.paths
        self.paths.ensure()
        self.core = build_core(self.config, self.paths, mock_llm=mock_llm)
        self.loader = PluginLoader(
            root=self.paths.root,
            config=self.config,
            runtime=self.core,
            logger=self.logger,
        )
        self._plugin_dirs = plugin_dirs or []
        self.started = False

    def load_plugins(self) -> None:
        self.loader.discover(self._plugin_dirs or [self.paths.root / "plugins"])
        self.loader.load_all()

    def start(self) -> None:
        if self.started:
            return
        self.core.memory.open_session(self.core.accounts.default_key)
        self.loader.start_all()
        self.started = True

    def shutdown(self) -> None:
        if not self.started:
            return
        self.loader.stop_all()
        self.core.shutdown()
        self.started = False

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "core": self.core.health(),
            "plugins": self.loader.health(),
        }


def create_runtime(root: str | Path, **kwargs: Any) -> JinyvRuntime:
    return JinyvRuntime(root, **kwargs)
