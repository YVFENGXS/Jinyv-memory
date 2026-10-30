# -*- coding: utf-8 -*-
"""B站账号接入：扫码登录 + 弹幕监听 + 账号管理（评论 / 表情 / 头像 / 昵称）。

只读部分匿名即可；发弹幕、发评论、换头像、改昵称需要登录态（SESSDATA + bili_jct）。
凭据存本地 JSON（0600），不进日志、不进环境变量。
写操作会先补齐 buvid3/buvid4 与 bili_ticket，降低被 B站风控拦的概率；
风控拦下时如实返回接口消息，不假装成功。

环境变量：
  RP_BILI_ROOM      直播间号（也支持在聊天里 /bili room 设置）
  RP_BILI_ON        1/0 开关，默认 1（配了房间且有登录态才真正连）
  RP_BILI_NAMES     触发词，逗号分隔，默认「瑾瑜,jinyu」
  RP_BILI_GAP       两条弹幕最小间隔秒数，默认 20
  RP_BILI_DAILY     每天最多发几条，默认 60
  RP_BILI_CMT_DAILY 每天最多发几条视频评论，默认 3（0=不发）
  RP_BILI_CMT_GAP   两条评论最小间隔秒数，默认 600
  RP_BILI_CMT_CHARS 单条评论字数上限，默认 100
  RP_BILI_AUTH      登录态文件，默认 <数据目录>/bili_auth.json
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import logging
import os
import re
import time
import zlib
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("rp_memory")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
LIVE_API = "https://api.live.bilibili.com"
PASSPORT = "https://passport.bilibili.com"
API = "https://api.bilibili.com"
DANMU_MAX = 20                  # 普通账号弹幕上限，超了发不出去
UNAME_MAX = 16                 # B站昵称上限
EMOTE_FALLBACK = ("[doge]", "[笑哭]", "[妙啊]", "[鼓掌]", "[点赞]", "[喜欢]",
                  "[害羞]", "[思考]", "[哦呼]", "[嫌弃]", "[大笑]", "[微笑]")

_state: dict = {}
_path: Optional[Path] = None


# ---------- 配置 / 登录态 ----------
def init(path, room: str = "", owner_uid: str = "", owner_name: str = "") -> None:
    """打开登录态文件；没登录过也能先起监听（匿名只读）。

    owner_uid / owner_name：主人自己的B站账号。评论默认不 @ 任何人，
    只放行 @主人本人（避免四处点名）。
    """
    global _path, _state
    _path = Path(path).expanduser()
    _path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if _path.exists():
        try:
            data = json.loads(_path.read_text("utf-8") or "{}")
        except (OSError, ValueError):
            logger.warning("B站登录态文件损坏，按未登录处理 path=%s", _path)
            data = {}
    _state = {
        "sessdata": str(data.get("sessdata") or ""),
        "bili_jct": str(data.get("bili_jct") or ""),
        "uid": str(data.get("uid") or ""),
        "uname": str(data.get("uname") or ""),
        "refresh_token": str(data.get("refresh_token") or ""),
        "dedeuserid_ckmd5": str(data.get("dedeuserid_ckmd5") or ""),
        "buvid3": str(data.get("buvid3") or ""),
        "buvid4": str(data.get("buvid4") or ""),
        "b_nut": str(data.get("b_nut") or ""),
        "bili_ticket": str(data.get("bili_ticket") or ""),
        "bili_ticket_expires": float(data.get("bili_ticket_expires") or 0),
        "room": str(room or data.get("room") or ""),
        "on": bool(data.get("on", True)),
        "day": str(data.get("day") or ""),
        "sent": int(data.get("sent") or 0),
        "cmt_day": str(data.get("cmt_day") or ""),
        "cmt_sent": int(data.get("cmt_sent") or 0),
        "pause": str(data.get("pause") or ""),
        "pause_at": float(data.get("pause_at") or 0),
        "pause_code": str(data.get("pause_code") or ""),
        "pending": dict(data.get("pending") or {}),
        "owner_uid": str(owner_uid or data.get("owner_uid") or ""),
        "owner_name": str(owner_name or data.get("owner_name") or ""),
    }
    _state["_last_send"] = 0.0
    _state["_last_cmt"] = 0.0


def _save() -> None:
    if _path is None:
        return
    keep = {k: v for k, v in _state.items() if not k.startswith("_")}
    tmp = _path.with_suffix(".tmp")
    tmp.write_text(json.dumps(keep, ensure_ascii=False, indent=1), "utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(_path)


def ready() -> bool:
    """能监听（有房间且开关打开）。"""
    return bool(_state.get("room")) and bool(_state.get("on", True))


def can_send() -> bool:
    return bool(_state.get("sessdata") and _state.get("bili_jct"))


def owner() -> tuple[str, str]:
    """主人自己的B站账号（UID, 昵称）；没配置返回两个空串。"""
    return (str(_state.get("owner_uid") or ""),
            str(_state.get("owner_name") or ""))


# ---------- 写操作护栏：暂停 / 结果不明 / 身份核对 ----------
_RISK_CODES = {-352, -412, -429, -403, -509, -799, 403, 412, 429}
_RISK_WORDS = ("验证码", "风控", "频繁", "拒绝访问")


def _risk_reason(resp: dict) -> str:
    """风控类失败码/文案 → 暂停原因；普通失败返回空串。"""
    resp = resp or {}
    try:
        code = int(resp.get("code") or 0)
    except (TypeError, ValueError):
        code = 0
    msg = str(resp.get("message") or resp.get("msg") or "")
    if code in _RISK_CODES:
        return f"风控拦截（{code} {msg}）".strip()
    if any(w in msg for w in _RISK_WORDS):
        return f"风控拦截（{code} {msg}）".strip()
    return ""


def paused() -> tuple[bool, str]:
    """写操作是否暂停；返回 (是否暂停, 原因)。暂停只能人工 /bili unlock 解除。"""
    if _state.get("pause"):
        return True, (f"B站写操作已暂停：{_state.get('pause')}"
                      "（只能人工发 /bili unlock 解除）")
    if _state.get("pending"):
        return True, ("上一条评论结果不明（已记 pending，不自动重试）；"
                      "确认后发 /bili unlock 解除")
    return False, ""


def pause(reason: str, code="") -> None:
    """持久暂停所有写操作：风控、身份对不上、评论结果不明时调用。"""
    _state.update({"pause": str(reason or "风控拦截"),
                   "pause_at": time.time(), "pause_code": str(code or "")})
    _save()
    logger.warning("B站写操作已暂停 reason=%s code=%s", reason, code)


def unlock() -> str:
    """人工解除暂停并清 pending；返回给用户看的说明。"""
    old_pause = str(_state.get("pause") or "")
    old_pending = dict(_state.get("pending") or {})
    _state.update({"pause": "", "pause_at": 0, "pause_code": "", "pending": {}})
    _save()
    parts = []
    if old_pause:
        parts.append(f"原暂停原因：{old_pause}")
    if old_pending:
        parts.append(f"清掉的 pending：{str(old_pending.get('text') or '')[:30]}")
    if not parts:
        return "写操作本来就是通的，没有暂停或 pending。"
    return "已解除B站写操作暂停。" + "｜".join(parts)


def check_account() -> tuple[bool, str]:
    """写操作前核对登录身份：nav 的 mid 必须与本地 uid 一致，不一致就拒绝并暂停。"""
    if not can_send():
        return False, "未登录，先 /bili login 扫码"
    local = str(_state.get("uid") or "")
    info = nav_info()
    if info.get("err"):
        return False, "B站资料暂时查不到（网络问题），这次先不做写操作"
    if not info.get("login"):
        return False, "登录态已失效，重新 /bili login 扫码"
    remote = str(info.get("uid") or "")
    if local and remote and local != remote:
        pause(f"登录身份对不上（本地 UID {local}，线上 UID {remote}）", "uid_mismatch")
        return False, (f"登录身份对不上（本地 {local} / 线上 {remote}），"
                       "已暂停写操作，发 /bili unlock 核对后再试")
    if remote and not local:
        _state["uid"] = remote
        _save()
    return True, ""


def room() -> str:
    return str(_state.get("room") or "")


def my_uid() -> str:
    """登录账号的 UID；匿名时为空。用来跳过自己发出的弹幕，避免自问自答。"""
    return str(_state.get("uid") or "")


def status() -> str:
    if can_send():
        info = nav_info()
        if info.get("login"):
            login = (f"已登录 {info.get('uname') or _state.get('uname')}"
                     f"（UID {info.get('uid') or _state.get('uid')}，"
                     f"Lv.{info.get('level')}，硬币 {info.get('coins')}）")
        elif info.get("err"):
            login = "登录态暂未确认（B站接口不可达，稍后再 /bili status）"
        else:
            login = "登录态已失效，发 /bili login 重新扫码"
    else:
        login = "未登录（发 /bili login 扫码；看视频不受影响）"
    if ready():
        head = f"B站直播：房间 {room()}"
    else:
        head = "B站直播：未配置房间（发 /bili room 直播间号，仅直播用）"
    if _state.get("pause"):
        tail = f"｜写操作已暂停：{_state.get('pause')}（发 /bili unlock 解除）"
    elif _state.get("pending"):
        tail = "｜上一条评论结果不明（pending），发 /bili unlock 确认后再发"
    else:
        tail = ""
    oid, oname = owner()
    who = ""
    if oname or oid:
        who = "｜主人 " + (f"@{oname}" if oname else "") + (f"（UID {oid}）" if oid else "")
    return (f"{head}｜{login}｜今日弹幕 {_sent_today()} 条｜评论 "
            f"{_cmt_today()}/{comment_quota()} 条｜触发词 {','.join(_names())}{who}{tail}")


def set_room(text: str) -> tuple[bool, str]:
    """接受直播间号或 live.bilibili.com/xxx 链接。"""
    rid = resolve_room(text)
    if not rid:
        return False, "房间号要是数字，或直接贴直播间链接。"
    _state["room"] = rid
    _save()
    return True, f"直播间已设为 {rid}。"


def set_on(on: bool) -> None:
    _state["on"] = bool(on)
    _save()


def resolve_room(text: str) -> str:
    s = str(text or "").strip()
    m = re.search(r"live\.bilibili\.com/([0-9]+)", s)
    if m:
        return m.group(1)
    m = re.fullmatch(r"[0-9]{1,12}", s)
    return m.group(0) if m else ""


def _names() -> list:
    raw = os.environ.get("RP_BILI_NAMES") or "瑾瑜,jinyu"
    return [x.strip() for x in raw.split(",") if x.strip()]


def mentioned(text: str) -> bool:
    """被点名才回复：只看昵称/名字，不做语义猜测。"""
    t = str(text or "")
    return any(n.lower() in t.lower() for n in _names())


# ---------- 扫码登录 ----------
def login_begin() -> dict:
    """申请登录二维码：返回 {url, key} 或 {error}。"""
    import httpx
    try:
        with httpx.Client(timeout=10, headers={"User-Agent": UA,
                                               "Referer": "https://www.bilibili.com"}) as c:
            r = c.get(PASSPORT + "/x/passport-login/web/qrcode/generate")
            d = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return {"error": f"二维码申请失败：{type(e).__name__}"}
    data = d.get("data") or {}
    if d.get("code") != 0 or not data.get("qrcode_key"):
        return {"error": f"二维码申请失败：{d.get('message') or d.get('code')}"}
    return {"url": data.get("url") or "", "key": data.get("qrcode_key") or ""}


def login_poll(key: str) -> dict:
    """轮询扫码结果；成功时落登录态。

    返回 {state: waiting|scanned|ok|expired, msg}。
    凭据可能藏在回调 url 里，也可能只在 Set-Cookie 头里，两边都收。
    """
    import httpx
    if not key:
        return {"state": "expired", "msg": "二维码已失效，请重新 /bili login。"}
    try:
        with httpx.Client(timeout=10, headers={
                "User-Agent": UA, "Referer": "https://www.bilibili.com"}) as c:
            r = c.get(PASSPORT + "/x/passport-login/web/qrcode/poll",
                      params={"qrcode_key": key})
            d = r.json()
            jar = dict(c.cookies)
    except (httpx.HTTPError, ValueError) as e:
        return {"state": "waiting", "msg": f"查询失败：{type(e).__name__}"}
    if d.get("code") != 0:
        return {"state": "waiting", "msg": f"查询失败：{d.get('message')}"}
    data = d.get("data") or {}
    code = int(data.get("code") or 0)
    if code == 86101:
        return {"state": "waiting", "msg": ""}
    if code == 86090:
        return {"state": "scanned", "msg": "已扫码，请在手机上点确认。"}
    if code != 0:
        return {"state": "expired", "msg": "二维码已失效，请重新 /bili login。"}

    cookie = cookie_from_url(data.get("url") or "")
    for k, v in jar.items():                       # Set-Cookie 兜底
        cookie.setdefault(k, v)
    if not cookie.get("SESSDATA") or not cookie.get("bili_jct"):
        logger.warning("B站扫码回调缺凭据 keys=%s", sorted(cookie))
        return {"state": "expired", "msg": "登录返回缺凭据，请重试。"}
    return _finish_login(cookie, str(data.get("refresh_token") or ""))


def _finish_login(cookie: dict, refresh_token: str = "") -> dict:
    """落登录态并换取昵称；用 nav 验证登录真的生效。"""
    _state.update({
        "sessdata": str(cookie.get("SESSDATA") or ""),
        "bili_jct": str(cookie.get("bili_jct") or ""),
        "uid": str(cookie.get("DedeUserID") or _state.get("uid") or ""),
        "dedeuserid_ckmd5": str(cookie.get("DedeUserID__ckMd5")
                               or _state.get("dedeuserid_ckmd5") or ""),
        "refresh_token": refresh_token or _state.get("refresh_token") or "",
        "buvid3": str(cookie.get("buvid3") or _state.get("buvid3") or ""),
        "buvid4": str(cookie.get("buvid4") or _state.get("buvid4") or ""),
    })
    _ensure_fingerprint()
    info = nav_info()
    if info.get("login"):
        _state.update({"uid": info.get("uid") or _state.get("uid"),
                       "uname": info.get("uname") or _state.get("uname")})
        _save()
        logger.info("B站扫码登录成功 uname=%s uid=%s",
                    _state.get("uname"), _state.get("uid"))
        return {"state": "ok",
                "msg": f"登录成功：{info.get('uname') or 'B站账号'}。"
                       "现在可以发评论、发表情、换头像、改昵称。"}
    if info.get("err"):
        _save()
        logger.info("B站扫码登录成功（资料暂未查到）uid=%s", _state.get("uid"))
        return {"state": "ok",
                "msg": "登录成功（资料暂时没查到）。现在可以发评论、发表情、换头像、改昵称。"}
    return {"state": "expired", "msg": "扫码确认了，但 B站没认这个登录态，请重新 /bili login。"}


def cookie_from_url(url: str) -> dict:
    """登录成功回调 url 里的凭据 → {SESSDATA, bili_jct, DedeUserID...}。"""
    try:
        q = parse_qs(urlparse(str(url or "")).query)
    except ValueError:
        return {}
    return {k: v[0] for k, v in q.items() if v}


def _cookie_header() -> str:
    jar = {
        "SESSDATA": _state.get("sessdata", ""),
        "bili_jct": _state.get("bili_jct", ""),
        "DedeUserID": _state.get("uid", ""),
        "DedeUserID__ckMd5": _state.get("dedeuserid_ckmd5", ""),
        "buvid3": _state.get("buvid3", ""),
        "buvid4": _state.get("buvid4", ""),
        "b_nut": _state.get("b_nut", ""),
        "_uuid": _state.get("uuid", ""),
    }
    if (_state.get("bili_ticket")
            and float(_state.get("bili_ticket_expires") or 0) > time.time()):
        jar["bili_ticket"] = _state.get("bili_ticket")
        jar["bili_ticket_expires"] = str(int(_state.get("bili_ticket_expires") or 0))
    return "; ".join(f"{k}={v}" for k, v in jar.items() if v)


def cookie_header() -> str:
    """给同进程的读取路径复用登录态；未登录返回空串。"""
    return _cookie_header() if can_send() else ""


def _headers(referer: str = "https://www.bilibili.com/") -> dict:
    return {"User-Agent": UA, "Referer": referer,
            "Origin": "https://www.bilibili.com", "Cookie": _cookie_header()}


def _ensure_fingerprint() -> None:
    """补 buvid3/buvid4：没有设备指纹时 B站写接口容易直接风控。"""
    if _state.get("buvid3"):
        return
    import httpx
    try:
        with httpx.Client(timeout=10, headers=_headers()) as c:
            d = c.get(API + "/x/frontend/finger/spi").json()
    except (httpx.HTTPError, ValueError):
        return
    data = d.get("data") or {}
    b3 = str(data.get("b_3") or "")
    b4 = str(data.get("b_4") or "")
    if not b3:
        return
    _state.update({"buvid3": b3, "buvid4": b4,
                   "b_nut": str(int(time.time() * 1000))})
    _save()


def _ensure_ticket() -> None:
    """补 bili_ticket（3 天有效）：B站网页写操作的常见风控前置。"""
    exp = float(_state.get("bili_ticket_expires") or 0)
    if _state.get("bili_ticket") and exp > time.time() + 3600:
        return
    _ensure_fingerprint()
    ts = int(time.time())
    sign = hmac.new(b"XgwSnGZ1p", f"ts{ts}".encode(), hashlib.sha256).hexdigest()
    import httpx
    try:
        with httpx.Client(timeout=10, headers=_headers()) as c:
            d = c.post(API + "/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket",
                       params={"key_id": "ec02", "hexsign": sign,
                               "context[ts]": ts,
                               "csrf": _state.get("bili_jct", "")}).json()
    except (httpx.HTTPError, ValueError):
        return
    data = d.get("data") or {}
    ticket = str(data.get("ticket") or "")
    if not ticket:
        return
    ttl = int(data.get("ttl") or 0)
    created = int(data.get("created_at") or ts)
    _state.update({"bili_ticket": ticket,
                   "bili_ticket_expires": float(created + ttl if ttl else ts + 3 * 86400)})
    _save()


def _warmup() -> None:
    _ensure_fingerprint()
    _ensure_ticket()


def nav_info() -> dict:
    """登录账号的实时资料：昵称/UID/等级/硬币/头像；失败带 err，不抛异常。"""
    info = {"login": False, "uname": _state.get("uname") or "",
            "uid": _state.get("uid") or "", "level": 0, "coins": 0, "face": ""}
    if not can_send():
        return info
    _ensure_fingerprint()
    import httpx
    try:
        with httpx.Client(timeout=10, headers=_headers()) as c:
            d = c.get(API + "/x/web-interface/nav").json()
    except (httpx.HTTPError, ValueError):
        info["err"] = "网络"
        return info
    data = d.get("data") or {}
    info.update({
        "login": bool(d.get("code") == 0 and data.get("isLogin")),
        "uname": str(data.get("uname") or ""),
        "uid": str(data.get("mid") or _state.get("uid") or ""),
        "level": int((data.get("level_info") or {}).get("current_level") or 0),
        "coins": int((data.get("wallet") or {}).get("bcoin_balance") or 0),
        "face": str(data.get("face") or ""),
    })
    return info


def account_info() -> dict:
    """改昵称前要读到的现有资料（签名/性别/生日）。"""
    if not can_send():
        return {}
    import httpx
    try:
        with httpx.Client(timeout=10, headers=_headers()) as c:
            d = c.get(API + "/x/member/web/account").json()
    except (httpx.HTTPError, ValueError) as e:
        return {"error": f"网络：{type(e).__name__}"}
    if d.get("code") != 0:
        return {"error": str(d.get("message") or d.get("code"))}
    return d.get("data") or {}


_emote_cache = {"ts": 0.0, "texts": list(EMOTE_FALLBACK)}


def emote_texts(limit: int = 24) -> list:
    """B站评论区可用表情的文本写法（如 [doge]）；接口失败时给常用兜底。"""
    now = time.time()
    if now - float(_emote_cache["ts"]) < 6 * 3600:
        return list(_emote_cache["texts"])[:limit]
    if not can_send():
        return list(EMOTE_FALLBACK)[:limit]
    import httpx
    texts: list = []
    try:
        with httpx.Client(timeout=10, headers=_headers()) as c:
            d = c.get(API + "/x/emote/user/panel/web",
                      params={"business": "reply"}).json()
        for pkg in ((d.get("data") or {}).get("packages") or []):
            for em in (pkg.get("emote") or []):
                text = str(em.get("text") or "")
                if text and text not in texts:
                    texts.append(text)
    except (httpx.HTTPError, ValueError):
        texts = []
    if texts:
        _emote_cache.update(ts=now, texts=texts)
    else:
        texts = list(EMOTE_FALLBACK)
    return texts[:limit]


def clean_uname(text: str) -> str:
    """昵称清洗：去控制符/零宽字符，压空白，按 B站上限截断。"""
    t = re.sub(r"[\u0000-\u001f\u200b-\u200f\u202a-\u202e\u2066-\u2069]",
               "", str(text or ""))
    t = re.sub(r"\s+", " ", t).strip()
    return t[:UNAME_MAX]


def whoami(cookie: dict) -> tuple[str, str]:
    """用登录态换昵称；失败只返回 uid，不拦登录。"""
    import httpx
    uid = str(cookie.get("DedeUserID") or "")
    if not cookie.get("SESSDATA"):
        return "", uid
    try:
        with httpx.Client(timeout=10, headers={
                "User-Agent": UA, "Referer": "https://www.bilibili.com",
                "Cookie": f"SESSDATA={cookie['SESSDATA']}; bili_jct={cookie.get('bili_jct','')}"
                          f"; DedeUserID={uid}"}) as c:
            r = c.get("https://api.bilibili.com/x/web-interface/nav")
            d = r.json()
        data = d.get("data") or {}
        return str(data.get("uname") or ""), str(data.get("mid") or uid)
    except (httpx.HTTPError, ValueError):
        return "", uid


def qr_png(url: str) -> Optional[bytes]:
    """二维码渲染成 PNG；没装 qrcode 时返回 None，由调用方降级发链接。"""
    try:
        import qrcode
    except ImportError:
        return None
    try:
        import io
        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf)
        return buf.getvalue()
    except Exception:                                   # noqa: BLE001 渲染失败就降级
        return None


# ---------- 弹幕收发 ----------
def _sent_today() -> int:
    today = time.strftime("%Y-%m-%d")
    if _state.get("day") != today:
        _state["day"], _state["sent"] = today, 0
    return int(_state.get("sent") or 0)


def clip_reply(text: str) -> str:
    """模型回复 → 一条能发出去的弹幕：去标记、去换行、按上限截断。"""
    t = re.sub(r"\[\[[^\]]*\]\]", "", str(text or ""))
    t = re.sub(r"[\r\n]+", " ", t).strip().strip("“”\"'")
    return t[:DANMU_MAX]


def may_send() -> tuple[bool, str]:
    """先判节流再生成回复，避免白白跑一轮前台模型。"""
    if not can_send():
        return False, "未登录，先 /bili login"
    blocked, why = paused()
    if blocked:
        return False, why
    if not room():
        return False, "未设置直播间"
    gap = max(0.0, float(os.environ.get("RP_BILI_GAP") or 20))
    daily = max(0, int(float(os.environ.get("RP_BILI_DAILY") or 60)))
    if gap and time.time() - float(_state.get("_last_send") or 0) < gap:
        return False, "发送太快，已节流"
    if daily and _sent_today() >= daily:
        return False, "今日弹幕额度已用完"
    return True, ""


def send_danmu(text: str) -> tuple[bool, str]:
    """以登录账号发一条弹幕；失败返回原因，不抛异常。"""
    ok, why = may_send()
    if not ok:
        return False, why
    rid = room()
    msg = clip_reply(text)
    if not msg:
        return False, "内容为空"
    import httpx
    now = time.time()
    try:
        with httpx.Client(timeout=12, headers={
                "User-Agent": UA, "Referer": f"https://live.bilibili.com/{rid}",
                "Cookie": _cookie_header()}) as c:
            r = c.post(LIVE_API + "/msg/send", data={
                "bubble": "0", "color": "16777215", "fontsize": "25", "mode": "1",
                "msg": msg, "rnd": str(int(now * 1000000)), "roomid": rid,
                "csrf": _state.get("bili_jct", ""), "csrf_token": _state.get("bili_jct", "")})
            d = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return False, f"发送失败：{type(e).__name__}"
    if d.get("code") != 0:
        logger.warning("B站弹幕发送失败 room=%s code=%s msg=%s",
                       rid, d.get("code"), d.get("message"))
        reason = _risk_reason(d)
        if reason:
            pause(f"弹幕{reason}", d.get("code"))
            return False, f"弹幕被拦下：{reason}。写操作已暂停，发 /bili unlock 解除。"
        return False, f"发送失败：{d.get('message') or d.get('code')}"
    _state["_last_send"] = now
    _state["sent"] = _sent_today() + 1
    _save()
    return True, msg


# ---------- 视频评论区 ----------
def comment_quota() -> int:
    return max(0, int(float(os.environ.get("RP_BILI_CMT_DAILY") or 3)))


def _cmt_today() -> int:
    today = time.strftime("%Y-%m-%d")
    if _state.get("cmt_day") != today:
        _state["cmt_day"], _state["cmt_sent"] = today, 0
    return int(_state.get("cmt_sent") or 0)


def comment_allowed() -> tuple[bool, str]:
    """发评论前先判：暂停、登录、每日额度、间隔；@ 由 clean_comment 统一剥掉。"""
    blocked, why = paused()
    if blocked:
        return False, why
    if not can_send():
        return False, "未登录，先 /bili login 扫码"
    quota = comment_quota()
    if quota <= 0:
        return False, "评论功能已关闭（RP_BILI_CMT_DAILY=0）"
    if _cmt_today() >= quota:
        return False, f"今日评论额度已用完（{quota} 条）"
    gap = max(0.0, float(os.environ.get("RP_BILI_CMT_GAP") or 600))
    if gap and time.time() - float(_state.get("_last_cmt") or 0) < gap:
        return False, "评论发得太勤，先缓一缓"
    return True, ""


def clean_comment(text: str, limit: int = 0) -> str:
    """评论正文：去标记/换行，按字数上限截断。

    默认不 @ 任何人，只放行 @主人本人（昵称来自 RP_BILI_OWNER_NAME）。
    B站要把「@昵称」后面留一个空格才会识别成提及，所以恢复时补空格。
    """
    try:
        limit = limit or int(float(os.environ.get("RP_BILI_CMT_CHARS") or 100))
    except (TypeError, ValueError):
        limit = 100
    t = re.sub(r"\[\[[^\]]*\]\]", "", str(text or ""))
    name = str(_state.get("owner_name") or "").strip()
    tok = "\x00OWNER\x00"
    if name:
        t = re.sub(r"@" + re.escape(name), tok, t)   # 先保护主人的 @
    t = re.sub(r"@[^\s@]{1,24}", "", t)               # 其余 @ 一律去掉
    t = re.sub(r"[\r\n]+", " ", t)
    t = re.sub(r"[ \t]+", " ", t).strip().strip("“”\"'")
    if name:
        # 恢复成「@昵称 + 空格」；落在句尾时也保留这个空格，B站才认成提及
        t = re.sub(r"\x00OWNER\x00\s*", f"@{name} ", t).strip()
        if t.endswith("@" + name):
            t = t + " "
    return t[:max(20, limit)]


def _drop_owner(text: str) -> str:
    """去掉正文里的 @主人，用于笔记标题这种短字段。"""
    t = str(text or "")
    name = str(_state.get("owner_name") or "").strip()
    if name:
        t = re.sub(r"@" + re.escape(name) + r"\s*", "", t)
    return re.sub(r"[ \t]+", " ", t).strip()


def readback_comment(oid: int, text: str, tries: int = 3) -> bool:
    """发完评论后回读评论区，按自身 mid + 正文匹配确认真的出现。

    读不到只代表结果不明（可能审核延迟/被折叠），不自动重发。
    """
    import httpx
    mid = str(_state.get("uid") or "")
    body = clean_comment(text)
    if not mid or not body:
        return False
    for i in range(max(1, tries)):
        try:
            with httpx.Client(timeout=12, headers=_headers()) as c:
                d = c.get(API + "/x/v2/reply", params={
                    "oid": str(oid), "type": "1", "pn": 1, "ps": 20,
                    "sort": "0", "nohot": "1"}).json()
        except (httpx.HTTPError, ValueError):
            d = {}
        for r in ((d.get("data") or {}).get("replies") or []):
            if str((r.get("member") or {}).get("mid") or "") != mid:
                continue
            msg = str((r.get("content") or {}).get("message") or "")
            if msg.strip() == body.strip():
                return True
        if i < tries - 1:
            time.sleep(0.8)
    return False


def _post_comment_net(aid: int, msg: str, bvid: str) -> dict:
    """真正发评论；网络异常也折成 dict，交给 _comment_result 统一处理。"""
    import httpx
    referer = f"https://www.bilibili.com/video/{bvid or 'BV1xx411c7mD'}"
    _warmup()
    try:
        with httpx.Client(timeout=12, headers=_headers(referer)) as c:
            r = c.post(API + "/x/v2/reply/add", data={
                "oid": str(aid), "type": "1", "message": msg, "plat": "1",
                "csrf": _state.get("bili_jct", ""),
                "csrf_token": _state.get("bili_jct", "")})
            return r.json() or {}
    except (httpx.HTTPError, ValueError) as e:
        return {"_net": f"评论失败：{type(e).__name__}"}


def _comment_result(d: dict, aid: int, msg: str, bvid: str) -> tuple[bool, str]:
    """评论接口返回 → 结果：风控暂停 / 回读核验 / 只有确认后才记额度。"""
    if d.get("_net"):
        return False, str(d["_net"])
    if d.get("code") != 0:
        logger.warning("B站评论发送失败 aid=%s code=%s msg=%s",
                       aid, d.get("code"), d.get("message"))
        reason = _risk_reason(d)
        if reason:
            pause(f"评论{reason}", d.get("code"))
            return False, f"评论被拦下：{reason}。写操作已暂停，发 /bili unlock 解除。"
        return False, f"评论失败：{d.get('message') or d.get('code')}"
    if not readback_comment(aid, msg):
        _state["pending"] = {"kind": "comment", "aid": aid, "text": msg,
                             "bvid": bvid, "at": time.time()}
        pause("评论回读不到，结果不明，不自动重试", "readback")
        return False, ("评论可能发出去了，但评论区回读不到，结果不明；"
                       "已记 pending 并暂停写操作，不自动重试，发 /bili unlock 人工确认。")
    _state["pending"] = {}
    _state["_last_cmt"] = time.time()
    _state["cmt_sent"] = _cmt_today() + 1
    _save()
    logger.info("B站评论已发送并回读确认 aid=%s chars=%d", aid, len(msg))
    return True, msg


def post_comment(aid: int, text: str, bvid: str = "") -> tuple[bool, str]:
    """在视频评论区发一条评论；失败返回原因，不抛异常。

    顺序：暂停检查 → 额度/间隔 → 身份核对 → 发送 → 回读核验。
    """
    ok, why = comment_allowed()
    if not ok:
        return False, why
    if not aid:
        return False, "拿不到视频 aid，发不了评论"
    msg = clean_comment(text)
    if not msg:
        return False, "评论内容为空"
    ok, why = check_account()
    if not ok:
        return False, why
    return _comment_result(_post_comment_net(aid, msg, bvid), aid, msg, bvid)


# ---------- 带图评论：视频笔记（普通评论接口不支持图片） ----------
NOTE_IMG_MAX = 8 * 1024 * 1024
NOTE_IMG_LIMIT = 5 * 1024 * 1024


def _image_ext(data: bytes) -> str:
    if data[:8].startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return ""


def note_image_bytes(data: bytes) -> tuple[bytes, str, str]:
    """笔记配图预处理：B站只收 jpg/png/gif 且 ≤5MB；webp/超限自动转 JPEG。"""
    if not data:
        return b"", "", "图片是空的"
    if len(data) > NOTE_IMG_MAX:
        return b"", "", "图片超过 8MB"
    ext = _image_ext(data)
    if ext and len(data) <= NOTE_IMG_LIMIT:
        return data, ext, ""
    try:
        from PIL import Image
    except ImportError:
        return (data, ext, "") if ext else (b"", "", "图片格式不支持（要 jpg/png/gif）")
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.seek(0)
            im = im.convert("RGB")
            w, h = im.size
            if max(w, h) > 1600:
                scale = 1600 / max(w, h)
                im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            for q in (88, 78, 68):
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=q, optimize=True)
                out = buf.getvalue()
                if len(out) <= NOTE_IMG_LIMIT:
                    return out, "jpg", ""
    except (OSError, ValueError):
        pass
    return b"", "", "图片读不了"


def note_content(text: str, image_url: str, width: int = 0) -> str:
    """视频笔记正文序列：图片 + 文字（B站要求 JSON 序列字符串）。"""
    parts = [{"insert": {"imageUpload": {"url": str(image_url or ""),
                                        "status": "done",
                                        "width": max(0, int(width or 0) - 2)}}},
             {"insert": "\n"}]
    body = str(text or "").strip()
    if body:
        parts.append({"insert": body})
        parts.append({"insert": "\n"})
    return json.dumps(parts, ensure_ascii=False)


def note_title(text: str, fallback: str = "") -> str:
    first = re.sub(r"\s+", " ", str(text or "")).strip()
    return (first[:20] or str(fallback or "").strip()[:20] or "看完视频随手记")


def upload_note_image(data: bytes) -> tuple[str, int, str]:
    """先传图到 B站图床，拿到笔记可用的 image_url。返回 (url, width, 错误)。"""
    img, ext, why = note_image_bytes(data)
    if not img:
        return "", 0, why
    import httpx
    csrf = _state.get("bili_jct", "")
    _warmup()
    mime = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif"}.get(ext, "image/jpeg")
    try:
        with httpx.Client(timeout=30, headers=_headers()) as c:
            r = c.post(API + "/x/dynamic/feed/draw/upload_bfs",
                       data={"category": "daily", "biz": "new_dyn", "csrf": csrf},
                       files={"file_up": (f"note.{ext}", img, mime)})
            d = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return "", 0, f"传图失败：{type(e).__name__}"
    if d.get("code") != 0:
        logger.warning("B站笔记传图失败 code=%s msg=%s", d.get("code"), d.get("message"))
        reason = _risk_reason(d)
        if reason:
            pause(f"笔记传图{reason}", d.get("code"))
            return "", 0, (f"传图被拦下：{reason}。写操作已暂停，发 /bili unlock 解除。")
        return "", 0, f"传图失败：{d.get('message') or d.get('code')}"
    item = d.get("data") or {}
    url = str(item.get("image_url") or "")
    if not url:
        return "", 0, "传图返回没带图片地址"
    return url, int(item.get("image_width") or 0), ""


def _add_note_net(aid: int, title: str, summary: str, content: str,
                  cont_len: int) -> dict:
    """创建公开笔记并挂到评论区（auto_comment=1）；网络异常折成 dict。"""
    import httpx
    _warmup()
    referer = f"https://www.bilibili.com/video/av{aid}"
    try:
        with httpx.Client(timeout=20, headers=_headers(referer)) as c:
            r = c.post(API + "/x/note/add", data={
                "oid": str(aid), "oid_type": "0", "title": title,
                "summary": summary, "content": content, "tags": "",
                "cls": "1", "from": "save", "cont_len": str(max(0, cont_len)),
                "platform": "web", "publish": "1", "auto_comment": "1",
                "csrf": _state.get("bili_jct", "")})
            return r.json() or {}
    except (httpx.HTTPError, ValueError) as e:
        return {"_net": f"发笔记失败：{type(e).__name__}"}


def readback_note(oid: int, note_id: str, text: str, tries: int = 3) -> bool:
    """回读笔记内容，确认图片真的在正文里；读不到只代表结果不明。"""
    import httpx
    body = str(text or "").strip()
    for i in range(max(1, tries)):
        try:
            with httpx.Client(timeout=12, headers=_headers()) as c:
                d = c.get(API + "/x/note/info", params={
                    "oid": str(oid), "oid_type": "0",
                    "note_id": str(note_id)}).json()
        except (httpx.HTTPError, ValueError):
            d = {}
        data = d.get("data") or {}
        if d.get("code") == 0:
            content = str(data.get("content") or "")
            summary = str(data.get("summary") or "")
            if "imageUpload" in content or (body and body[:20] in content + summary):
                return True
        if i < tries - 1:
            time.sleep(0.8)
    return False


def _note_result(d: dict, aid: int, text: str, bvid: str) -> tuple[bool, str]:
    """笔记接口返回 → 结果：风控暂停 / 回读核验 / 只有确认后才记额度。"""
    if d.get("_net"):
        return False, str(d["_net"])
    if d.get("code") != 0:
        logger.warning("B站笔记发送失败 aid=%s code=%s msg=%s",
                       aid, d.get("code"), d.get("message"))
        reason = _risk_reason(d)
        if reason:
            pause(f"笔记{reason}", d.get("code"))
            return False, f"带图评论被拦下：{reason}。写操作已暂停，发 /bili unlock 解除。"
        code = d.get("code")
        if code == 79508:
            return False, "这个视频已经有笔记了，B站不允许重复添加。"
        if code == 79513:
            return False, "这个视频不支持笔记，换个视频或改发文字评论。"
        return False, f"发笔记失败：{d.get('message') or code}"
    note_id = str((d.get("data") or {}).get("note_id") or "")
    if not note_id or not readback_note(aid, note_id, text):
        _state["pending"] = {"kind": "note", "aid": aid, "note_id": note_id,
                             "text": text, "bvid": bvid, "at": time.time()}
        pause("笔记回读不到，结果不明，不自动重试", "readback")
        return False, ("带图笔记可能发出去了，但回读不到，结果不明；"
                       "已记 pending 并暂停写操作，不自动重试，发 /bili unlock 人工确认。")
    _state["pending"] = {}
    _state["_last_cmt"] = time.time()
    _state["cmt_sent"] = _cmt_today() + 1
    _save()
    logger.info("B站带图笔记已发送并回读确认 aid=%s note_id=%s", aid, note_id)
    return True, note_id


def post_note_comment(aid: int, text: str, image: bytes, bvid: str = "",
                      title: str = "") -> tuple[bool, str]:
    """带图评论：传图 → 建公开笔记(auto_comment=1) → 回读核验。

    和普通评论共用每日额度与暂停护栏。
    """
    ok, why = comment_allowed()
    if not ok:
        return False, why
    if not aid:
        return False, "拿不到视频 aid，发不了带图评论"
    if not image:
        return False, "没拿到图片，带图评论发不了"
    ok, why = check_account()
    if not ok:
        return False, why
    url, width, why = upload_note_image(image)
    if not url:
        return False, why
    body = clean_comment(text, 200)
    # 笔记标题不挂 @：标题只有 20 字，留给人话本身
    head = note_title(_drop_owner(title or body))
    summary = body[:80] or head
    content = note_content(body, url, width)
    return _note_result(_add_note_net(aid, head, summary, content, len(body)),
                        aid, body, bvid)


# ---------- 账号资料：头像 / 昵称 ----------
def avatar_bytes(data: bytes) -> tuple[Optional[bytes], str]:
    """头像预处理：居中裁 square 并缩到 512，统一转 JPEG。

    B站对非正方形的长图会自己裁，但裁到哪里不可控；先裁好再传，
    顺便把大图压到接口能接受的体积。Pillow 缺失时原样上传。
    """
    if not data:
        return None, "图片是空的"
    if len(data) > 8 * 1024 * 1024:
        return None, "图片超过 8MB"
    try:
        from PIL import Image
    except ImportError:
        return data, ""
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.seek(0)
            im = im.convert("RGBA")
            w, h = im.size
            side = min(w, h)
            left, top = (w - side) // 2, (h - side) // 2
            im = im.crop((left, top, left + side, top + side))
            im = im.resize((512, 512), Image.LANCZOS)
            bg = Image.new("RGB", (512, 512), (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            buf = io.BytesIO()
            bg.save(buf, "JPEG", quality=90)
            return buf.getvalue(), ""
    except Exception as exc:                       # noqa: BLE001 坏图/异常格式
        return None, f"图片读不了：{type(exc).__name__}"


def set_avatar(data: bytes) -> tuple[bool, str]:
    """换 B站头像；返回 (是否成功, 给用户看的话)。"""
    blocked, why = paused()
    if blocked:
        return False, why
    ok, why = check_account()
    if not ok:
        return False, why
    jpg, why = avatar_bytes(data)
    if jpg is None:
        return False, why
    import httpx
    _warmup()
    csrf = _state.get("bili_jct", "")
    try:
        with httpx.Client(timeout=30, headers=_headers()) as c:
            r = c.post(API + "/x/member/web/face/update",
                       params={"csrf": csrf},
                       data={"dopost": "save", "DisplayRank": "10000",
                             "csrf": csrf},
                       files={"face": ("avatar.jpg", jpg, "image/jpeg")})
            d = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return False, f"换头像失败：{type(e).__name__}"
    if d.get("code") != 0:
        logger.warning("B站换头像失败 code=%s msg=%s", d.get("code"), d.get("message"))
        reason = _risk_reason(d)
        if reason:
            pause(f"换头像{reason}", d.get("code"))
            return False, f"换头像被拦下：{reason}。写操作已暂停，发 /bili unlock 解除。"
        return False, f"换头像失败：{d.get('message') or d.get('code')}"
    logger.info("B站头像已更新 bytes=%d", len(jpg))
    return True, "头像已更新（B站可能要几分钟才刷新）"


def set_name(text: str) -> tuple[bool, str]:
    """改 B站昵称；B站规则是扣 6 枚硬币，硬币不足会直接拒绝。"""
    blocked, why = paused()
    if blocked:
        return False, why
    if not can_send():
        return False, "未登录，先 /bili login 扫码"
    new = clean_uname(text)
    if not new:
        return False, "昵称不能为空"
    ok, why = check_account()
    if not ok:
        return False, why
    info = account_info()
    if info.get("error"):
        return False, f"读账号资料失败：{info['error']}"
    if info.get("uname") == new:
        return False, "新昵称和现在的昵称一样，不用改"
    nav = nav_info()
    coins = int(nav.get("coins") or 0)
    if nav.get("login") and coins < 6:
        return False, f"B站改昵称要 6 枚硬币，现在只有 {coins} 枚"
    import httpx
    _warmup()
    payload = {
        "uname": new,
        "usersign": str(info.get("sign") or ""),
        "sex": str(info.get("sex") or "保密"),
        "csrf": _state.get("bili_jct", ""),
    }
    if info.get("birthday"):
        payload["birthday"] = str(info.get("birthday"))
    try:
        with httpx.Client(timeout=15, headers=_headers()) as c:
            d = c.post(API + "/x/member/web/update", data=payload).json()
    except (httpx.HTTPError, ValueError) as e:
        return False, f"改昵称失败：{type(e).__name__}"
    if d.get("code") != 0:
        logger.warning("B站改昵称失败 code=%s msg=%s", d.get("code"), d.get("message"))
        reason = _risk_reason(d)
        if reason:
            pause(f"改昵称{reason}", d.get("code"))
            return False, f"改昵称被拦下：{reason}。写操作已暂停，发 /bili unlock 解除。"
        return False, f"改昵称失败：{d.get('message') or d.get('code')}"
    _state["uname"] = new
    _save()
    logger.info("B站昵称已更新 uname=%s", new)
    return True, f"昵称已改为「{new}」（B站扣 6 枚硬币）"


def _pack(op: int, body: str) -> bytes:
    payload = body.encode("utf-8")
    return (len(payload).to_bytes(4, "big") + (16).to_bytes(2, "big")
            + (1).to_bytes(2, "big") + op.to_bytes(4, "big")
            + (1).to_bytes(4, "big") + payload)


def _unpack(blob: bytes) -> list:
    """拆包（含 zlib/brotli 压缩帧）；只关心 op=5 的 JSON 消息。"""
    out, i = [], 0
    while i + 16 <= len(blob):
        n = int.from_bytes(blob[i:i + 4], "big")
        h = int.from_bytes(blob[i + 4:i + 6], "big") or 16
        pv = int.from_bytes(blob[i + 6:i + 8], "big")
        op = int.from_bytes(blob[i + 8:i + 12], "big")
        body = blob[i + h:i + n]
        i += max(n, 16)
        if op != 5:
            continue
        if pv == 2:
            try:
                import brotli
                body = brotli.decompress(body)
            except Exception:                           # noqa: BLE001 没装 brotli 就跳过压缩帧
                continue
        elif pv == 1:
            try:
                body = zlib.decompress(body)
            except zlib.error:
                continue
        try:
            d = json.loads(body)
        except (TypeError, ValueError):
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def parse_danmu(msg: dict) -> Optional[dict]:
    """DANMU_MSG → {uid, uname, text}；其它消息返回 None。"""
    if not isinstance(msg, dict) or str(msg.get("cmd") or "") != "DANMU_MSG":
        return None
    info = msg.get("info") or []
    try:
        text = str(info[1] or "").strip()
        uid = str((info[2] or [""])[0])
        uname = str((info[2] or ["", ""])[1] or "").strip() or uid
    except (IndexError, TypeError):
        return None
    if not text:
        return None
    return {"uid": uid, "uname": uname, "text": text}


def danmu_info(rid: str) -> dict:
    """取弹幕服务器地址与鉴权 token。"""
    import httpx
    with httpx.Client(timeout=10, headers={
            "User-Agent": UA, "Referer": f"https://live.bilibili.com/{rid}",
            "Cookie": _cookie_header()}) as c:
        r = c.get(LIVE_API + "/xlive/web-room/v1/index/getDanmuInfo",
                  params={"id": rid, "type": 0})
        d = r.json()
    data = d.get("data") or {}
    if d.get("code") != 0 or not data.get("token"):
        raise RuntimeError(f"getDanmuInfo 失败：{d.get('message') or d.get('code')}")
    hosts = data.get("host_list") or []
    if not hosts:
        raise RuntimeError("getDanmuInfo 无可用弹幕服务器")
    host = hosts[0]
    return {"room_id": str(data.get("room_id") or rid), "token": data["token"],
            "url": f"wss://{host['host']}/sub"}


async def listen(on_danmu, stop: Optional[asyncio.Event] = None,
                 on_open=None) -> None:
    """连上直播间弹幕流，逐条回调；断线由调用方负责重连。"""
    import websockets

    info = danmu_info(room())
    stop = stop or asyncio.Event()
    async with websockets.connect(info["url"], ping_interval=None,
                                  additional_headers={"User-Agent": UA}) as ws:
        await ws.send(_pack(7, json.dumps({
            "uid": int(_state.get("uid") or 0), "roomid": int(info["room_id"]),
            "protover": 2, "platform": "web", "type": 2, "key": info["token"]},
            ensure_ascii=False)))
        logger.info("B站弹幕已连接 room=%s", info["room_id"])
        if on_open is not None:
            await on_open()

        async def beat():
            while not stop.is_set():
                try:
                    await ws.send(_pack(2, "[object Object]"))
                except Exception:                       # noqa: BLE001 断线交给外层
                    return
                await asyncio.sleep(30)

        beat_task = asyncio.create_task(beat())
        try:
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                for d in _unpack(raw):
                    item = parse_danmu(d)
                    if item is not None:
                        await on_danmu(item)
        finally:
            beat_task.cancel()
