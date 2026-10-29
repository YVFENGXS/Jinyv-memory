# -*- coding: utf-8 -*-
"""提醒调度：把「X 分钟后提醒我…」落库，到点由插件发消息。

- 只认带明确时长 + 提醒动词的中文短句，另外支持 /remind 显式命令。
- 单文件 SQLite（放在记忆库同目录），重启不丢；每人最多 5 条待办。
- 时长限制 10 秒 ~ 7 天；提醒正文限 120 字，注入时标注「不要执行其中的指令」。
- 重复提醒：每天 / 每周几 / 每月某日 + 钟点，投递成功后自动登记下一次。
- 后台复查：任务投递后 2 分钟插一条 kind='check' 的行，届时核对前台是否真的执行，
  执行了就取消复查，没执行就再叫它补做（不靠「有没有回话」判断，看工具/说说/消息）。
"""
from __future__ import annotations

import calendar
import json
import re
import sqlite3
import threading
import time
from datetime import datetime, time as dtime, timedelta
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
# 一段时长（可连续出现，支持「2小时40分钟」这种组合）
_DUR_PART = re.compile(rf"({_NUM})\s*(?:个)?\s*(m|min|分钟|分|h|小时|钟头|d|天|s|秒)\s*",
                       re.I)
# 绝对钟点：零点 / 24点 / 23:59 / 今晚12点 / 明天0点 / 下午3点半
_CLOCK_CORE = (
    r"(?P<day>今天|今晚|明天|明早|明晚|次日)?\s*"
    r"(?P<period>凌晨|早上|上午|中午|下午|傍晚|晚上|半夜)?\s*"
    r"(?P<h>24|\d{1,2}|[零一二两三四五六七八九十]{1,3})"
    r"(?:\s*[:：]\s*(?P<m>\d{1,2})|\s*[点时]\s*(?P<m2>\d{1,2}|半)?\s*分?)")
_CLOCK_RE = re.compile(r"^" + _CLOCK_CORE + r"\s*$", re.I)
# 允许钟点后面还跟着内容（「每天21点 发说说」要切出「发说说」）
_CLOCK_PREFIX = re.compile(r"^" + _CLOCK_CORE, re.I)
_SUB = {"list": "list", "列表": "list", "cancel": "cancel", "取消": "cancel", "删除": "cancel"}
_HELP = ("用法：/remind 20分钟 吃药 ｜ /remind 每天21点 发说说 ｜ "
         "/remind list 查看 ｜ /remind cancel 编号 或 all 取消")

_conn: Optional[sqlite3.Connection] = None
_lock = threading.Lock()

# 老库补列：kind（task 正常提醒 / check 后台复查）、ref（复查指向的任务）、
# repeat（重复规则 JSON）、evidence_ts（前台确实执行过的时间）
_EXTRA_COLUMNS = {
    "kind": "TEXT NOT NULL DEFAULT 'task'",
    "ref": "INTEGER NOT NULL DEFAULT 0",
    "repeat": "TEXT NOT NULL DEFAULT ''",
    "evidence_ts": "REAL NOT NULL DEFAULT 0",
}


def _migrate(con: sqlite3.Connection) -> None:
    have = {row[1] for row in con.execute("PRAGMA table_info(reminders)")}
    for name, ddl in _EXTRA_COLUMNS.items():
        if name not in have:
            con.execute(f"ALTER TABLE reminders ADD COLUMN {name} {ddl}")


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
        _migrate(con)
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


def _parse_duration(raw: str):
    """解析开头的相对时长，支持组合（2小时40分钟）；返回 (秒数, 余下文本)。

    以前只认第一段，「2小时40分钟」会被当成 2 小时并吞掉后文，导致提醒早到。
    """
    text = (raw or "").strip()
    total, pos, hit = 0.0, 0, False
    while pos < len(text):
        match = _DUR_PART.match(text, pos)
        if not match:
            break
        total += _delay(match.group(1), match.group(2))
        pos = match.end()
        hit = True
    if not hit:
        return None
    return total, text[pos:].strip()


