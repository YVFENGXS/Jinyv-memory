# -*- coding: utf-8 -*-
"""核心装配：租约库 + 账号车道 + 记忆引擎 + LLM。

没有配置密钥时使用 MissingLLM 占位：记忆读写照常，只有真正生成回复才会报错，
与旧 host.py 的降级行为一致。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..accounts import AccountRegistry
from ..config import Config
from ..paths import Paths
from .engine import llm
from .engine.persona import PERSONA
from .engine.rp_skeleton import DB, RPContextMiddleware
from .lease import LeaseManager
from .memory import MemoryAPI
from .mock_llm import MockBack, MockFront


class MissingLLM:
    def __init__(self, role: str):
        self.role = role

    def _fail(self):
        raise RuntimeError(
            f"缺少 {self.role} 模型密钥：请设置 RP_FRONT_KEY / RP_BACK_KEY，"
            "或在 config/core.toml 里设置 mock_llm = true"
        )

    def chat(self, *args, **kwargs):
        self._fail()

    def chat_tools(self, *args, **kwargs):
        self._fail()


@dataclass
class CoreRuntime:
    config: Config
    paths: Paths
    db: DB
    middleware: RPContextMiddleware
    memory: MemoryAPI
    leases: LeaseManager
    accounts: AccountRegistry
    persona: dict

    def health(self) -> dict:
        return {
            "db": str(self.db.path),
            "runtime_db": str(self.leases.path),
            "accounts": [lane.key for lane in self.accounts.list()],
            "character": self.persona.get("name", ""),
            "mock_llm": isinstance(self.middleware.front, MockFront),
            "unexpected_errors": int(getattr(self.middleware, "_unexpected_errors", 0)),
        }

    def shutdown(self, timeout: float | None = None) -> None:
        self.memory.shutdown(timeout=timeout)


def _build_llm(role: str, mock: bool):
    if mock:
        return MockFront("瑾瑜") if role == "front" else MockBack()
    try:
        return llm.from_env(role)
    except ValueError:
        return MissingLLM(role)


def _persona(core: dict) -> dict:
    persona = dict(PERSONA)
    override = core.get("persona")
    if isinstance(override, dict):
        for key in ("name", "core", "style"):
            if override.get(key):
                persona[key] = str(override[key])
        for key in ("taboo_words", "forbidden_punct"):
            if isinstance(override.get(key), list):
                persona[key] = list(override[key])
    return persona


def build_core(config: Config, paths: Paths, *, mock_llm: bool | None = None) -> CoreRuntime:
    paths.ensure()
    core = config.core
    mock = bool(core.get("mock_llm", False))
    if mock_llm is not None:
        mock = bool(mock_llm)
    if str(config.env.get("JINYV_LLM_MOCK") or "").lower() in {"1", "true", "yes", "on"}:
        mock = True

    db_path = core.get("db_path")
    if db_path:
        db_file = Path(str(db_path))
        if not db_file.is_absolute():
            db_file = paths.root / db_file
    else:
        db_file = paths.data_dir / "rp_memory.db"
    db_file.parent.mkdir(parents=True, exist_ok=True)

    db = DB(str(db_file))
    front = _build_llm("front", mock)
    back = _build_llm("back", mock)
    middleware = RPContextMiddleware(
        front,
        back,
        db,
        keep_recent=int(core.get("keep_recent", 2)),
        archive_batch=int(core.get("archive_batch", 2)),
        max_tokens=int(core.get("max_tokens", 64000)),
        output_reserve=int(core.get("output_reserve", 4096)),
        profile=str(core.get("profile", "cl100k")),
        recall_limit=int(core.get("recall_limit", 4)),
        guard_retry=int(core.get("guard_retry", 1)),
        shutdown_timeout=int(core.get("shutdown_timeout", 60)),
        social=bool(core.get("social", False)),
    )
    leases = LeaseManager(paths.core_data() / "runtime.db")
    accounts = AccountRegistry.from_config(config.accounts)
    persona = _persona(core)
    memory = MemoryAPI(db, middleware, accounts, leases, persona=persona)
    return CoreRuntime(
        config=config,
        paths=paths,
        db=db,
        middleware=middleware,
        memory=memory,
        leases=leases,
        accounts=accounts,
        persona=persona,
    )
