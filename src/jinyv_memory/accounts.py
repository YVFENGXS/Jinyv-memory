# -*- coding: utf-8 -*-
"""多账号 / 多平台车道。

一条 lane = 机器人账号 × 平台 × 会话。热窗、游标、回复租约都落在 lane 上；
长期记忆按 character_id 与 session_id 共享，因此同一人格的不同入口可以读写
同一条记忆，但仍保留各自的写入来源（bot_id / platform）。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from .errors import ConfigError


@dataclass(frozen=True)
class Lane:
    key: str
    platform: str
    account: str
    bot_id: str
    character_id: str
    session_id: str
    continuity_id: str
    role: str = "primary"
    owner: bool = False

    @property
    def is_primary(self) -> bool:
        return self.role == "primary"

    def with_session(self, session_id: str) -> "Lane":
        return replace(self, session_id=session_id)


def _derive_session(platform: str, account: str, kind: str) -> str:
    if platform == "qq":
        if kind == "group":
            return f"qq-g{account}"
        return f"qq-u{account}"
    if platform == "bili":
        return f"bili-l{account}"
    prefix = "g" if kind == "group" else "u"
    return f"{platform}-{prefix}{account}"


class AccountRegistry:
    def __init__(self, lanes: list[Lane], default_key: str = ""):
        self._lanes = {lane.key: lane for lane in lanes}
        self.default_key = default_key or (lanes[0].key if lanes else "")

    @classmethod
    def from_config(cls, raw: Mapping[str, Any]) -> "AccountRegistry":
        defaults = raw.get("defaults") if isinstance(raw.get("defaults"), dict) else {}
        entries = raw.get("accounts") if isinstance(raw.get("accounts"), dict) else {}
        lanes: list[Lane] = []
        for key, item in entries.items():
            if not isinstance(item, dict):
                continue
            platform = str(item.get("platform") or defaults.get("platform") or "qq")
            account = str(item.get("account") or key)
            character = str(item.get("character") or defaults.get("character") or "jinyu")
            kind = str(item.get("kind") or defaults.get("kind") or "private")
            session_id = str(item.get("session") or _derive_session(platform, account, kind))
            continuity = str(item.get("continuity") or defaults.get("continuity")
                             or f"{character}-main")
            lane = Lane(
                key=str(key),
                platform=platform,
                account=account,
                bot_id=str(item.get("bot_id") or defaults.get("bot_id") or platform),
                character_id=character,
                session_id=session_id,
                continuity_id=continuity,
                role=str(item.get("role") or defaults.get("role") or "primary"),
                owner=bool(item.get("owner", defaults.get("owner", False))),
            )
            lanes.append(lane)
        default_key = str(defaults.get("account") or "")
        if not default_key and lanes:
            primary = next((lane for lane in lanes if lane.is_primary), lanes[0])
            default_key = primary.key
        return cls(lanes, default_key)

    def get(self, key: str | None = None) -> Lane:
        lane_key = str(key or self.default_key)
        if not lane_key:
            raise ConfigError("没有可用账号车道：请检查 config/accounts.toml")
        lane = self._lanes.get(lane_key)
        if lane is None:
            raise ConfigError(f"未知账号车道：{lane_key}")
        return lane

    def find(self, key: str) -> Lane | None:
        return self._lanes.get(key)

    def match_session(self, platform: str, session_id: str) -> Lane | None:
        for lane in self._lanes.values():
            if lane.platform == platform and lane.session_id == session_id:
                return lane
        return None

    def register(self, lane: Lane) -> None:
        self._lanes[lane.key] = lane
        if not self.default_key:
            self.default_key = lane.key

    def list(self) -> list[Lane]:
        return sorted(self._lanes.values(), key=lambda lane: lane.key)