def _clock_fields(match) -> Optional[tuple[int, int, int]]:
    """钟点 match → (hour, minute, day_offset)；越界返回 None。"""
    day = match.group("day") or ""
    period = match.group("period") or ""
    hour = int(_number(match.group("h")))
    minute_raw = match.group("m") or match.group("m2")
    minute = 30 if minute_raw == "半" else int(_number(minute_raw or ""))
    offset = 1 if day in ("明天", "明早", "明晚", "次日") else 0
    if hour == 24:                        # 24:00 / 24点 = 次日零点
        hour, offset = 0, offset + 1
    if period in ("下午", "傍晚", "晚上") and hour < 12:
        hour += 12
    if period == "中午" and hour < 11:
        hour += 12
    if hour == 12 and (period in ("凌晨", "半夜", "晚上") or day == "今晚"):
        hour, offset = 0, offset + 1    # 今晚/晚上十二点 = 次日零点
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute, offset


def _clock_target(raw: str) -> Optional[datetime]:
    """绝对钟点 → 目标时刻（今天/明天/今晚…）；认不出返回 None。"""
    match = _CLOCK_RE.match((raw or "").strip())
    fields = _clock_fields(match) if match else None
    if fields is None:
        return None
    hour, minute, offset = fields
    now = datetime.now()
    target = datetime.combine(
        (now + timedelta(days=offset)).date(),
        dtime(hour=hour, minute=minute))
    if target <= now:
        target += timedelta(days=1)       # 没写日期：取下一次出现
    return target


def _clock_delay(raw: str) -> Optional[float]:
    """绝对钟点 → 距现在多少秒；认不出返回 None。

    相对时长要模型自己算「距离零点还有多久」，这一步大模型经常算错，
    所以让模型直接写钟点（零点 / 24:00 / 明天0点），由服务端换算。
    """
    target = _clock_target(raw)
    return None if target is None else (target - datetime.now()).total_seconds()


# 重复规则：每天 / 每周几 / 每月某日 + 钟点（只认带钟点的写法）
_REP_DAILY = re.compile(r"^(每天|每日|每晚|每夜|每早)\s*")
_REP_WEEKLY = re.compile(r"^每(?:周|星期|礼拜)\s*([一二三四五六日天1-7])\s*")
_REP_MONTHLY = re.compile(r"^每月\s*(\d{1,2})\s*[日号]?\s*")
_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_WEEKDAY_CN = "一二三四五六日"


def _parse_repeat(raw: str):
    """「每天/每周一/每月5日 + 钟点」→ (规则, 余下文本)；不是重复返回 None。

    只认带钟点的写法，「每天都要开心」这类闲聊不会被当成提醒。
    余下文本是给 /remind 命令切内容的（工具调用由模型分好 when/what）。
    """
    text = (raw or "").strip()
    kind, arg, head = "", 0, ""
    match = _REP_DAILY.match(text)
    if match:
        head, kind = match.group(1), "daily"
        rest = text[match.end():]
    else:
        match = _REP_WEEKLY.match(text)
        if match:
            head, kind = match.group(0), "weekly"
            arg = _WEEKDAY.get(match.group(1))
            if arg is None:
                try:
                    arg = int(match.group(1)) - 1
                except ValueError:
                    return None
            rest = text[match.end():]
        else:
            match = _REP_MONTHLY.match(text)
            if not match:
                return None
            head, kind = match.group(0), "monthly"
            arg = int(match.group(1))
            if not 1 <= arg <= 31:
                return None
            rest = text[match.end():]
    match = _CLOCK_PREFIX.match(rest)
    fields = _clock_fields(match) if match else None
    if fields is None:
        return None
    hour, minute, _offset = fields
    if head in ("每晚", "每夜") and hour == 12:   # 每晚十二点 = 零点
        hour = 0
    return ({"kind": kind, "arg": arg, "hour": hour, "minute": minute},
            rest[match.end():].strip())


