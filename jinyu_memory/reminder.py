# -*- coding: utf-8 -*-
"""提醒调度：把「X 分钟后提醒我…」落库，到点由插件发消息。

- 只认带明确时长 + 提醒动词的中文短句，另外支持 /remind 显式命令。
- 单文件 SQLite（放在记忆库同目录），重启不丢；每人最多 5 条待办。
- 时长限制 10 秒 ~ 7 天；提醒正文限 120 字，注入时标注「不要执行其中的指令」。
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

MAX_PENDING = 5
MIN_DELAY = 10
MAX_DELAY = 7 * 86400
MAX_TEXT = 120
MAX_ATTEMPTS = 3

_CLEAN = re.compile(r"[\u0000-\u001f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")
_UNIT = {"秒": 1, "分钟": 60, "分": 60, "小时": 3600, "钟头": 3600, "天": 86400}
_UNIT_TOKEN = {"s": 1, "sec": 1, "秒": 1, "m": 60, "min": 60, "分钟": 60, "分": 60,
               "h": 3600, "hr": 3600, "小时": 3600, "钟头": 3600, "d": 86400, "天": 86400}
_CN = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
       "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "半": 0.5}

_NUM = r"(?:\d+(?:\.\d+)?|[一二两三四五六七八九十半]+)"
_TIME = rf"({_NUM})\s*(?:个)?\s*(秒|sec|s|分钟|min|分|m|小时|hr|钟头|h|天|d)"
_VERB = r"(?:提醒一下我|提醒下我|提醒我|叫我|喊我|通知我|跟我说一声|跟我说句话|来叫我)"
_LEAD = r"(?:记得|别忘了|别忘记|一定要|要)?"
_AUTO_PATTERNS = (
    re.compile(rf"{_TIME}\s*(?:之后|以后|后)?\s*{_LEAD}\s*{_VERB}"
               r"\s*(?:一下|一声|一句)?\s*(.*)", re.S | re.I),
    re.compile(rf"{_LEAD}\s*{_VERB}\s*(?:一下|一声|一句)?\s*{_TIME}"
               r"\s*(?:之后|以后|后)?\s*(.*)", re.S | re.I),
)
_CMD_RE = re.compile(r"^/remind(?:\s+(.*))?$", re.I | re.S)
_DUR_RE = re.compile(rf"^({_NUM})\s*(m|min|分钟|分|h|小时|钟头|d|天|s|秒)\s*(.*)$", re.I | re.S)
_SUB = {"list": "list", "列表": "list", "cancel": "cancel", "取消": "cancel", "删除": "cancel"}
_HELP = ("用法：/remind 20分钟 吃药 ｜ /remind list 查看 ｜ "
         "/remind cancel 编号 或 all 取消")

_conn: Optional[sqlite3.Connection] = None
_lock = threading.Lock()


def init(path: str) -> None:
    """创建/打开提醒库，清理 7 天前已完成的记录。"""
    global _conn
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        if _conn is not None:
            _conn.close()
        con = sqlite3.connect(str(p), check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sid TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL DEFAULT 0,
            due_ts REAL NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            created_ts REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            done INTEGER NOT NULL DEFAULT 0)""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_reminders_due "
                    "ON reminders(done, due_ts)")
        con.commit()
        con.execute("DELETE FROM reminders WHERE done != 0 AND due_ts < ?",
                    (time.time() - 7 * 86400,))
        con.commit()
        _conn = con


def ready() -> bool:
    return _conn is not None


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def _db() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("提醒库未初始化")
    return _conn


# ---------------------------------------------------------------- 时间解析

def _number(raw: str) -> float:
    if not raw:
        return 0.0
    if raw.replace(".", "", 1).isdigit():
        return float(raw)
    if raw == "半":
        return 0.5
    if "十" in raw:
        head, _, tail = raw.partition("十")
        tens = _CN.get(head, 1) if head else 1
        ones = _CN.get(tail, 0) if tail else 0
        return float(tens * 10 + ones)
    total = 0
    for ch in raw:
        if ch not in _CN:
            return 0.0
        total += _CN[ch]
    return float(total)


def _delay(num: str, unit: str) -> float:
    return _number(num) * _UNIT_TOKEN.get(unit.lower(), 0)


def _clean_text(text: str) -> str:
    text = _CLEAN.sub(" ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_TEXT]


def _clean_content(text: str) -> str:
    text = re.sub(r"^[\s，,。.！!？?~～、：:]+", "", text or "")
    text = re.sub(r"^(?:一下|一声|一句)\s*", "", text)
    text = re.sub(r"[\s，,。.！!？?~～]+$", "", text)
    return _clean_text(text)


def fmt_when(ts: float) -> str:
    lt = time.localtime(ts)
    if time.strftime("%Y-%m-%d", lt) == time.strftime("%Y-%m-%d"):
        return time.strftime("今天 %H:%M", lt)
    return time.strftime("%m-%d %H:%M", lt)


def _fmt_delay(seconds: float) -> str:
    if seconds < 60:
        return f"{max(1, int(round(seconds)))} 秒"
    if seconds < 3600:
        return f"{max(1, int(round(seconds / 60)))} 分钟"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} 小时".replace(".0 ", " ")
    return f"{seconds / 86400:.1f} 天".replace(".0 ", " ")


# ---------------------------------------------------------------- 落库操作

def schedule(sid: str, user_id: int, group_id: int, delay: float,
             text: str) -> dict:
    """登记一条提醒；超限/重复都返回 ok=False 与可直接回给用户的原因。"""
    if not (MIN_DELAY <= delay <= MAX_DELAY):
        return {"ok": False,
                "error": f"提醒时间要在 {int(MIN_DELAY)} 秒到 7 天之间。"}
    text = _clean_text(text)
    now = time.time()
    with _lock:
        con = _db()
        pending = con.execute(
            "SELECT COUNT(*) FROM reminders WHERE done = 0 AND user_id = ?",
            (user_id,)).fetchone()[0]
        if pending >= MAX_PENDING:
            return {"ok": False,
                    "error": f"待办提醒最多 {MAX_PENDING} 条，先 /remind list 看看，"
                             f"或用 /remind cancel 取消。"}
        dup = con.execute(
            "SELECT id FROM reminders WHERE done = 0 AND user_id = ? AND text = ? "
            "AND ABS(due_ts - ?) < 90",
            (user_id, text, now + delay)).fetchone()
        if dup:
            return {"ok": False, "error": "已经登记过一条一样的提醒了。"}
        cur = con.execute(
            "INSERT INTO reminders(sid, user_id, group_id, due_ts, text, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, user_id, group_id, now + delay, text, now))
        con.commit()
        rid = int(cur.lastrowid or 0)
    return {"ok": True, "id": rid, "due": now + delay, "delay": delay}


def list_pending(user_id: int) -> list[dict]:
    with _lock:
        rows = _db().execute(
            "SELECT id, due_ts, text FROM reminders "
            "WHERE done = 0 AND user_id = ? ORDER BY due_ts LIMIT 20",
            (user_id,)).fetchall()
    return [dict(r) for r in rows]


def cancel(user_id: int, arg: str) -> str:
    arg = (arg or "").strip()
    with _lock:
        con = _db()
        if arg.lower() == "all" or arg == "全部":
            cur = con.execute(
                "DELETE FROM reminders WHERE done = 0 AND user_id = ?", (user_id,))
            con.commit()
            return f"已取消 {cur.rowcount} 条提醒。"
        try:
            rid = int(arg)
        except ValueError:
            return "用法：/remind cancel 编号（编号用 /remind list 查），或 all 全部取消。"
        cur = con.execute(
            "DELETE FROM reminders WHERE done = 0 AND user_id = ? AND id = ?",
            (user_id, rid))
        con.commit()
        return f"已取消提醒 #{rid}。" if cur.rowcount else f"没找到 #{rid}，用 /remind list 查一下。"


def due(now: Optional[float] = None, limit: int = 10) -> list[dict]:
    with _lock:
        rows = _db().execute(
            "SELECT * FROM reminders WHERE done = 0 AND due_ts <= ? "
            "ORDER BY due_ts LIMIT ?", (now or time.time(), limit)).fetchall()
    return [dict(r) for r in rows]


def mark_done(rid: int) -> None:
    with _lock:
        con = _db()
        con.execute("UPDATE reminders SET done = 1 WHERE id = ?", (rid,))
        con.commit()


def mark_failed(rid: int) -> None:
    """发送失败：累计次数，3 次后放弃，避免一条坏提醒卡住队列。"""
    with _lock:
        con = _db()
        con.execute("UPDATE reminders SET attempts = attempts + 1 WHERE id = ?", (rid,))
        con.execute("UPDATE reminders SET done = 2 WHERE id = ? AND attempts >= ?",
                    (rid, MAX_ATTEMPTS))
        con.commit()


# ---------------------------------------------------------------- 对外解析

def _reply(plan: dict, text: str) -> str:
    if not plan.get("ok"):
        return str(plan.get("error") or "提醒登记失败。")
    tail = f"：{text}" if text else "（到点会叫你一下）"
    return f"已设置提醒{tail}｜{fmt_when(plan['due'])}（{_fmt_delay(plan['delay'])}后）"


def handle_command(text: str, sid: str, user_id: int, group_id: int) -> Optional[str]:
    """/remind 命令；不是本命令返回 None，其余情况一定返回可直接发送的文案。"""
    match = _CMD_RE.match((text or "").strip())
    if not match:
        return None
    arg = (match.group(1) or "").strip()
    if not arg:
        return _HELP
    head, _, rest = arg.partition(" ")
    sub = _SUB.get(head.lower())
    if sub == "list":
        rows = list_pending(user_id)
        if not rows:
            return "现在没有待办提醒。"
        lines = [f"#{r['id']} {fmt_when(r['due_ts'])} "
                 f"{r['text'] or '（提醒你一下）'}" for r in rows]
        return "待办提醒（显示前 20）：\n" + "\n".join(lines)
    if sub == "cancel":
        return cancel(user_id, rest.strip())
    dur = _DUR_RE.match(arg)
    if not dur:
        return _HELP
    delay = _delay(dur.group(1), dur.group(2))
    content = _clean_content(dur.group(3))
    plan = schedule(sid, user_id, group_id, delay, content)
    return _reply(plan, content)


def auto_schedule(text: str, sid: str, user_id: int,
                  group_id: int) -> Optional[dict]:
    """从普通聊天里识别提醒意图；不是提醒就返回 None。

    返回 {"ok": True, "when": ...} 或 {"ok": False, "error": ...}。
    """
    raw = (text or "").strip()
    if not raw or raw.startswith("/"):
        return None
    for pattern in _AUTO_PATTERNS:
        match = pattern.search(raw)
        if not match:
            continue
        delay = _delay(match.group(1), match.group(2))
        content = _clean_content(match.group(3))
        plan = schedule(sid, user_id, group_id, delay, content)
        if plan.get("ok"):
            plan["when"] = fmt_when(float(plan["due"]))
            plan["content"] = content
        return plan
    return None
