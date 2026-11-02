# -*- coding: utf-8 -*-
"""DSH 本地记忆仓的只读适配。

只吸收行为级布局（session 目录 + turns.jsonl/cards.json/facts.json），
不把对方的表结构搬进主库；导入时映射到现有 event_cards / durable_facts。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class DshSession:
    session_id: str
    path: Path
    turns: list[dict[str, Any]] = field(default_factory=list)
    cards: list[dict[str, Any]] = field(default_factory=list)
    facts: list[dict[str, Any]] = field(default_factory=list)


def memory_root(home: str | Path | None = None) -> Path:
    base = Path(home) if home else Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh")
    return base / "jinyu-memory" / "sessions"


def session_dir_name(session_id: str) -> str:
    """与 DSH 插件一致的目录映射：安全字符截断，必要时追加内容哈希。"""
    raw = str(session_id or "session")
    safe = "".join(ch if (ch.isalnum() or ch in "_.-") else "_" for ch in raw)[:64]
    if safe == raw and len(raw) <= 64:
        return safe
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{digest}"


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    except OSError:
        return []
    return rows


def list_sessions(home: str | Path | None = None) -> list[str]:
    root = memory_root(home)
    if not root.exists():
        return []
    return sorted(path.name for path in root.iterdir() if path.is_dir())


def read_session(path: str | Path) -> DshSession:
    directory = Path(path)
    if not directory.is_dir():
        raise FileNotFoundError(f"DSH 会话目录不存在：{directory}")
    session_id = directory.name
    return DshSession(
        session_id=session_id,
        path=directory,
        turns=_read_jsonl(directory / "turns.jsonl"),
        cards=_read_json(directory / "cards.json", []),
        facts=_read_json(directory / "facts.json", []),
    )


def find_session(session_id: str, home: str | Path | None = None) -> DshSession:
    root = memory_root(home)
    direct = root / session_dir_name(session_id)
    if direct.is_dir():
        return read_session(direct)
    for name in list_sessions(home):
        candidate = root / name
        for turn in _read_jsonl(candidate / "turns.jsonl"):
            if str(turn.get("session") or "") == session_id:
                return read_session(candidate)
    raise FileNotFoundError(f"找不到 DSH 会话：{session_id}")