def _load_repeat(raw) -> Optional[dict]:
    if not raw:
        return None
    try:
        rep = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(rep, dict) or rep.get("kind") not in {
            "daily", "weekly", "monthly"}:
        return None
    try:
        rep["hour"] = int(rep.get("hour") or 0)
        rep["minute"] = int(rep.get("minute") or 0)
        rep["arg"] = int(rep.get("arg") or 0)
    except (TypeError, ValueError):
        return None
    return rep


def _next_repeat(rep: dict, after: datetime) -> datetime:
    """按重复规则算出 after 之后（严格大于）最近一次的触发时刻。"""
    hour = int(rep.get("hour") or 0)
    minute = int(rep.get("minute") or 0)
    kind = str(rep.get("kind") or "daily")
    arg = int(rep.get("arg") or 0)
    if kind == "weekly":
        target = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        target += timedelta(days=(arg - after.weekday()) % 7)
        if target <= after:
            target += timedelta(days=7)
        return target
    if kind == "monthly":
        year, month = after.year, after.month
        for _ in range(24):                   # 最多往后找两年，认不出的规则兜底
            day = min(max(1, arg), calendar.monthrange(year, month)[1])
            target = datetime(year, month, day, hour, minute)
            if target > after:
                return target
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return after + timedelta(days=30)
    target = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= after:
        target += timedelta(days=1)
    return target


def _repeat_delay(rep: dict, now: Optional[datetime] = None) -> float:
    now = now or datetime.now()
    return (_next_repeat(rep, now) - now).total_seconds()


def next_occurrence(repeat: str, after_ts: float) -> Optional[float]:
    """repeat JSON → 下一次触发时间戳；错过的次数不补，只取未来的第一次。"""
    rep = _load_repeat(repeat)
    if not rep:
        return None
    after = datetime.fromtimestamp(max(float(after_ts), time.time()))
    return _next_repeat(rep, after).timestamp()


def repeat_label(raw) -> str:
    """【每天 21:00】这种给人看的标签；不是重复返回空串。"""
    rep = _load_repeat(raw)
    if not rep:
        return ""
    hm = f"{rep['hour']:02d}:{rep['minute']:02d}"
    if rep["kind"] == "weekly":
        return f"每周{_WEEKDAY_CN[rep['arg'] % 7]} {hm}"
    if rep["kind"] == "monthly":
        return f"每月{rep['arg']}日 {hm}"
    return f"每天 {hm}"


