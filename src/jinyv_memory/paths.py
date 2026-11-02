# -*- coding: utf-8 -*-
"""目录布局：配置、密钥、运行数据按插件隔离。

仓库内只放示例配置；真实密钥与运行数据都落在 data_dir / secrets_dir，
两者默认在 .gitignore 中。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    root: Path
    config_dir: Path
    data_dir: Path
    secrets_dir: Path

    @classmethod
    def resolve(cls, root: str | Path, data_dir: str | Path | None = None) -> "Paths":
        root_path = Path(root).resolve()
        data = Path(data_dir).resolve() if data_dir else Path(
            os.environ.get("JINYV_DATA_DIR") or root_path / "var"
        ).resolve()
        secrets = Path(os.environ.get("JINYV_SECRETS_DIR") or root_path / "secrets").resolve()
        return cls(
            root=root_path,
            config_dir=root_path / "config",
            data_dir=data,
            secrets_dir=secrets,
        )

    def ensure(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.secrets_dir.mkdir(parents=True, exist_ok=True)

    def plugin_data(self, plugin_id: str) -> Path:
        path = self.data_dir / "plugins" / plugin_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def plugin_secrets(self, plugin_id: str) -> Path:
        path = self.secrets_dir / "plugins" / plugin_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def core_data(self) -> Path:
        path = self.data_dir / "core"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def plugin_config_file(self, plugin_id: str) -> Path:
        return self.config_dir / "plugins" / f"{plugin_id}.toml"
