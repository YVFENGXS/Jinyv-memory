# -*- coding: utf-8 -*-
"""框架中立宿主：不依赖 NoneBot / OneBot，任何程序都能读写瑾瑜记忆。

- chat()     发一句话 → 走完整链路（落 turns、拼上下文、生成回复、触发归档）
- remember() 直接写一条长期词条（外部事件、dsh 观察到的东西）
- recall()   只读：相关词条 + 最近事件卡
- close()    冲刷尾部归档（进程退出前必须调用）

宿主只负责「谁在说话、哪个会话」，人设与记忆逻辑仍由 core 统一管理。
"""
from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path
from typing import Optional

from .llm import from_env
from .persona import PERSONA
from .rp_skeleton import DB, RPContextMiddleware


class _NoLLM:
    """没配密钥时的占位：读写记忆照常，只有真正生成时才报错。"""

    def __init__(self, role: str):
        self.role = role

    def _fail(self):
        raise RuntimeError(f"缺少 {self.role} 模型密钥：请设置 RP_FRONT_KEY / RP_BACK_KEY")

    def chat(self, *args, **kwargs):
        self._fail()

    def chat_tools(self, *args, **kwargs):
        self._fail()


def _llm_or_stub(role: str):
    try:
        return from_env(role)
    except ValueError:
        return _NoLLM(role)


class JinyuHost:
    """一个会话的宿主句柄；同一实例内串行，多个会话就建多个实例。"""

    def __init__(self, db_path: Optional[str] = None, cid: Optional[str] = None,
                 continuity: Optional[str] = None, sid: Optional[str] = None,
                 front_llm=None, back_llm=None, persona: Optional[dict] = None,
                 max_tokens: Optional[int] = None, output_reserve: Optional[int] = None,
                 profile: Optional[str] = None):
        self.cid = str(cid or os.environ.get("RP_CID") or "jinyu")
        self.continuity = str(continuity or os.environ.get("RP_CONTINUITY")
                              or self.cid + "-main")
        self.sid = str(sid or "host-" + uuid.uuid4().hex[:8])
        path = str(db_path or os.environ.get("RP_DB") or "data/rp_memory.db")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = DB(path)
        self.mw = RPContextMiddleware(
            front_llm or _llm_or_stub("front"),
            back_llm or _llm_or_stub("back"), self.db,
            max_tokens=int(max_tokens or os.environ.get("RP_MAX_TOKENS") or 64000),
            output_reserve=int(output_reserve
                               or os.environ.get("RP_OUTPUT_RESERVE") or 4096),
            profile=profile or os.environ.get("RP_PROFILE") or "cl100k")
        self.persona = dict(persona or PERSONA)
        self._lock = threading.Lock()
        self._closed = False
        self.mw.open_session(self.sid, self.cid, self.continuity)

    def chat(self, text: str) -> str:
        """写入一轮对话并返回瑾瑜的回复；空文本直接返回空串。"""
        text = str(text or "").strip()
        if not text:
            return ""
        with self._lock:
            return str(self.mw.chat(text, self.persona, self.sid, self.cid) or "")

    def remember(self, text: str, speaker: str = "", importance: float = 0.7) -> int:
        """直接补一条长期词条，返回新 id（0 = 空文本或重复被拒）。"""
        with self._lock:
            return int(self.db.add_fact(self.sid, self.cid, str(text or ""), 0,
                                        str(speaker or ""), float(importance)))

    def recall(self, query: str = "", limit: int = 6) -> dict:
        """只读召回：相关长期词条 + 最近事件卡。"""
        limit = max(1, min(50, int(limit or 6)))
        facts = self.db.list_facts_detail(self.sid, self.cid, limit=limit,
                                          query=str(query or ""))
        cards = self.db.recent_cards(self.sid, self.cid, limit=limit)
        return {
            "facts": [{"id": f.get("id"), "text": f.get("label")} for f in facts],
            "cards": [{"id": c.get("id"), "summary": c.get("summary"),
                       "turn_end": c.get("turn_end")} for c in cards],
        }

    def new_session(self, sid: Optional[str] = None) -> str:
        """换一个新会话，continuity 不变（跨会话长期记忆继续生效）。"""
        with self._lock:
            self.mw.close_session(self.sid, self.cid)
            self.sid = str(sid or "host-" + uuid.uuid4().hex[:8])
            self.mw.open_session(self.sid, self.cid, self.continuity)
            return self.sid

    def close(self) -> None:
        """冲刷归档；可重复调用。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.mw.shutdown()
