# -*- coding: utf-8 -*-
"""主人身份闸门：验证码双向绑定 + 观测态，模型永远不能把自己升级成主人。

身份三层：
- persona  人格，全局唯一；
- principal 真人主体，由已验证的账号集合代表；
- account  平台账号，键 = (platform, account)，例如 ("qq", "123456")。

绑定必须双向证明：
1. 管理员在自己账号的私聊里发 /owner bind qq 123456 → 生成 6 位验证码；
2. 目标账号 123456 在 qq 私聊里发 /owner verify <验证码> → 才算 verified。

模型侧只能写 observed（「看起来像主人」的观察），永远不会覆盖 verified，
也不能把 observed 升成 verified。没绑定过的账号一律按陌生人处理。
"""
from __future__ import annotations

import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

STATUS_PENDING = "pending"
STATUS_VERIFIED = "verified"
STATUS_OBSERVED = "observed"
STATUS_REVOKED = "revoked"

CODE_TTL = 600          # 验证码 10 分钟有效
MAX_TRIES = 5           # 错误次数上限，超过作废，需要重新发起

_CMD = re.compile(r"^/owner(?:\s+(.*))?$", re.I | re.S)
_PLATFORM = re.compile(r"^[a-z0-9_-]{1,16}$")
_ACCOUNT = re.compile(r"^[A-Za-z0-9_.:@-]{1,64}$")

_HELP = ("用法：/owner bind 平台 账号 → 生成验证码；目标账号在私聊发 "
         "/owner verify 验证码｜/owner list 查看｜/owner revoke 平台 账号 解绑｜"
         "/owner who 查自己")

_conn: Optional[sqlite3.Connection] = None
_lock = threading.Lock()


def _norm(platform, account) -> tuple[str, str]:
    p = str(platform or "").strip().lower()
    a = str(account or "").strip()
    return p, a


def _valid(p: str, a: str) -> bool:
    return bool(_PLATFORM.match(p) and _ACCOUNT.match(a))


