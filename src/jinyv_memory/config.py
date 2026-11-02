# -*- coding: utf-8 -*-
"""分层配置：core.toml + accounts.toml + plugins/<id>.toml + 环境变量覆盖。

环境变量约定：
- JINYV_DATA_DIR / JINYV_SECRETS_DIR
- JINYV_CORE_<KEY>                     覆盖 [core].<key>
- JINYV_PLUGIN_<ID>_<KEY>              覆盖插件配置的顶层 key

密钥不写进 TOML：优先读显式环境变量，其次读 secrets/plugins/<id>/<name>。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 兼容
    import tomli as tomllib  # type: ignore[no-redef]

from .errors import ConfigError
from .paths import Paths


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML 解析失败 {path}: {exc}") from exc


def _scalar(value: str) -> Any:
    text = value.strip()
    if text.lower() in {"1", "true", "yes", "on"}:
        return True
    if text.lower() in {"0", "false", "no", "off"}:
        return False
    for caster in (int, float):
        try:
            return caster(text)
        except ValueError:
            pass
    return text


def _plugin_env_prefix(plugin_id: str) -> str:
    return "JINYV_PLUGIN_" + plugin_id.upper().replace("-", "_").replace(".", "_") + "_"


@dataclass
class Config:
    root: Path
    paths: Paths
    core: dict[str, Any] = field(default_factory=dict)
    accounts: dict[str, Any] = field(default_factory=dict)
    plugins: dict[str, dict[str, Any]] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=os.environ)

    @classmethod
    def load(cls, root: str | Path, data_dir: str | Path | None = None,
             env: Mapping[str, str] | None = None) -> "Config":
        paths = Paths.resolve(root, data_dir)
        env_map = dict(os.environ if env is None else env)
        core = _read_toml(paths.config_dir / "core.toml")
        # 允许把核心参数写在 [core] 段；[llm] / [persona] 等段继续并列保留
        if isinstance(core.get("core"), dict):
            nested = dict(core["core"])
            for key, value in core.items():
                if key != "core":
                    nested.setdefault(key, value)
            core = nested
        accounts = _read_toml(paths.config_dir / "accounts.toml")
        plugins: dict[str, dict[str, Any]] = {}
        plugin_dir = paths.config_dir / "plugins"
        if plugin_dir.exists():
            for path in sorted(plugin_dir.glob("*.toml")):
                plugins[path.stem] = _read_toml(path)
        # 环境变量覆盖 core 顶层键
        for key in list(env_map):
            if key.startswith("JINYV_CORE_"):
                core[key[len("JINYV_CORE_"):].lower()] = _scalar(env_map[key])
        # 环境变量覆盖插件顶层键
        for plugin_id, raw in plugins.items():
            prefix = _plugin_env_prefix(plugin_id)
            for key in list(env_map):
                if key.startswith(prefix):
                    raw[key[len(prefix):].lower()] = _scalar(env_map[key])
        return cls(root=paths.root, paths=paths, core=core, accounts=accounts,
                   plugins=plugins, env=env_map)

    def core_get(self, key: str, default: Any = None) -> Any:
        return self.core.get(key, default)

    def plugin_raw(self, plugin_id: str) -> dict[str, Any]:
        return self.plugins.get(plugin_id, {})

    def plugin(self, plugin_id: str) -> "PluginConfig":
        return PluginConfig(plugin_id=plugin_id, raw=self.plugin_raw(plugin_id),
                            paths=self.paths, env=self.env)


@dataclass(frozen=True)
class PluginConfig(Mapping[str, Any]):
    plugin_id: str
    raw: Mapping[str, Any]
    paths: Paths
    env: Mapping[str, str]

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.raw)

    def __len__(self) -> int:
        return len(self.raw)

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    def require(self, key: str) -> Any:
        if key not in self.raw:
            raise ConfigError(f"插件 {self.plugin_id} 缺少配置项 {key}")
        return self.raw[key]

    def section(self, name: str) -> Mapping[str, Any]:
        value = self.raw.get(name)
        return value if isinstance(value, dict) else {}

    @property
    def data_dir(self) -> Path:
        return self.paths.plugin_data(self.plugin_id)

    @property
    def config_file(self) -> Path:
        return self.paths.plugin_config_file(self.plugin_id)

    def secret(self, name: str, env_var: str | None = None) -> str:
        """读密钥：环境变量优先，其次 secrets/plugins/<id>/<name> 文件。"""
        candidate = env_var or name.upper()
        value = self.env.get(candidate)
        if value:
            return str(value).strip()
        path = self.paths.plugin_secrets(self.plugin_id) / name
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        return ""
