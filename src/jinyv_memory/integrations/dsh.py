# -*- coding: utf-8 -*-
"""DSH 特殊接入桥接（不属于 Python 插件契约）。

DSH 侧是 JS/cordis 插件，按它自己的宿主机制运行；核心侧只做四件事：
1. 检测：DSH 插件包、本地记忆仓、第三方分镜 CLI。
2. 启停：可选托管命令（默认不托管，避免替用户启动第三方进程）。
3. 导入：把 DSH 的 cards/facts 映射到核心表，按内容幂等。
4. 桥接：核心 → DSH 的导出，带 journal、冲突记录与确定性裁决。

同步语义：
- 导入：DSH 新条目进入核心；同一 id 内容变化视为冲突，默认 core-wins（保留核心，
  记录冲突），不静默覆盖。
- 导出：核心条目写入 DSH；用 `.jinyv-bridge.json` 记录 core/dsh 双端内容哈希。
  只有一端变化时同步该端；两端同时变化时按 `conflict_policy` 裁决（默认 core-wins）。
- 幂等：核心侧靠 event_cards.idempotency_key / durable_facts UNIQUE；桥接侧靠
  journal 的双端哈希与稳定 id，重复 import/export 不产生副本。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from ..core.memory import MemoryAPI
from ..paths import Paths
from . import dsh_store

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "dsh_home": "",
    "plugin_dir": "",
    "storyboard_dir": "",
    "account": "desktop",
    "auto_start": False,
    "start_command": [],
    "stop_timeout_seconds": 5.0,
    "conflict_policy": "core-wins",
    "sync_mode": "both",
}

JOURNAL_NAME = ".jinyv-bridge.json"
JOURNAL_VERSION = 1


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _norm_card(card: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "summary": str(card.get("summary") or "").strip(),
        "hooks": [str(item) for item in (card.get("hooks") or card.get("entity_hooks") or [])],
        "rel_hooks": [str(item) for item in (card.get("relHooks") or card.get("rel_hooks") or [])],
        "importance": round(float(card.get("importance") or 0.5), 3),
        "boundary": str(card.get("boundary") or card.get("knowledge_boundary") or "both"),
        "turn_start": int(card.get("turnStart") or card.get("turn_start") or 0),
        "turn_end": int(card.get("turnEnd") or card.get("turn_end") or 0),
        "mentions": int(card.get("mentions") or 0),
        "verified": int(card.get("verified") or 0),
    }


def _norm_fact(fact: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "text": str(fact.get("text") or fact.get("fact") or "").strip(),
        "tags": [str(item) for item in (fact.get("tags") or [])],
        "kind": str(fact.get("kind") or fact.get("category") or ""),
        "importance": round(float(fact.get("importance") or 0.5), 3),
        "turn": int(fact.get("turn") or fact.get("turn_end") or 0),
    }


def _card_sig(card: Mapping[str, Any]) -> str:
    return _digest(_norm_card(card))


def _fact_sig(fact: Mapping[str, Any]) -> str:
    return _digest(_norm_fact(fact))


def _iso(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    text = text.replace(" ", "T")
    if not text.endswith("Z") and "+" not in text:
        text += "Z"
    return text


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def _write_json(path: Path, value: Any) -> None:
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _empty_journal() -> dict[str, Any]:
    return {"version": JOURNAL_VERSION, "imported": {}, "exported": {}, "conflicts": []}


def _load_journal(session: dsh_store.DshSession) -> dict[str, Any]:
    data = _read_json(session.path / JOURNAL_NAME, None)
    if not isinstance(data, dict):
        return _empty_journal()
    for key, default in (("imported", {}), ("exported", {}), ("conflicts", [])):
        data.setdefault(key, default)
    data["version"] = JOURNAL_VERSION
    return data


@dataclass
class DshBridge:
    config: dict[str, Any]
    memory: MemoryAPI
    paths: Paths
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("jinyv.dsh"))
    _process: subprocess.Popen | None = None
    _log_file: Any = None

    @classmethod
    def from_file(cls, path: str | Path, memory: MemoryAPI, paths: Paths,
                  logger: logging.Logger | None = None,
                  overrides: Mapping[str, Any] | None = None) -> "DshBridge":
        raw: dict[str, Any] = {}
        file_path = Path(path)
        if file_path.exists():
            try:
                with file_path.open("rb") as fh:
                    raw = tomllib.load(fh)
            except (tomllib.TOMLDecodeError, OSError):
                raw = {}
        config = dict(DEFAULTS)
        config.update(raw)
        if overrides:
            config.update(dict(overrides))
        return cls(config=config, memory=memory, paths=paths,
                   logger=logger or logging.getLogger("jinyv.dsh"))

    # ------------------------------------------------------------ 路径
    @property
    def home(self) -> Path:
        return Path(str(self.config.get("dsh_home") or os.environ.get("DSH_HOME")
                        or Path.home() / ".dsh")).expanduser()

    @property
    def sessions_root(self) -> Path:
        return dsh_store.memory_root(self.home)

    @property
    def plugin_dir(self) -> Path:
        configured = str(self.config.get("plugin_dir") or "")
        if configured:
            path = Path(configured).expanduser()
            if not path.is_absolute():
                path = self.paths.root / path
            return path
        return self.home / "plugins" / "jinyu-memory-dsh"

    @property
    def storyboard_dir(self) -> Path:
        configured = str(self.config.get("storyboard_dir") or os.environ.get("RP_STORYBOARD_DIR")
                         or "")
        if configured:
            path = Path(configured).expanduser()
            if not path.is_absolute():
                path = self.paths.root / path
            return path
        return self.paths.root / "var" / "vendor" / "dsh-video-storyboard"

    @property
    def account(self) -> str:
        return str(self.config.get("account") or "desktop")

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    # ------------------------------------------------------------ 检测 / 启停
    def detect(self) -> dict[str, Any]:
        package = self.plugin_dir / "package.json"
        dsh_plugin = False
        if package.exists():
            try:
                data = json.loads(package.read_text(encoding="utf-8"))
                dsh_plugin = str(data.get("name") or "") == "jinyu-memory-dsh"
            except (OSError, ValueError):
                dsh_plugin = False
        storyboard = (self.storyboard_dir / "bin" / "storyboard.mjs").exists()
        sessions = len(dsh_store.list_sessions(self.home))
        return {
            "dsh_home": str(self.home),
            "sessions_root": str(self.sessions_root),
            "plugin_dir": str(self.plugin_dir),
            "dsh_plugin": dsh_plugin,
            "storyboard_dir": str(self.storyboard_dir),
            "storyboard": storyboard,
            "sessions": sessions,
            "available": bool(dsh_plugin or storyboard or sessions),
        }

    def status(self) -> dict[str, Any]:
        running = bool(self._process and self._process.poll() is None)
        return {
            "enabled": self.enabled,
            "managed": running,
            "pid": self._process.pid if running else None,
            "account": self.account,
            "conflict_policy": str(self.config.get("conflict_policy") or "core-wins"),
            **self.detect(),
        }

    def start(self) -> bool:
        if not self.enabled:
            return False
        if self._process and self._process.poll() is None:
            return True
        raw_command = self.config.get("start_command") or []
        if isinstance(raw_command, str):
            command = shlex.split(raw_command)
        else:
            command = [str(item) for item in raw_command]
        if not command:
            self.logger.info("DSH 未配置 start_command，桥接按需使用，不托管进程")
            return False
        log_dir = self.paths.data_dir / "integrations" / "dsh"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log_file = (log_dir / "bridge.log").open("a", encoding="utf-8")
        cwd = self.plugin_dir if self.plugin_dir.exists() else self.home
        self._process = subprocess.Popen(
            command, cwd=str(cwd), stdout=self._log_file, stderr=subprocess.STDOUT,
            shell=False,
        )
        self.logger.info("DSH 托管进程已启动 pid=%s command=%s", self._process.pid, command)
        return True

    def stop(self) -> bool:
        process = self._process
        if process is None or process.poll() is not None:
            self._close_log()
            self._process = None
            return False
        timeout = float(self.config.get("stop_timeout_seconds") or 5.0)
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)
        self.logger.info("DSH 托管进程已停止 pid=%s", process.pid)
        self._process = None
        self._close_log()
        return True

    def _close_log(self) -> None:
        if self._log_file:
            try:
                self._log_file.close()
            finally:
                self._log_file = None

    # ------------------------------------------------------------ 导入
    def _session(self, ref: str) -> dsh_store.DshSession:
        return dsh_store.find_session(ref, self.home)

    def import_session(self, ref: str, account: str | None = None) -> dict[str, Any]:
        session = self._session(ref)
        journal = _load_journal(session)
        cards: list[dict[str, Any]] = []
        facts: list[dict[str, Any]] = []
        skipped = 0
        conflicts: list[dict[str, Any]] = []
        for card in session.cards:
            card_id = str(card.get("id") or "")
            if not card_id:
                continue
            key = f"card:{card_id}"
            signature = _card_sig(card)
            previous = journal["imported"].get(key)
            if previous == signature:
                skipped += 1
                continue
            if previous is not None:
                conflicts.append({
                    "kind": "card", "id": card_id,
                    "reason": "DSH 条目在导入后发生变化；按 core-wins 保留核心版本",
                    "previous": previous, "current": signature,
                })
                continue
            cards.append(card)
            journal["imported"][key] = signature
        for fact in session.facts:
            fact_id = str(fact.get("id") or "")
            if not fact_id:
                continue
            key = f"fact:{fact_id}"
            signature = _fact_sig(fact)
            previous = journal["imported"].get(key)
            if previous == signature:
                skipped += 1
                continue
            if previous is not None:
                conflicts.append({
                    "kind": "fact", "id": fact_id,
                    "reason": "DSH 条目在导入后发生变化；按 core-wins 保留核心版本",
                    "previous": previous, "current": signature,
                })
                continue
            facts.append(fact)
            journal["imported"][key] = signature
        result = self.memory.import_external(account or self.account, cards, facts)
        if conflicts:
            journal["conflicts"].extend(conflicts)
            journal["conflicts"] = journal["conflicts"][-200:]
        _write_json(session.path / JOURNAL_NAME, journal)
        return {
            "source_session": session.session_id,
            "journal": str(session.path / JOURNAL_NAME),
            "skipped": skipped,
            "conflicts": len(conflicts),
            **result,
        }

    # ------------------------------------------------------------ 导出
    def _core_card_to_dsh(self, card: Mapping[str, Any]) -> dict[str, Any]:
        core_id = str(card.get("event_id") or "")
        return {
            "id": f"core-{core_id}",
            "summary": str(card.get("summary") or ""),
            "hooks": [str(item) for item in (card.get("entity_hooks") or [])],
            "relHooks": [str(item) for item in (card.get("rel_hooks") or [])],
            "importance": float(card.get("importance") or 0.5),
            "boundary": str(card.get("knowledge_boundary") or "both"),
            "turnStart": int(card.get("turn_start") or 0),
            "turnEnd": int(card.get("turn_end") or 0),
            "createdAt": _iso(card.get("created_at")),
            "mentions": int(card.get("mentions") or 0),
            "verified": int(card.get("verified") or 0),
            "jinyv_core_id": core_id,
        }

    def _core_fact_to_dsh(self, fact: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": f"core-F{fact.get('id')}",
            "text": str(fact.get("text") or ""),
            "tags": [str(item) for item in (fact.get("tags") or [])],
            "kind": str(fact.get("category") or ""),
            "importance": float(fact.get("importance") or 0.5),
            "turn": int(fact.get("turn_end") or 0),
            "createdAt": _iso(fact.get("created_at")),
            "jinyv_core_id": str(fact.get("id") or ""),
        }

    def export_session(self, ref: str, account: str | None = None,
                       limit: int = 50) -> dict[str, Any]:
        session = self._session(ref)
        journal = _load_journal(session)
        snapshot = self.memory.export_memory(account or self.account, limit)
        cards = [dict(item) for item in session.cards]
        facts = [dict(item) for item in session.facts]
        cards_by_id = {str(item.get("id") or ""): item for item in cards}
        facts_by_text = {str(item.get("text") or ""): item for item in facts}
        stats = {"cards_new": 0, "cards_updated": 0, "facts_new": 0, "facts_updated": 0,
                 "skipped": 0, "conflicts": 0}
        conflicts: list[dict[str, Any]] = []

        for core_card in snapshot["cards"]:
            core_id = str(core_card.get("event_id") or "")
            if not core_id or core_id in cards_by_id:
                continue                    # 来自 DSH 的回流卡，避免回声
            mapped = self._core_card_to_dsh(core_card)
            dsh_id = mapped["id"]
            core_sig = _card_sig(mapped)
            entry_key = f"card:{core_id}"
            entry = journal["exported"].get(entry_key)
            target_id = str((entry or {}).get("dsh_id") or dsh_id)
            target = cards_by_id.get(target_id)
            if target is None:
                cards.append(mapped)
                cards_by_id[dsh_id] = mapped
                journal["exported"][entry_key] = {
                    "dsh_id": dsh_id, "core_sig": core_sig, "dsh_sig": core_sig,
                }
                stats["cards_new"] += 1
                continue
            dsh_sig = _card_sig(target)
            if entry is None:
                # 已有同名导出卡但缺 journal：以核心为准重建映射
                merged = dict(target)
                merged.update({key: value for key, value in mapped.items() if key != "id"})
                merged["id"] = target.get("id", dsh_id)
                self._replace_by_id(cards, merged)
                journal["exported"][entry_key] = {
                    "dsh_id": merged["id"], "core_sig": core_sig, "dsh_sig": core_sig,
                }
                stats["cards_updated"] += 1
                continue
            if dsh_sig != entry.get("dsh_sig") and core_sig != entry.get("core_sig"):
                conflicts.append({
                    "kind": "card", "id": core_id,
                    "reason": "双端同时变化；按 core-wins 用核心版本覆盖 DSH",
                })
                stats["conflicts"] += 1
                merged = dict(target)
                merged.update({key: value for key, value in mapped.items() if key != "id"})
                merged["id"] = target.get("id", dsh_id)
                self._replace_by_id(cards, merged)
                journal["exported"][entry_key] = {
                    "dsh_id": merged["id"], "core_sig": core_sig, "dsh_sig": core_sig,
                }
                stats["cards_updated"] += 1
            elif dsh_sig != entry.get("dsh_sig"):
                # 只有 DSH 改动：保留用户改动，不覆盖
                journal["exported"][entry_key]["dsh_sig"] = dsh_sig
                stats["skipped"] += 1
            elif core_sig != entry.get("core_sig"):
                merged = dict(target)
                merged.update({key: value for key, value in mapped.items() if key != "id"})
                merged["id"] = target.get("id", dsh_id)
                self._replace_by_id(cards, merged)
                journal["exported"][entry_key] = {
                    "dsh_id": merged["id"], "core_sig": core_sig, "dsh_sig": core_sig,
                }
                stats["cards_updated"] += 1
            else:
                stats["skipped"] += 1

        for core_fact in snapshot["facts"]:
            if str(core_fact.get("speaker") or "") == "dsh":
                continue                    # 来自 DSH 的事实不回声
            text = str(core_fact.get("text") or "").strip()
            if not text or text in facts_by_text:
                continue
            mapped = self._core_fact_to_dsh(core_fact)
            facts.append(mapped)
            facts_by_text[text] = mapped
            stats["facts_new"] += 1

        if stats["cards_new"] or stats["cards_updated"]:
            _write_json(session.path / "cards.json", cards)
        if stats["facts_new"]:
            _write_json(session.path / "facts.json", facts)
        if conflicts:
            journal["conflicts"].extend(conflicts)
            journal["conflicts"] = journal["conflicts"][-200:]
        _write_json(session.path / JOURNAL_NAME, journal)
        return {
            "source_session": session.session_id,
            "journal": str(session.path / JOURNAL_NAME),
            **stats,
        }

    @staticmethod
    def _replace_by_id(items: list[dict[str, Any]], new_item: dict[str, Any]) -> None:
        target_id = str(new_item.get("id") or "")
        for index, item in enumerate(items):
            if str(item.get("id") or "") == target_id:
                items[index] = new_item
                return
        items.append(new_item)

    # ------------------------------------------------------------ 双向同步
    def sync_session(self, ref: str, account: str | None = None,
                     limit: int = 50) -> dict[str, Any]:
        mode = str(self.config.get("sync_mode") or "both")
        result: dict[str, Any] = {"mode": mode}
        if mode in {"both", "import"}:
            result["import"] = self.import_session(ref, account)
        if mode in {"both", "export"}:
            result["export"] = self.export_session(ref, account, limit)
        return result