def init(path: str) -> None:
    """打开/创建绑定库。"""
    global _conn
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        if _conn is not None:
            _conn.close()
        con = sqlite3.connect(str(p), timeout=10.0, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("""CREATE TABLE IF NOT EXISTS owner_bindings (
            platform TEXT NOT NULL,
            account TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            code TEXT NOT NULL DEFAULT '',
            tries INTEGER NOT NULL DEFAULT 0,
            created_ts REAL NOT NULL DEFAULT 0,
            expires_ts REAL NOT NULL DEFAULT 0,
            verified_ts REAL NOT NULL DEFAULT 0,
            verified_by TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (platform, account))""")
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
        raise RuntimeError("主人绑定库未初始化")
    return _conn


def status_of(platform: str, account: str) -> str:
    """当前状态；没记录返回空串。"""
    p, a = _norm(platform, account)
    if not ready() or not a:
        return ""
    with _lock:
        row = _db().execute(
            "SELECT status FROM owner_bindings WHERE platform=? AND account=?",
            (p, a)).fetchone()
    return str(row["status"]) if row else ""


def is_owner(platform: str, account: str) -> bool:
    """只有 verified 才算主人；observed 永远不算。"""
    return status_of(platform, account) == STATUS_VERIFIED


def verified_accounts(platform: str = "") -> list[tuple[str, str]]:
    """所有完成双向验证的绑定；platform 为空表示全部平台。

    用来按真人主体合并长期记忆：同一个主人名下的多个账号共用一份记忆。
    """
    if not ready():
        return []
    p = str(platform or "").strip().lower()
    with _lock:
        if p:
            rows = _db().execute(
                "SELECT platform, account FROM owner_bindings"
                " WHERE status=? AND platform=? ORDER BY platform, account",
                (STATUS_VERIFIED, p)).fetchall()
        else:
            rows = _db().execute(
                "SELECT platform, account FROM owner_bindings WHERE status=?"
                " ORDER BY platform, account", (STATUS_VERIFIED,)).fetchall()
    return [(str(r["platform"]), str(r["account"])) for r in rows]


def begin(platform: str, account: str, by: str = "",
          now: Optional[float] = None) -> dict:
    """管理员发起绑定：生成验证码，等目标账号回码。"""
    p, a = _norm(platform, account)
    if not _valid(p, a):
        return {"ok": False, "error": "平台用 qq/bilibili 这类小写标识，账号只允许字母数字与 _.:@-。"}
    now = time.time() if now is None else float(now)
    with _lock:
        con = _db()
        row = con.execute(
            "SELECT status FROM owner_bindings WHERE platform=? AND account=?",
            (p, a)).fetchone()
        if row and str(row["status"]) == STATUS_VERIFIED:
            return {"ok": False, "error": "该账号已经绑定为主人，先 /owner revoke 再重绑。"}
        code = f"{secrets.randbelow(1000000):06d}"
        con.execute(
            "INSERT INTO owner_bindings(platform,account,status,code,tries,"
            " created_ts,expires_ts,verified_ts,verified_by,note)"
            " VALUES(?,?,?,?,0,?,?,0,?,'')"
            " ON CONFLICT(platform,account) DO UPDATE SET"
            " status=excluded.status, code=excluded.code, tries=0,"
            " created_ts=excluded.created_ts, expires_ts=excluded.expires_ts,"
            " verified_ts=0, verified_by=excluded.verified_by",
            (p, a, STATUS_PENDING, code, now, now + CODE_TTL, by))
        con.commit()
    return {"ok": True, "platform": p, "account": a, "code": code,
            "expires": now + CODE_TTL}


def confirm(platform: str, account: str, code: str, via: str = "",
            now: Optional[float] = None) -> dict:
    """目标账号回码：验证码正确且未过期才升 verified。"""
    p, a = _norm(platform, account)
    entered = str(code or "").strip()
    now = time.time() if now is None else float(now)
    with _lock:
        con = _db()
        row = con.execute(
            "SELECT * FROM owner_bindings WHERE platform=? AND account=?",
            (p, a)).fetchone()
        if not row or str(row["status"]) != STATUS_PENDING:
            return {"ok": False, "error": "这个账号没有待验证的绑定，先让管理员生成验证码。"}
        if float(row["expires_ts"] or 0) <= now:
            con.execute("UPDATE owner_bindings SET status=?, code=''"
                        " WHERE platform=? AND account=?",
                        (STATUS_REVOKED, p, a))
            con.commit()
            return {"ok": False, "error": "验证码已过期，请让管理员重新生成。"}
        if not secrets.compare_digest(str(row["code"] or ""), entered):
            tries = int(row["tries"] or 0) + 1
            left = MAX_TRIES - tries
            if left <= 0:
                con.execute("UPDATE owner_bindings SET status=?, code='', tries=?"
                            " WHERE platform=? AND account=?",
                            (STATUS_REVOKED, tries, p, a))
                con.commit()
                return {"ok": False,
                        "error": "验证码错误次数过多，本次绑定作废，请让管理员重新生成。"}
            con.execute("UPDATE owner_bindings SET tries=? WHERE platform=? AND account=?",
                        (tries, p, a))
            con.commit()
            return {"ok": False, "error": f"验证码不对，还可以试 {left} 次。"}
        con.execute("UPDATE owner_bindings SET status=?, code='', verified_ts=?,"
                    " verified_by=? WHERE platform=? AND account=?",
                    (STATUS_VERIFIED, now, via, p, a))
        con.commit()
    return {"ok": True, "platform": p, "account": a}


def observe(platform: str, account: str, note: str = "",
            now: Optional[float] = None) -> bool:
    """模型/流程侧只能记「观察到像主人」：已 verified 或仍在等码的一律不动。"""
    p, a = _norm(platform, account)
    if not _valid(p, a):
        return False
    now = time.time() if now is None else float(now)
    with _lock:
        con = _db()
        row = con.execute(
            "SELECT status FROM owner_bindings WHERE platform=? AND account=?",
            (p, a)).fetchone()
        if row and str(row["status"]) in (STATUS_VERIFIED, STATUS_PENDING):
            return False
        con.execute(
            "INSERT INTO owner_bindings(platform,account,status,code,tries,"
            " created_ts,expires_ts,verified_ts,verified_by,note)"
            " VALUES(?,?,?, '',0,?,0,0,'',?)"
            " ON CONFLICT(platform,account) DO UPDATE SET"
            " status=excluded.status, note=excluded.note, created_ts=excluded.created_ts",
            (p, a, STATUS_OBSERVED, now, str(note or "")[:60]))
        con.commit()
    return True


def revoke(platform: str, account: str) -> bool:
    p, a = _norm(platform, account)
    with _lock:
        con = _db()
        cur = con.execute("DELETE FROM owner_bindings WHERE platform=? AND account=?",
                          (p, a))
        con.commit()
    return bool(cur.rowcount)


def listing() -> list[dict]:
    with _lock:
        rows = _db().execute(
            "SELECT platform,account,status,created_ts,verified_ts,verified_by,note"
            " FROM owner_bindings ORDER BY status, platform, account").fetchall()
    return [dict(r) for r in rows]


def _status_cn(status: str) -> str:
    return {"verified": "已验证", "pending": "待回码", "observed": "仅观察",
            "revoked": "已作废"}.get(status, status or "未知")


def handle_command(text: str, platform: str, account: str,
                   is_group: bool, is_admin: bool) -> Optional[str]:
    """/owner 命令；不是本命令返回 None。"""
    match = _CMD.match((text or "").strip())
    if not match:
        return None
    arg = (match.group(1) or "").strip()
    if not arg:
        return _HELP
    head, _, rest = arg.partition(" ")
    sub = head.lower()
    if sub in ("who", "我"):
        status = status_of(platform, account)
        if status == STATUS_VERIFIED:
            return f"你是已验证的主人账号（{platform}:{account}）。"
        if status == STATUS_OBSERVED:
            return ("我观察到你可能和主人有关，但这只是观察，还没有验证过——"
                    "让管理员发起绑定后，你在私聊回验证码才算数。")
        return "这个账号还没有绑定主人身份。"
    if sub == "verify":
        if is_group:
            return "验证码请在和我的私聊里发，群里人太多，不验证。"
        result = confirm(platform, account, rest.strip(), via=account)
        if not result.get("ok"):
            return str(result.get("error"))
        return "验证通过：这个账号已绑定为主人。"
    if not is_admin:
        return _HELP
    if sub in ("list", "列表"):
        rows = listing()
        if not rows:
            return "还没有任何绑定记录。"
        return "绑定记录：\n" + "\n".join(
            f"{r['platform']}:{r['account']} {_status_cn(r['status'])}"
            + ("（发起：" + r["verified_by"] + "）" if r.get("verified_by") else "")
            for r in rows[:30])
    if sub in ("bind", "绑定"):
        parts = rest.split()
        if len(parts) != 2:
            return "用法：/owner bind 平台 账号，例如 /owner bind qq 123456"
        result = begin(parts[0], parts[1], by=account)
        if not result.get("ok"):
            return str(result.get("error"))
        return (f"验证码：{result['code']}（10 分钟内有效）\n"
                f"请让 {result['platform']}:{result['account']} 在私聊里发 "
                f"/owner verify {result['code']}。")
    if sub in ("revoke", "解绑"):
        parts = rest.split()
        if len(parts) != 2:
            return "用法：/owner revoke 平台 账号"
        return f"已解绑 {parts[0]}:{parts[1]}。" if revoke(parts[0], parts[1]) \
            else f"没有找到 {parts[0]}:{parts[1]} 的绑定。"
    return _HELP