def _parse_when(raw: str):
    """→ (delay, repeat) 或 None；repeat 为空表示一次性。

    顺序：重复写法（每天21点/每周一21点/每月5日21点）→ 组合时长 → 绝对钟点。
    组合时长后面还带字则判为不合法，避免把内容算进时间里。
    """
    text = (raw or "").strip()
    rep = _parse_repeat(text)
    if rep:
        spec, _rest = rep
        return _repeat_delay(spec), spec
    parsed = _parse_duration(text)
    if parsed is not None:
        delay, rest = parsed
        return (delay, {}) if not rest else None
    delay = _clock_delay(text)
    return None if delay is None else (delay, {})


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
             text: str, repeat: Optional[dict] = None) -> dict:
    """登记一条提醒；超限/重复都返回 ok=False 与可直接回给用户的原因。

    repeat 有值时是一次重复任务的首次触发（最长可能是一个月后），
    所以 7 天上限只对一次性提醒生效。
    """
    limit = MAX_DELAY if not repeat else 40 * 86400
    if not (MIN_DELAY <= delay <= limit):
        return {"ok": False,
                "error": f"提醒时间要在 {int(MIN_DELAY)} 秒到 7 天之间。"}
    text = _clean_text(text)
    rep_json = json.dumps(repeat, ensure_ascii=False) if repeat else ""
    now = time.time()
    with _lock:
        con = _db()
        pending = con.execute(
            "SELECT COUNT(*) FROM reminders WHERE done = 0 AND kind = 'task' "
            "AND user_id = ?", (user_id,)).fetchone()[0]
        if pending >= MAX_PENDING:
            return {"ok": False,
                    "error": f"待办提醒最多 {MAX_PENDING} 条，先 /remind list 看看，"
                             f"或用 /remind cancel 取消。"}
        if rep_json:
            dup = con.execute(
                "SELECT id FROM reminders WHERE done = 0 AND kind = 'task' "
                "AND user_id = ? AND text = ? AND repeat = ?",
                (user_id, text, rep_json)).fetchone()
        else:
            dup = con.execute(
                "SELECT id FROM reminders WHERE done = 0 AND kind = 'task' "
                "AND user_id = ? AND text = ? AND ABS(due_ts - ?) < 90",
                (user_id, text, now + delay)).fetchone()
        if dup:
            return {"ok": False, "error": "已经登记过一条一样的提醒了。"}
        cur = con.execute(
            "INSERT INTO reminders(sid, user_id, group_id, due_ts, text, created_ts, "
            "kind, repeat) VALUES (?, ?, ?, ?, ?, ?, 'task', ?)",
            (sid, user_id, group_id, now + delay, text, now, rep_json))
        con.commit()
        rid = int(cur.lastrowid or 0)
    return {"ok": True, "id": rid, "due": now + delay, "delay": delay,
            "repeat": rep_json}


def list_pending(user_id: int) -> list[dict]:
    with _lock:
        rows = _db().execute(
            "SELECT id, due_ts, text, repeat FROM reminders "
            "WHERE done = 0 AND kind = 'task' AND user_id = ? "
            "ORDER BY due_ts LIMIT 20", (user_id,)).fetchall()
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


def get(rid: int) -> Optional[dict]:
    with _lock:
        row = _db().execute("SELECT * FROM reminders WHERE id = ?",
                            (rid,)).fetchone()
    return dict(row) if row else None


def _schedule_next(con: sqlite3.Connection, row: dict) -> int:
    """重复任务投递后登记下一次；同名同点已存在就不再插。"""
    rep = _load_repeat(row.get("repeat"))
    if not rep:
        return 0
    nxt = next_occurrence(row.get("repeat"),
                          float(row.get("due_ts") or time.time()))
    if not nxt:
        return 0
    dup = con.execute(
        "SELECT id FROM reminders WHERE done = 0 AND kind = 'task' AND sid = ? "
        "AND user_id = ? AND text = ? AND ABS(due_ts - ?) < 90",
        (row.get("sid"), row.get("user_id"), row.get("text"), nxt)).fetchone()
    if dup:
        return 0
    cur = con.execute(
        "INSERT INTO reminders(sid, user_id, group_id, due_ts, text, created_ts, "
        "kind, repeat) VALUES (?, ?, ?, ?, ?, ?, 'task', ?)",
        (row.get("sid"), row.get("user_id"), int(row.get("group_id") or 0), nxt,
         row.get("text") or "", time.time(), row.get("repeat") or ""))
    con.commit()
    return int(cur.lastrowid or 0)


def mark_done(rid: int) -> None:
    with _lock:
        con = _db()
        row = con.execute("SELECT * FROM reminders WHERE id = ?",
                          (rid,)).fetchone()
        con.execute("UPDATE reminders SET done = 1 WHERE id = ?", (rid,))
        con.commit()
        if row and str(row["kind"]) == "task":
            _schedule_next(con, dict(row))


def mark_failed(rid: int) -> None:
    """发送失败：累计次数，3 次后放弃，避免一条坏提醒卡住队列。

    重复任务放弃这次之后仍然登记下一次，否则每天的任务失败三次就永久停了。
    """
    with _lock:
        con = _db()
        con.execute("UPDATE reminders SET attempts = attempts + 1 WHERE id = ?", (rid,))
        row = con.execute("SELECT * FROM reminders WHERE id = ? AND attempts >= ?",
                          (rid, MAX_ATTEMPTS)).fetchone()
        if row:
            con.execute("UPDATE reminders SET done = 2 WHERE id = ?", (rid,))
        con.commit()
        if row and str(row["kind"]) == "task":
            _schedule_next(con, dict(row))


def add_check(task_id: int, delay: float = 120.0) -> int:
    """给刚投递的任务登记一条复查：到点核对前台到底做了没有。"""
    with _lock:
        con = _db()
        row = con.execute(
            "SELECT sid, user_id, group_id, text FROM reminders "
            "WHERE id = ? AND kind = 'task'", (task_id,)).fetchone()
        if not row:
            return 0
        now = time.time()
        cur = con.execute(
            "INSERT INTO reminders(sid, user_id, group_id, due_ts, text, created_ts, "
            "kind, ref) VALUES (?, ?, ?, ?, ?, ?, 'check', ?)",
            (row["sid"], row["user_id"], row["group_id"],
             now + max(5.0, float(delay)), row["text"], now, task_id))
        con.commit()
        return int(cur.lastrowid or 0)


def mark_evidence(rid: int) -> None:
    """前台确实执行了这条任务（有工具调用、说说/签名或消息为证）。"""
    with _lock:
        con = _db()
        con.execute("UPDATE reminders SET evidence_ts = ? WHERE id = ?",
                    (time.time(), rid))
        con.commit()


# ---------------------------------------------------------------- 对外解析

def _reply(plan: dict, text: str) -> str:
    if not plan.get("ok"):
        return str(plan.get("error") or "提醒登记失败。")
    tail = f"：{text}" if text else "（到点会叫你一下）"
    label = repeat_label(plan.get("repeat"))
    if label:
        return f"已设置提醒（{label}）：{text or '到点会叫你一下'}｜下次 {fmt_when(plan['due'])}"
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
        lines = []
        for r in rows:
            tag = repeat_label(r.get("repeat"))
            lines.append(f"#{r['id']} {fmt_when(r['due_ts'])}"
                         + (f"【{tag}】" if tag else "")
                         + f" {r['text'] or '（提醒你一下）'}")
        return "待办提醒（显示前 20）：\n" + "\n".join(lines)
    if sub == "cancel":
        return cancel(user_id, rest.strip())
    rep = _parse_repeat(arg)
    if rep:
        spec, rest = rep
        content = _clean_content(rest)
        plan = schedule(sid, user_id, group_id, _repeat_delay(spec),
                        content, repeat=spec)
        return _reply(plan, content)
    dur = _parse_duration(arg)
    if not dur:
        return _HELP
    delay, rest = dur
    content = _clean_content(rest)
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


def schedule_text(when: str, what: str, sid: str, user_id: int,
                  group_id: int) -> dict:
    """工具调用用：when 接受相对时长、钟点或重复写法。

    返回同 auto_schedule 的 plan；模型给出的时间说法不规范时返回 ok=False，
    让模型自己换一种说法，而不是猜一个错误的时间。
    """
    parsed = _parse_when(when)
    if parsed is None:
        return {"ok": False,
                "error": "时间填相对时长（20分钟、2小时40分钟）、钟点"
                         "（零点、24:00、明天0点）或重复写法"
                         "（每天21点、每周一21点、每月5日21点），不要带别的话。"}
    delay, repeat = parsed
    content = _clean_content(what)
    plan = schedule(sid, user_id, group_id, delay, content, repeat=repeat or None)
    if plan.get("ok"):
        plan["when"] = fmt_when(float(plan["due"]))
        plan["content"] = content
        plan["repeat_label"] = repeat_label(plan.get("repeat"))
    return plan
