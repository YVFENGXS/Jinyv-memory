# -*- coding: utf-8 -*-
"""富消息渲染与贴纸库：把回复文本里的标记变成 OneBot v11 消息段。

支持的标记（写在回复正文里即可）：

    [[img:https://example.com/a.jpg]]   发送网络图片
    [[search:关键词]]                    服务端搜图（必应），逐张视觉校验后发合格结果
    [[sticker:标签]]                     从本地贴纸库按标签挑一张
    [[face:14]]                         原生 QQ 表情（数字 id，直传不校验）
    [[voice:要念的文字]]                 文字转语音后以语音发送
    [[voice:https://.../a.mp3]]          直接发送音频文件

贴纸库：RP_STICKER_DIR 下的图片文件 + tags.json（文件名 -> 标签列表）。
群友发的图片/商城表情可自动入库并用 DeepSeek 视觉模型打标（见 autosave_*）。

安全：
- 外链只允许 http/https；域名先做 DNS 解析，解析到内网/回环/链路本地地址一律拒绝（防 SSRF）。
- 贴纸库只读一层目录；下载内容限 15MB（RP_IMG_MAX_MB 可调）且按魔数判扩展名。
- 视觉判读前会把动图取首帧、超长边缩小并转 JPEG，避免大 GIF 被模型拒收。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html as html_mod
import ipaddress
import json
import logging
import os
import random
import re
import socket
import threading
import time
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from nonebot.adapters.onebot.v11 import Message, MessageSegment

logger = logging.getLogger("jinyu_memory.richmsg")

MARKER_RE = re.compile(r"\[\[(img|sticker|face|voice|search):(.+?)\]\]", re.S)
CTRL = re.compile(r"[\u0000-\u001f\u202a-\u202e\u2066-\u2069]")   # 控制符/bidi 清洗

IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
AUDIO_EXT = {".mp3", ".wav", ".ogg", ".silk", ".amr", ".flac", ".m4a"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


# QQ 的图片 CDN 常见 6~12MB 的 GIF，太小会把正常表情包挡在门外（RP_IMG_MAX_MB 可调）
DOWNLOAD_LIMIT = max(1, _env_int("RP_IMG_MAX_MB", 15)) * 1024 * 1024
BLOCKED_HOSTS = {"localhost", "localhost.localdomain", "metadata.google.internal"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_MAGIC = {b"\x89PNG": ".png", b"GIF8": ".gif", b"\xff\xd8\xff": ".jpg"}

_tag_cache: Tuple[float, Dict[str, int]] = (0.0, {})
# 贴纸库/配额是「读文件-改-写」；多个图片判读在线程池里并发完成，
# 没有这把锁会互相覆盖标签或把当日配额记错。
_STORE_LOCK = threading.RLock()


def rich_enabled() -> bool:
    return (os.environ.get("RP_RICH") or "1").strip() not in {"0", "false", "no", "off"}


def autosave_enabled() -> bool:
    return (os.environ.get("RP_AUTOSTICKER") or "1").strip() not in {"0", "false", "no", "off"}


def sticker_dir() -> Path:
    return Path(os.environ.get("RP_STICKER_DIR") or "/opt/qqbot/stickers").expanduser()


def tts_voice() -> str:
    return (os.environ.get("RP_TTS_VOICE") or "zh-CN-XiaoyiNeural").strip()


def _front_key() -> str:
    return (os.environ.get("RP_FRONT_KEY") or "").strip()


def _tag_model() -> str:
    return (os.environ.get("RP_TAG_MODEL") or "deepseek-flash").strip()


def _glm_key() -> str:
    return (os.environ.get("RP_GLM_KEY") or "").strip()


def _glm_model() -> str:
    return (os.environ.get("RP_GLM_MODEL") or "glm-5.3-flash").strip()


def _glm_base() -> str:
    return (os.environ.get("RP_GLM_BASE")
            or "https://open.bigmodel.cn/api/paas/v4").rstrip("/")


def _think_off() -> bool:
    """后台判读是否关闭思考（RP_BACK_THINKING=off）：省 token，也避免输出被思考截断。"""
    return (os.environ.get("RP_BACK_THINKING") or "").strip().lower() in {
        "off", "none", "0", "false", "no", "disabled"}


# ---------------------------------------------------------------- SSRF 防护

def host_is_public(host: str) -> bool:
    """域名解析后所有 IP 都必须是公网地址。解析失败按不通过处理。"""
    host = (host or "").lower().rstrip(".")
    if host in BLOCKED_HOSTS:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            return False
        addrs = {info[4][0] for info in infos}
    else:
        addrs = {str(ip)}
    if not addrs:
        return False
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            return False
    return True


def url_is_public(url: str) -> bool:
    try:
        u = urlparse(url)
    except ValueError:
        return False
    if u.scheme not in {"http", "https"} or not u.hostname:
        return False
    return host_is_public(u.hostname)


# ---------------------------------------------------------------- 贴纸库

def _tags_file() -> Path:
    return sticker_dir() / "tags.json"


def _load_tags() -> Dict[str, List[str]]:
    try:
        return json.loads(_tags_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_tags(data: Dict[str, List[str]]) -> None:
    global _tag_cache
    sticker_dir().mkdir(parents=True, exist_ok=True)
    tmp = _tags_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_tags_file())
    _tag_cache = (0.0, {})            # 标签变了，提示词里的清单立刻刷新


def add_sticker_bytes(data: bytes, tags: List[str]) -> Optional[Path]:
    """按内容 md5 入库；已存在则只合并标签。返回文件路径或 None。"""
    if not data or len(data) > DOWNLOAD_LIMIT:
        return None
    ext = ".bin"
    for magic, e in _MAGIC.items():
        if data.startswith(magic):
            ext = e
            break
    else:
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ext = ".webp"
        else:
            return None
    name = hashlib.md5(data).hexdigest()[:16] + ext
    tags = [t.strip() for t in tags if t.strip()][:12]
    with _STORE_LOCK:                 # 读 tags.json → 合并 → 写回，必须原子
        d = sticker_dir()
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        path = d / name
        if not path.exists():
            path.write_bytes(data)
        if tags:
            store = _load_tags()
            old = set(store.get(name, []))
            store[name] = sorted(old | set(tags))[:12]
            _save_tags(store)
    return path


def add_sticker_from_url(url: str, tags: List[str]) -> Optional[Path]:
    if not url_is_public(url):
        return None
    try:
        import httpx
        resp = httpx.get(url, headers={"User-Agent": UA}, timeout=20,
                         follow_redirects=True)
        if resp.status_code != 200:
            return None
        return add_sticker_bytes(resp.content, tags)
    except Exception:                     # noqa: BLE001
        return None


# ---------------------------------------------------------------- 感知去重
# 同一张表情包多次发送时，QQ 图床会重新编码（甚至换分辨率），字节 md5 不同
# 但画面一样，只按 md5 去重会往库里堆同一张图。入库前先算 8x8 差分哈希
# （dhash）找候选，再用 64x64 灰度平均差复核，确认同图就复用已有文件。
PHASH_NEAR = 6          # dhash 汉明距离上限：重编码/缩放通常不超过 4
PHASH_MAE = 8.0         # 灰度平均差上限，排除「构图相似的不同图」


def _dhash(data: bytes) -> Optional[int]:
    """首帧灰度 8x8 差分哈希；动图只取首帧，解析失败返回 None。"""
    try:
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(data)) as im:
            im.seek(0)
            gray = im.convert("L").resize((9, 8), Image.LANCZOS).tobytes()
    except Exception:                     # noqa: BLE001
        return None
    bits = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            bits = (bits << 1) | (1 if gray[base + col] > gray[base + col + 1] else 0)
    return bits


def _gray64(data: bytes) -> Optional[bytes]:
    try:
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(data)) as im:
            im.seek(0)
            return im.convert("L").resize((64, 64), Image.LANCZOS).tobytes()
    except Exception:                     # noqa: BLE001
        return None


def _pixel_mae(a: bytes, b: bytes) -> float:
    if len(a) != len(b):
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _ensure_phashes() -> None:
    """给库里还没有 phash 的图片补齐（手工放进目录的图也能参与去重）。"""
    d = sticker_dir()
    if not d.is_dir():
        return
    store = _load_meta()
    changed = False
    for p in d.iterdir():
        if not p.is_file() or p.suffix.lower() not in IMG_EXT:
            continue
        if (store.get(p.name) or {}).get("phash"):
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        ph = _dhash(data)
        if ph is None:
            continue
        store.setdefault(p.name, {})["phash"] = f"{ph:016x}"
        changed = True
    if changed:
        _save_meta(store)


def _find_duplicate(data: bytes, ph: Optional[int]) -> Optional[str]:
    """找同一张图的已有文件（只是编码/分辨率不同），返回文件名或 None。"""
    if ph is None:
        return None
    d = sticker_dir()
    store = _load_meta()
    hits = []
    for name, m in store.items():
        raw = m.get("phash")
        if not raw or not (d / name).is_file():
            continue
        try:
            dist = bin(int(raw, 16) ^ ph).count("1")
        except ValueError:
            continue
        if dist <= PHASH_NEAR:
            hits.append((dist, name))
    if not hits:
        return None
    mine = None
    for _, name in sorted(hits)[:5]:
        try:
            other_data = (d / name).read_bytes()
        except OSError:
            continue
        if mine is None:
            mine = _gray64(data)
            if mine is None:
                return None
        other = _gray64(other_data)
        if other is not None and _pixel_mae(mine, other) <= PHASH_MAE:
            return name
    return None


def _is_usable(name: str, meta: Dict[str, dict]) -> bool:
    """贴纸是否允许发出：后台判读为不适用的、图中文字辱骂的一律拒绝。"""
    m = meta.get(name)
    if not m:
        return True                     # 老库还没有判读记录，先放行
    if m.get("blocked") or m.get("suitable") is False:
        return False
    return not text_is_blocked(m.get("text") or "")


def usable_stickers() -> List[Path]:
    """库中所有通过安全判读的贴纸文件。"""
    d = sticker_dir()
    if not d.is_dir():
        return []
    meta = _load_meta()
    return [p for p in d.iterdir()
            if p.is_file() and p.suffix.lower() in IMG_EXT
            and _is_usable(p.name, meta)]


def list_tags() -> Dict[str, int]:
    """标签 -> 可用贴纸数（不含被判为不当的），缓存 60s。"""
    global _tag_cache
    now = time.time()
    if now - _tag_cache[0] < 60:
        return _tag_cache[1]
    counts: Dict[str, int] = {}
    files = usable_stickers()
    names = {p.name for p in files}
    store = _load_tags()
    for name, tags in store.items():
        if name not in names:
            continue
        for t in tags:
            counts[t] = counts.get(t, 0) + 1
    for p in files:
        if p.name not in store:
            for t in re.split(r"[\s_\-.]+", p.stem):
                t = t.strip()
                if t and not t.isdigit():
                    counts[t] = counts.get(t, 0) + 1
    _tag_cache = (now, counts)
    return counts


# 同义情绪词：前台写"高兴"、库里标"开心"也要能选中，别因为措辞差异发不出贴纸。
_SYNONYM_GROUPS = [
    {"开心", "高兴", "快乐", "喜悦", "愉快", "兴奋", "笑"},
    {"无语", "无奈", "沉默", "呆滞", "愣住", "流汗"},
    {"生气", "愤怒", "恼火", "发火", "气"},
    {"难过", "伤心", "委屈", "哭", "流泪", "崩溃"},
    {"惊讶", "震惊", "吃惊", "诧异"},
    {"害羞", "脸红", "羞", "害臊"},
    {"可爱", "卖萌", "萌", "呆萌"},
    {"撒娇", "贴贴", "求抱抱", "亲亲"},
    {"嫌弃", "讨厌", "鄙视", "翻白眼", "敷衍"},
    {"疲惫", "累", "困", "没精神"},
    {"疑惑", "不解", "懵逼", "问号", "思考"},
    {"激动", "期待", "心动"},
    {"尴尬", "难为情", "社死"},
    {"傲娇", "嘴硬", "别扭"},
    {"睡觉", "赖床", "瞌睡", "困倦"},
]


def _related_tag(want: str, have: str) -> bool:
    """标签是否算同一类：互相包含，或落在同一个同义词组里。"""
    if not want or not have:
        return False
    if want in have or have in want:
        return True
    return any(want in g and have in g for g in _SYNONYM_GROUPS)


def pick_stickers(tag: str, n: int = 1,
                  allow_empty_random: bool = False) -> List[Path]:
    """按标签随机挑 n 张互不相同、通过安全判读的贴纸；没命中返回空列表。

    候选是「任一标签算同类」的并集（同义词组 + 互相包含），所以库里
    「可爱 / 卖萌 / 撒娇」这类重叠标签都会进池，每次随机取，不会总发同一张。
    """
    files = usable_stickers()
    if not files:
        return []
    tag = (tag or "").strip().lower()
    if not tag:
        hits = list(files) if allow_empty_random else []
    else:
        store = _load_tags()
        hits = [p for p in files
                if any(_related_tag(tag, t.lower()) for t in store.get(p.name, []))]
    random.shuffle(hits)
    return hits[:max(1, int(n))]


def pick_sticker(tag: str, allow_empty_random: bool = False) -> Optional[Path]:
    """按标签挑一张通过安全判读的贴纸；没有命中的标签返回 None（绝不乱发）。

    空标签只有管理员显式请求（/sticker 不带参数）时才随机；模型输出的空标记
    一律视为无匹配，避免「标签对不上就随便塞一张」。
    """
    got = pick_stickers(tag, 1, allow_empty_random)
    return got[0] if got else None


def list_stickers(tag: str = "") -> List[Tuple[str, List[str]]]:
    """列出贴纸（可按标签/文件名过滤）：[(文件名, 标签列表)]。"""
    d = sticker_dir()
    if not d.is_dir():
        return []
    store = _load_tags()
    out: List[Tuple[str, List[str]]] = []
    tag = (tag or "").strip().lower()
    for p in sorted(d.iterdir()):
        if not p.is_file() or p.suffix.lower() not in IMG_EXT:
            continue
        tags = store.get(p.name) or [t for t in re.split(r"[\s_\-.]+", p.stem) if t]
        if tag and tag not in [t.lower() for t in tags] and tag not in p.stem.lower():
            continue
        out.append((p.name, tags))
    return out


def delete_sticker(name: str) -> bool:
    """按文件名删除贴纸及其标签记录。文件名会先剥离路径部分。"""
    name = os.path.basename((name or "").strip())
    p = sticker_dir() / name
    with _STORE_LOCK:
        if not p.is_file():
            return False
        p.unlink()
        store = _load_tags()
        store.pop(name, None)
        _save_tags(store)
        meta = _load_meta()
        if meta.pop(name, None) is not None:
            _save_meta(meta)
    return True


_last_sticker: Dict[str, dict] = {}   # sid -> 这个会话最后发出去的一张贴纸


def _note_sticker(sid: str, path: Path) -> None:
    if not sid:
        return
    key = str(sid).split("#")[0]
    _last_sticker[key] = {
        "name": path.name,
        "tags": list(_load_tags().get(path.name) or []),
        "at": time.time(),
    }


def last_sticker(sid) -> Optional[dict]:
    """这个会话最后发出去的那张贴纸（文件名 + 标签）。"""
    return _last_sticker.get(str(sid or "").split("#")[0])


def forget_sticker(query: str = "", sid: str = "") -> Optional[Tuple[str, List[str]]]:
    """按标签/文件名删一张贴纸；query 为空就删本会话最后发的那张。

    一次只删一张：主人说「这张不要」时命中最近发的；说标签时优先删最近发过
    的同标签图，避免一次清空整类标签。
    """
    q = (query or "").strip().lower()
    d = sticker_dir()
    rec = last_sticker(sid)
    name = ""
    if not q:
        name = str((rec or {}).get("name") or "")
    elif os.path.basename(q) and (d / q).is_file():
        name = os.path.basename(q)
    else:
        hits = [n for n, tags in list_stickers()
                if q in n.lower() or q in [t.lower() for t in tags]]
        if rec and rec.get("name") in hits:
            name = str(rec["name"])
        elif hits:
            name = hits[0]
    if not name:
        return None
    tags = list(_load_tags().get(name) or [])
    if not delete_sticker(name):
        return None
    return name, tags


def add_tags(name: str, tags: List[str]) -> bool:
    name = os.path.basename((name or "").strip())
    tags = [t.strip() for t in tags if t.strip()]
    p = sticker_dir() / name
    with _STORE_LOCK:
        if not p.is_file() or not tags:
            return False
        store = _load_tags()
        cur = set(store.get(name, []))
        store[name] = sorted(cur | set(tags))[:12]
        _save_tags(store)
    return True


def remove_tag(name: str, tag: str) -> bool:
    name = os.path.basename((name or "").strip())
    with _STORE_LOCK:
        store = _load_tags()
        if name not in store:
            return False
        store[name] = [t for t in store[name] if t != (tag or "").strip()]
        _save_tags(store)
    return True


def fix_sticker(name: str, character=None, desc=None, tags=None):
    """纠正一张贴纸的判读结果：角色 / 描述 / 标签，并同步内存缓存。

    character 传空串表示「不确定是谁」，会把旧角色名从标签里一并删掉。
    返回 (文件名, 最终标签) 或 None（文件不存在）。
    """
    name = os.path.basename((name or "").strip())
    with _STORE_LOCK:
        store = _load_tags()
        meta_store = _load_meta()
        if name not in store and not (sticker_dir() / name).is_file():
            return None
        meta = meta_store.setdefault(name, {})
        tag_list = list(store.get(name) or meta.get("tags") or [])
        new = None
        if character is not None:
            new = CTRL.sub("", str(character)).strip()[:30]
            old = str(meta.get("character") or "").strip()
            meta["character"] = new
            if old and old in tag_list:
                tag_list.remove(old)
            if desc is None:
                # 描述里的旧角色名一并抹掉，否则下次引用又把它说回来。
                # 判读常写简称（character=初音未来，desc=初音），所以从长到短试前缀。
                body = str(meta.get("desc") or "")
                for n in range(len(old), max(1, len(old) - 3), -1):
                    cand = old[:n]
                    if cand and cand in body:
                        body = re.sub(r"\s{2,}", " ", body.replace(cand, "")).strip()
                        meta["desc"] = body
                        break
        if desc is not None:
            meta["desc"] = CTRL.sub("", str(desc)).strip()[:60]
        if tags is not None:
            tag_list = [CTRL.sub("", str(t)).strip()[:8] for t in tags
                        if str(t).strip()]
        if new and new not in tag_list:
            tag_list.append(new)
        meta["tags"] = tag_list[:12]
        _save_meta(meta_store)
        store[name] = tag_list[:12]
        _save_tags(store)
    hit = VISION_CACHE.get(name.split(".")[0])
    if hit is not None:                   # 同一张图再发时直接用纠正后的结果
        hit["character"] = str(meta.get("character") or "")
        if meta.get("desc"):
            hit["desc"] = meta["desc"]
        hit["tags"] = list(meta["tags"])
    return name, meta["tags"]


# ---------------------------------------------------------------- 视觉判读

# 图中文字命中即判为不可用贴纸：辱骂、人身攻击、性暗示。
ABUSE_RE = re.compile(
    r"傻[逼比]|煞笔|沙比|智障|弱智|脑残|去死|弄死|滚蛋|尼玛|你妈|他妈|草泥马|"
    r"妈的|婊子|贱人|骚货|约炮|做爱|强奸|裸体|艳照|黄图|操你|干你|日你|"
    r"打死你|砍你|nmsl|卧槽|我操|废物|垃圾东西|"
    r"fuck|shit|bitch", re.I)


def text_is_blocked(text: str) -> bool:
    """贴纸上的文字是否恶劣到不能发（辱骂/人身攻击/性暗示）。"""
    return bool(text and ABUSE_RE.search(text))


def _meta_file() -> Path:
    return sticker_dir() / "meta.json"


def _load_meta() -> Dict[str, dict]:
    """文件名 -> 视觉判读结果（desc/text/tags/suitable/blocked）。"""
    try:
        return json.loads(_meta_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_meta(data: Dict[str, dict]) -> None:
    sticker_dir().mkdir(parents=True, exist_ok=True)
    tmp = _meta_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_meta_file())


def _mime_of(data: bytes) -> str:
    """按魔数给 data URL 的真实 MIME；以前一律写 image/png，webp/gif 会被模型拒收。"""
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return "image/png"


_VISION_PROMPT = """这是QQ聊天里收到的一张图。仔细看，只输出一个 JSON 对象，不要任何解释：
{"kind":"表情包","character":"图中主要角色的名字","desc":"30字内写清画面：谁/什么、在做什么、什么表情","text":"图里可见的文字，逐字原样抄下来，没有就空字符串","tags":["3到6个中文短标签"],"suitable":true}
kind 也可以是 "图片"：照片、截图、聊天记录等需要认真回应的实拍内容，此时 tags 给 []。
硬性要求：
1. desc 必须写具体内容，禁止「一张图片」「一个表情包」「某个人物」这类空话；
2. character 填图中主要角色的名字：官方立绘、动画/游戏截图，以及同人图、换装、cosplay、
   梗图都算，只要认得出是哪个角色就填（这些不影响认人）。只写最出名的名字，多个角色
   只填最主要的一个；普通真人、纯风景/物件/截图填 ""；
   实在认不清是谁、或只是配色发型相似时，必须填 "" 并在 desc 末尾写明「认不清是谁」，
   不要硬猜一个名字；
3. text 必须把图中所有文字抄全，包括语气词、脏话，不要因为不雅而省略或打码；
   但密钥、密码、手机号、身份证号这类敏感字符串一律写成 <已隐藏>，不要原样抄；
   只抄看得见的字，不要根据上下文推测、改写、润色或补全句子；看不清的字写「[不清]」；
   文字特别多时（截图、文档、聊天记录）抄到 1500 字左右就停，末尾写「…（后略）」，
   停在句子结尾；1500 是上限不是任务，不要为了凑字数补写或编内容；
4. suitable 仅对表情包有意义：图中文字含辱骂、人身攻击、性暗示、脏话，或者画面模糊、
   属于一次性截图/私人隐私的，填 false；
5. tags 写情绪或使用场景（开心/无语/生气/撒娇/震惊/嫌弃 等），不要写「表情包」。"""


VISION_MAX_SIDE = 1280          # 视觉模型不需要原图分辨率，缩到这个长边足够看清文字
VISION_JPEG_QUALITY = 85
STICKER_TEXT_LIMIT = 60         # 表情包上的字：点睛的一两句，60 字足够
IMAGE_TEXT_LIMIT = 1500         # 截图/文档/聊天记录：整段要读，与视觉提示词的字数上限对齐
_VISION_RAW_LIMIT = 1_200_000   # 小于这个且非动图就原样送 PNG/JPEG，保留文字清晰度


def prepare_image_for_vision(data: bytes) -> bytes:
    """视觉前预处理：动图取首帧，最长边缩到 1280，必要时转 JPEG。

    大 GIF 的 base64 体积会超模型单图限制，直接送会被拒；Pillow 缺失或处理
    失败时回退原 bytes（调用方仍会得到一个明确错误，而不是卡死）。
    """
    if not data:
        return data
    try:
        from PIL import Image
    except ImportError:
        return data
    from io import BytesIO
    try:
        with Image.open(BytesIO(data)) as im:
            fmt = (im.format or "").upper()
            frames = int(getattr(im, "n_frames", 1) or 1)
            im.seek(0)
            need_resize = max(im.size) > VISION_MAX_SIDE
            need_convert = (frames > 1 or fmt in {"GIF", "BMP", "WEBP", "TIFF"}
                            or len(data) > _VISION_RAW_LIMIT)
            if not (need_resize or need_convert):
                return data
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                rgba = im.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[-1])
                out = bg
            else:
                out = im.convert("RGB")
            if need_resize:
                out.thumbnail((VISION_MAX_SIDE, VISION_MAX_SIDE), Image.LANCZOS)
            buf = BytesIO()
            out.save(buf, "JPEG", quality=VISION_JPEG_QUALITY, optimize=True)
            got = buf.getvalue()
            return got if got else data
    except Exception:                     # noqa: BLE001  损坏图/解压炸弹都回退原图
        return data


def _brief_text(value, limit: int = 600) -> str:
    """日志用：压成一行并截断，图片 base64 永远不进日志。"""
    text = " ".join(str(value or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _clip_text(text: str, limit: int) -> str:
    """按用途截断图上的字，并把「被截了」明说给前台，不让它当成全文念完。"""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…（后面还有 {len(text) - limit} 字，没展开）"


_glm_down_until = 0.0          # GLM 失败后的冷却，避免每张图都先白等一次
GLM_DOWN_SECONDS = 600.0       # 连不上时十分钟内直接走后台模型


def _run_with_deadline(func, seconds: float):
    """跑一个阻塞请求并设硬性总时限；超时返回 None（后台线程自行结束）。

    httpx 的 connect 超时是按单个 IP 算的，域名解析出多个 IP 时会被成倍放大，
    被墙的线路能拖到十几秒。这里用整体时限兜住，超时就当失败回退。
    """
    box = {}

    def runner():
        try:
            box["value"] = func()
        except Exception as exc:        # noqa: BLE001 留给调用方判断
            box["error"] = exc

    th = threading.Thread(target=runner, daemon=True)
    th.start()
    th.join(seconds)
    if th.is_alive():
        return None
    if "error" in box:
        raise box["error"]
    return box["value"]


def _glm_vision(data: bytes, prompt: str, label: str) -> Optional[str]:
    """GLM 优先判读；任何失败都返回 None，由 _vision_call 回退后台模型。"""
    global _glm_down_until
    key = _glm_key()
    if not key or time.time() < _glm_down_until:
        return None
    import httpx
    b64 = base64.b64encode(data).decode()
    payload = {
        "model": _glm_model(),
        "temperature": 0.1,
        "max_tokens": 4096,
        # 5.3-flash 始终思考，只能用 low 档压低延迟（关闭会直接 400）
        "thinking": {"type": "enabled", "level": "low"},
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": f"data:{_mime_of(data)};base64,{b64}"}},
        ]}],
    }
    url = f"{_glm_base()}/chat/completions"
    # 智谱忙时整段不回包，读超时给 25 秒：正常 6~12 秒出参，超时就尽快换后台模型
    timeout = httpx.Timeout(25.0, connect=8.0)

    def post():
        with httpx.Client(timeout=timeout) as client:
            return client.post(url, headers={"Authorization": f"Bearer {key}"},
                               json=payload)

    try:
        resp = _run_with_deadline(post, 30.0)
        if resp is None:
            raise TimeoutError("glm_total_timeout")
    except Exception as exc:            # noqa: BLE001 连不上就回退，别拖慢聊天
        _glm_down_until = time.time() + (
            120.0 if isinstance(exc, (TimeoutError, httpx.TimeoutException))
            else GLM_DOWN_SECONDS)
        logger.info("[jinyu] GLM 视觉不可用 用途=%s：%s，改用后台模型",
                    label, type(exc).__name__)
        return None
    if resp.status_code != 200:
        _glm_down_until = time.time() + (
            600.0 if resp.status_code in (400, 401, 403, 404) else 60.0)
        logger.info("[jinyu] GLM 视觉失败 用途=%s HTTP %s %s，改用后台模型",
                    label, resp.status_code, _brief_text(resp.text, 160))
        return None
    content, finish = "", ""
    try:
        body = resp.json()
        choice = (body.get("choices") or [{}])[0]
        finish = str(choice.get("finish_reason") or "")
        content = str((choice.get("message") or {}).get("content") or "").strip()
    except Exception:                   # noqa: BLE001 结构不对按失败处理
        content = ""
    if not content or finish == "length":      # 截断的 JSON 解析不了，直接回退
        _glm_down_until = time.time() + 60.0
        logger.info("[jinyu] GLM 视觉空结果 用途=%s finish=%s，改用后台模型",
                    label, finish or "-")
        return None
    logger.info("[jinyu] GLM 视觉出参 用途=%s 模型=%s %r",
                label, _glm_model(), _brief_text(content))
    return content


def _vision_call(data: bytes, prompt: str, label: str = "图片判读") -> str:
    """视觉请求：配了 GLM 就先问 GLM，失败再走后台模型。"""
    glm = _glm_vision(data, prompt, label)
    if glm:
        return glm
    key = _front_key()
    if not key:
        raise RuntimeError("no_key")
    import httpx
    base = (os.environ.get("RP_LLM_BASE") or "https://api.deepseek.com").rstrip("/")
    b64 = base64.b64encode(data).decode()
    want = 6000
    content = ""
    logger.info("[jinyu] 后台模型 视觉入参 用途=%s 模型=%s 提示词=%r",
                label, _tag_model(), _brief_text(prompt))
    for _ in range(3):
        payload = {
            "model": _tag_model(),
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:{_mime_of(data)};base64,{b64}"}},
            ]}],
            "max_tokens": want,
            "temperature": 0.1,
        }
        if _think_off():
            payload["thinking"] = {"type": "disabled"}
        resp = httpx.post(base + "/chat/completions",
                          headers={"Authorization": f"Bearer {key}"},
                          json=payload, timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")
        choice = (resp.json().get("choices") or [{}])[0]
        content = (choice.get("message", {}).get("content") or "").strip()
        finish = choice.get("finish_reason")
        if content and finish != "length":
            logger.info("[jinyu] 后台模型 视觉出参 用途=%s %r", label,
                        _brief_text(content))
            return content
        if finish == "length" and want < 16000:
            want *= 2
            continue
        if content:
            logger.info("[jinyu] 后台模型 视觉出参 用途=%s %r", label,
                        _brief_text(content))
            return content                   # 截断且预算到顶：交给调用方判错
        time.sleep(1.0)
    return content


def _json_obj(content: str) -> Optional[dict]:
    if not content:
        return None
    try:
        return json.loads(content)
    except ValueError:
        pass
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


VISION_CACHE: Dict[str, dict] = {}
ERROR_CACHE_SECONDS = 120     # 判读失败只短缓存：临时 429/超时不能变成永久「看不清」


def analyze_image(data: bytes) -> dict:
    """一张图的后台判读：类型、画面描述、图中文字、标签、是否可用。

    结果按内容 md5 缓存，并持久化进贴纸库 meta.json（重启后重发同一张不再调模型）。
    失败结果只保留短 TTL，避免一次网络抖动让这张图在进程生命周期内永远「没看清」。
    """
    md5 = hashlib.md5(data).hexdigest()[:16]
    cached = VISION_CACHE.get(md5)
    if cached is not None:
        if not cached.get("err") or time.time() - cached.get("_ts", 0) < ERROR_CACHE_SECONDS:
            return cached
    for name, m in _load_meta().items():      # 命中已入库的贴纸：直接用存档
        if name.startswith(md5):
            hit = {"kind": m.get("kind") or "表情包",
                   "desc": m.get("desc") or "", "text": m.get("text") or "",
                   "character": m.get("character") or "",
                   "tags": list(m.get("tags") or []),
                   "suitable": m.get("suitable", True) is not False,
                   "blocked": bool(m.get("blocked")
                                   or text_is_blocked(m.get("text") or "")),
                   "err": ""}
            VISION_CACHE[md5] = hit
            return hit

    out = {"kind": "图片", "desc": "", "text": "", "character": "", "tags": [],
           "suitable": True, "blocked": False, "err": "unparsed", "_ts": time.time()}
    try:
        content = _vision_call(prepare_image_for_vision(data), _VISION_PROMPT)
    except Exception as exc:                  # noqa: BLE001
        out["err"] = f"{type(exc).__name__}: {str(exc)[:80]}"
        VISION_CACHE[md5] = out
        return out
    parsed = _json_obj(content)
    if not isinstance(parsed, dict):
        out["err"] = f"no_json:{(content or '')[:60]!r}"
        VISION_CACHE[md5] = out
        return out

    kind = "表情包" if parsed.get("kind") == "表情包" else "图片"
    desc = CTRL.sub("", str(parsed.get("desc") or "")).strip()[:60]
    # 表情包上的字是点睛的一两句，60 字够；截图/文档/聊天记录是整段要读的，
    # 以前一律截到 60 字，长文就只能「念一半」，前台拿到的本来就是半截话。
    text = _clip_text(CTRL.sub("", str(parsed.get("text") or "")).strip(),
                      STICKER_TEXT_LIMIT if kind == "表情包" else IMAGE_TEXT_LIMIT)
    character = CTRL.sub("", str(parsed.get("character") or "")).strip()[:30]
    tags: List[str] = []
    for t in parsed.get("tags") or []:
        t = CTRL.sub("", str(t)).strip().strip("#。.!！?？")[:8]
        if t and t not in {"表情包", "图片", "未分类"} and t not in tags:
            tags.append(t)
    blocked = text_is_blocked(text)
    out = {"kind": kind, "desc": desc, "text": text, "character": character,
           "tags": tags[:6],
           "suitable": bool(parsed.get("suitable", True)) and not blocked,
           "blocked": blocked, "err": ""}
    VISION_CACHE[md5] = out
    return out


def _vision_tags(data: bytes) -> List[str]:
    """兼容旧调用：只取标签。"""
    return analyze_image(data)["tags"]


async def classify_image_bytes(data: bytes) -> dict:
    loop = asyncio.get_running_loop()
    md5 = hashlib.md5(data).hexdigest()[:16]
    if md5 in VISION_CACHE:
        return VISION_CACHE[md5]
    return await loop.run_in_executor(None, analyze_image, data)


def download_image(url: str) -> Optional[bytes]:
    """下载图片；失败返回 None（具体原因用 download_image_ex 取）。"""
    return download_image_ex(url)[0]


def download_image_ex(url: str) -> Tuple[Optional[bytes], str]:
    """下载图片：只允许公网 http(s)，限 DOWNLOAD_LIMIT，并复核重定向后的最终地址。

    返回 (字节, 失败原因)，成功时原因为空串。日志带上原因，才能区分
    「链接过期」「文件太大」「网络抖动」，不用再翻服务器抓包。
    借鉴 VimgFind 的做法：重定向后的地址重新校验，且 Content-Type 明显不是
    图片（HTML 错误页、JSON）时直接放弃，不把整页内容当图下载。
    """
    if not url or len(url) > 500:
        return None, "地址为空或过长"
    if not (url.startswith("http://") or url.startswith("https://")):
        return None, "只允许 http(s)"
    if not url_is_public(url):
        return None, "非公网地址"
    try:
        import httpx
        with httpx.stream("GET", url, headers={"User-Agent": UA},
                          timeout=25, follow_redirects=True) as resp:
            if resp.status_code != 200:
                return None, f"HTTP {resp.status_code}"
            if not host_is_public(resp.url.host):      # 重定向到内网：拒绝
                return None, "重定向到内网地址"
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            if ctype.startswith("text/") or ctype in ("application/json", "application/xml"):
                return None, f"非图片类型 {ctype}"
            buf = bytearray()
            for chunk in resp.iter_bytes():
                buf.extend(chunk)
                if len(buf) > DOWNLOAD_LIMIT:
                    return None, f"超过 {DOWNLOAD_LIMIT // (1024 * 1024)}MB"
            if not buf:
                return None, "空响应"
            return bytes(buf), ""
    except Exception as exc:                     # noqa: BLE001
        return None, f"{type(exc).__name__}: {str(exc)[:80]}"


def autosave_quota_left() -> int:
    """今天还能自动入库几张（防视觉调用费用失控；管理员不受此限）。"""
    daily = _env_int("RP_AUTOSTICKER_DAILY", 5)
    qf = sticker_dir() / ".quota.json"
    with _STORE_LOCK:
        try:
            q = json.loads(qf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            q = {}
    if q.get("date") != date.today().isoformat():
        return daily
    return max(0, daily - int(q.get("used", 0)))


def autosave_consume() -> None:
    daily = _env_int("RP_AUTOSTICKER_DAILY", 5)
    qf = sticker_dir() / ".quota.json"
    with _STORE_LOCK:
        try:
            q = json.loads(qf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            q = {}
        today = date.today().isoformat()
        used = int(q.get("used", 0)) + 1 if q.get("date") == today else 1
        qf.write_text(json.dumps({"date": today, "used": min(used, daily)}),
                      encoding="utf-8")


def _text_tags(text: str) -> List[str]:
    """图中文字折成标签：短整句直接用（如「我裂开了」），长句不硬塞。"""
    text = (text or "").strip()
    if not text or len(text) > 8:
        return []
    if re.search(r"[，。！？,.!?\n]", text):
        return []
    return [text]


def _record_meta(name: str, info: dict, tags: List[str],
                 phash: Optional[int] = None) -> None:
    """把判读结果写进 meta.json；已存在的图也要写，否则手工/重复入库的图永远没有审核记录。"""
    store = _load_meta()
    old = store.get(name) or {}
    store[name] = {"kind": info.get("kind") or "表情包",
                   "desc": info.get("desc") or "",
                   "text": info.get("text") or "",
                   "character": info.get("character") or "",
                   "tags": tags,
                   "suitable": True,
                   "blocked": False,
                   "phash": f"{phash:016x}" if phash is not None else (old.get("phash") or ""),
                   "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _save_meta(store)


def save_and_tag(data: bytes, extra_tags: Optional[List[str]] = None,
                 force: bool = False, info: Optional[dict] = None) -> Optional[Path]:
    """入库 + 视觉判读，并记录 desc/text/tags/suitable 供前台与筛选使用。

    判读失败、判定不适合（含辱骂文字/模糊/隐私）都丢弃不入库。
    force=True 跳过每日配额（管理员手动发的图），也不消耗自动收藏配额；
    info 传入已判读结果可省一次调用。
    """
    info = info or analyze_image(data)
    if info.get("blocked"):                # 脏话图任何人都不收
        return None
    if not force and (info.get("err") or not info.get("suitable", True)):
        return None
    md5 = hashlib.md5(data).hexdigest()[:16]
    ph = _dhash(data)
    with _STORE_LOCK:                      # 配额检查 → 入库 → 记 meta，必须原子
        d = sticker_dir()
        _ensure_phashes()
        known = any(p.stem == md5 for p in d.iterdir()) if d.is_dir() else False
        tags = list(info.get("tags") or []) \
            + ([info["character"]] if info.get("character") else []) \
            + _text_tags(info.get("text") or "") + list(extra_tags or [])
        tags = list(dict.fromkeys(t for t in tags if t))[:12]
        if known:
            path = add_sticker_bytes(data, tags)
            if path is not None:
                _record_meta(path.name, info, tags or ["未分类"], ph)
            return path
        dup = _find_duplicate(data, ph)
        if dup:
            # 同一张图只是换了编码/分辨率：复用旧文件，只并标签，不再落盘
            add_tags(dup, tags or ["未分类"])
            merged = _load_tags().get(dup) or tags or ["未分类"]
            _record_meta(dup, info, merged, ph)
            logger.info("[jinyu] 贴纸复用 %s（同图不同编码，未重复落盘）", dup)
            return d / dup
        if not force and autosave_quota_left() <= 0:
            return None
        path = add_sticker_bytes(data, tags or ["未分类"])
        if path is None:
            return None
        if not force:
            autosave_consume()
        _record_meta(path.name, info, tags or ["未分类"], ph)
        return path


# ---------------------------------------------------------------- 搜图

_BING_URL = "https://cn.bing.com/images/search?q={q}&form=HDRSC2&first=1"
_BAIDU_HOME = "https://image.baidu.com/"
_BAIDU_API = "https://image.baidu.com/search/acjson"
_BAIDU_PAGE = "https://image.baidu.com/search/index?tn=baiduimage&word={q}"
_BAIDU_WARM = ("https://www.baidu.com/", "https://image.baidu.com/")
_BAIDU_CACHE_TTL = 600          # 同一关键词 10 分钟内复用，别反复戳接口
_BAIDU_MIN_GAP = 1.5            # 两次接口请求的最小间隔，避免被判成爬虫
_baidu_session = {"client": None, "ts": 0.0}
_baidu_next = 0.0
_baidu_cache = {}
_baidu_lock = threading.Lock()
_MURL_RE = re.compile(r'murl&quot;:&quot;(https?://[^&"]+)')
_MURL_RE2 = re.compile(r'"murl"\s*:\s*"(https?://[^"]+)"')
# 真实结果项的锚点；顶部的联想词/相关搜索块不在 a.iusc 里，按结构取就不会
# 把「大肥 / 大群 / 大妹子」这些建议词当成搜索结果。先按原文取属性（内部引号
# 还是 &quot;），再反转义，避免 JSON 里的引号把属性提前截断
_IUSC_RE = re.compile(r'<a[^>]*\bclass="iusc"[^>]*\bm="([^"]*)"', re.S)


def _bing_image_urls(page: str) -> list[str]:
    """从必应图片页取真实结果直链：优先按 a.iusc 结构，结构变了再退回全文匹配。"""
    urls = []
    for raw in _IUSC_RE.findall(page):
        hit = _MURL_RE2.search(html_mod.unescape(raw))
        if hit:
            urls.append(hit.group(1))
    if urls:
        return urls
    blob = html_mod.unescape(page)
    return _MURL_RE2.findall(blob) + _MURL_RE.findall(page)


def _baidu_client(force: bool = False):
    """百度图片接口要 BAIDUID cookie：先访问首页拿一份，缓存一小时。

    只带 UA 时 acjson 会直接回 {"antiFlag":1,"message":"Forbid spider access"}；
    补上 Accept / Accept-Language，并确认首页真的发了 BAIDUID，才算建好会话。
    """
    now = time.time()
    with _baidu_lock:
        if force:
            cached = _baidu_session["client"]
            if cached is not None:
                cached.close()
            _baidu_session.update(client=None, ts=0.0)
        cached = _baidu_session["client"]
        if cached is not None and now - _baidu_session["ts"] < 3600:
            return cached
        try:
            import httpx
            client = httpx.Client(
                timeout=20, follow_redirects=True,
                headers={
                    "User-Agent": UA,
                    "Accept": "text/html,application/xhtml+xml,application/xml;"
                              "q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                })
            for warm in _BAIDU_WARM:       # 先百度主站拿 BIDUPSID/PSTM，再补图片站 cookie
                client.get(warm)
                if warm == _BAIDU_WARM[0] and not client.cookies.get("BAIDUID"):
                    break
        except Exception as exc:                # noqa: BLE001 拿不到就退回必应
            logger.info("百度图片会话建立失败：%s", exc)
            return None
        if not client.cookies.get("BAIDUID"):
            logger.info("百度图片会话没有拿到 BAIDUID，本轮改用必应")
            client.close()
            return None
        _baidu_session.update(client=client, ts=now)
        return client


def _baidu_image_urls(query: str) -> list[str]:
    """百度图片结果直链。

    用户给的那个 index 页面是 JS 渲染的空壳，真正的结果在它背后的 acjson
    接口里；这里带上同一组参数直接请求接口，省掉浏览器。同一关键词缓存 10
    分钟，两次请求至少隔 1.5 秒，尽量不触发风控；被拦就重建会话再试一次，
    仍失败返回空，由调用方退回必应。
    """
    global _baidu_next
    from urllib.parse import quote
    with _baidu_lock:
        hit = _baidu_cache.get(query)
        if hit and time.time() - hit[0] < _BAIDU_CACHE_TTL:
            return list(hit[1])
    params = {
        "tn": "resultjson_com", "ipn": "rj", "ct": "201326592",
        "is": "", "fp": "result", "fr": "", "word": query, "queryWord": query,
        "cl": "2", "lm": "-1", "ie": "utf-8", "oe": "utf-8", "st": "-1",
        "z": "", "ic": "0", "hd": "", "latest": "", "copyright": "",
        "s": "", "se": "", "tab": "", "width": "", "height": "", "face": "0",
        "istype": "2", "qc": "", "nc": "1", "expermode": "", "nojc": "",
        "pn": "0", "rn": "30", "gsm": "1e",
    }
    blocked = ""
    for attempt in range(2):
        client = _baidu_client(force=attempt > 0)
        if client is None:
            return []
        with _baidu_lock:
            wait = _baidu_next - time.time()
            _baidu_next = max(time.time(), _baidu_next) + _BAIDU_MIN_GAP
        if wait > 0:
            time.sleep(min(wait, 3.0))
        try:
            resp = client.get(_BAIDU_API, params=params,
                              headers={"Referer": _BAIDU_PAGE.format(q=quote(query))})
            data = resp.json()
        except Exception as exc:                # noqa: BLE001
            logger.info("百度图片接口失败 q=%r：%s", query, exc)
            return []
        if isinstance(data, dict) and data.get("antiFlag"):
            blocked = str(data.get("message") or "antiFlag")
            continue                            # 重建会话再试一次
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            logger.info("百度图片接口无结果 q=%r：%s", query, str(data)[:120])
            return []
        urls = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("adType") or "0") != "0":   # 广告位
                continue
            url = str(item.get("middleURL") or item.get("thumbURL") or "")
            if url.startswith("http") and url not in urls:
                urls.append(url)
        with _baidu_lock:
            if len(_baidu_cache) >= 64:         # 只留最近的，避免无限增长
                _baidu_cache.clear()
            _baidu_cache[query] = (time.time(), list(urls))
        return urls
    logger.info("百度图片接口被风控 q=%r：%s，本轮改用必应", query, blocked)
    return []


def _is_baidu_image(url: str) -> bool:
    """百度图片 CDN 的直链没有扩展名，不能按 .jpg/.png 过滤。"""
    host = (urlparse(url).hostname or "").lower()
    return host.endswith(".baidu.com") and "/it/u=" in url


# 素材/图库站常带水印，直链能过 SSRF 校验但不是表情包；先按域名跳过，省下载和判读
_STOCK_HOSTS = ("nipic.com", "58pic.com", "699pic.com", "veer.com",
                "shutterstock.com", "gettyimages", "dreamstime.com",
                "123rf.com", "alamy.com", "tuchong.com", "zcool.com.cn")
_STOCK_MARKS = ("昵图网", "摄图网", "千图网", "我图网", "包图网", "汇图网",
                "素材中国", "nipic", "shutterstock", "gettyimages", "水印")
SEARCH_MAX_CANDIDATES = 8      # 一条回复最多下载并判读 8 张（含换关键词重搜），避免拖住发消息
SEARCH_PER_VARIANT = 4         # 每个关键词变体先试 4 张，不合格再换下一个变体

# 搜图意图：只有明确写了「表情包/梗图」才走表情包通道，其余一律按普通图片搜。
# 「深圳 高楼 建筑」这种没说清类型的查询以前默认走表情包，必应返回的实拍大楼
# 会被画风门全部拦掉，一张都发不出来。
_STICKER_WORDS = re.compile(r"表情包|梗图|斗图|贴纸|沙雕图|meme", re.I)
_IMAGE_WORDS = re.compile(r"风景|风景照|照片|实拍|实景|壁纸|背景图|插画|原画|"
                          r"立绘|海报|截图|原图|高清图|图片|头像", re.I)


def _search_mode(query: str) -> str:
    """判断这次搜图要表情包（sticker）还是一般图片（image）。

    只有明确要表情包/梗图才走 sticker，其余（包括没写清楚的）都按普通图片搜。
    """
    q = str(query or "")
    if _STICKER_WORDS.search(q) and not _IMAGE_WORDS.search(q):
        return "sticker"
    return "image"


def _screen_candidate(data: bytes) -> str:
    """下载后的确定性预筛：过小图、超宽横幅、纯色卡先淘汰，省一次视觉调用。

    借鉴 VimgFind「在索引阶段就把垃圾图挡在门外」的思路：它的排除规则针对本地
    路径，这里换成对图片本身的像素统计。返回淘汰原因，空串表示通过。
    """
    try:
        from io import BytesIO
        from PIL import Image
    except ImportError:
        return ""
    try:
        with Image.open(BytesIO(data)) as im:
            im.seek(0)
            w, h = im.size
            if min(w, h) < 80:
                return f"尺寸过小 {w}x{h}"
            if max(w, h) / max(1, min(w, h)) > 4:
                return f"长宽比异常 {w}x{h}"
            small = im.convert("RGB").resize((32, 32))
            colors = small.getcolors(1024) or []
    except Exception as exc:                     # noqa: BLE001 解不开的图直接淘汰
        return f"无法解析 {type(exc).__name__}"
    if len(colors) <= 3:
        top = sum(n for n, _ in sorted(colors, reverse=True)[:3])
        if top / 1024 >= 0.98:                   # 98% 以上像素同色：纯色卡/渐变卡
            return "接近纯色"
    return ""


def _is_stock(url: str, info: Optional[dict] = None) -> bool:
    """图库素材站 / 带水印的图：不是表情包，直接淘汰。"""
    host = (urlparse(url).hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in _STOCK_HOSTS):
        return True
    if info:
        blob = f"{info.get('text') or ''} {info.get('desc') or ''}".lower()
        return any(m in blob for m in _STOCK_MARKS)
    return False


_SEARCH_CHECK_PROMPT = """这是为聊天搜到的一张候选图，主人在找「{q}」相关的内容。
仔细看，只输出一个 JSON：
{"ok":true,"kind":"照片","match":true,"desc":"60字内画面","text":"图中文字，没有就空串","why":"20字内"}
kind：照片 / 插画 / 壁纸 / 表情包 / 素材 / 其他。
ok=true 必须同时满足：
1. 画面里能看出「{q}」要的东西（要找深圳高楼，就得看得出城市高楼；
   要找表情包，就得有对应的角色、动作、情绪或文字）；
2. 不是图库素材样图、带水印、纯色卡、二维码、logo、图表、聊天截图；
3. 文字不含辱骂、人身攻击、性暗示。
图里有多个人物同框时，只要其中有一个符合就算对得上，不要因为画面主体是
一群人就判不合格；多角色合影、群像插画都属于合格情况。
desc 必须写具体：几个人物、各自发色和服饰、在做什么；单人也写发色、服饰、动作。
text 里遇到密钥、密码、手机号、身份证号这类敏感字符串，用 <已隐藏> 代替，不要原样抄。
找的是角色名（如「XX娘」）时，按发色、发型、服饰、标志物这些外观特征判断，
不需要你认识这个角色；图里有符合外观特征的角色就匹配。
match 只填 true/false：画面里有没有「{q}」要的东西。
画面风格不限：实拍、二次元、插画、卡通都可以，只看内容对不对。
确实没有符合的内容才填 false。"""


_QUERY_GENERIC = re.compile(r"动漫表情包|二次元|表情包|动漫|卡通|可爱|搞笑|沙雕|高清|壁纸|图片")
_QUERY_GENERIC_IMAGE = re.compile(
    r"风景照|风景|照片|实拍|实景|插画|原画|立绘|海报|截图|原图|高清图|图片|壁纸|头像")
_QUERY_LEAD = re.compile(r"^(?:深|浅|亮|暗)?(?:蓝|红|金|白|黑|粉|紫|绿|灰|棕|橙|黄)色?")


def _subject_score(query: str, info: dict, mode: str = "sticker") -> float:
    """确定性主体核对：搜「蓝鲸」不能返回粉发少女，模型判切题也要对得上字面主体。

    去掉「表情包/二次元/可爱」或「风景照/插画/图片」这类泛词和「蓝色/大」这类修饰后，
    剩下的词至少有一半能在画面描述里找到；找不到任何字（如蓝鲸→鲸）就判 0。
    """
    generic = _QUERY_GENERIC_IMAGE if mode == "image" else _QUERY_GENERIC
    terms = [t for t in re.sub(r"[\s\W_]+", " ",
                               generic.sub(" ", query or "")).split() if t]
    if not terms:
        return 1.0
    hay = " ".join([str(info.get("desc") or ""),
                    str(info.get("character") or ""),
                    str(info.get("text") or ""),
                    str(info.get("kind") or ""),
                    " ".join(info.get("tags") or [])])
    hit = 0.0
    for t in terms:
        if t in hay:
            hit += 1.0
            continue
        core = _QUERY_LEAD.sub("", t) or t
        if core != t and core in hay:
            hit += 1.0
            continue
        if any(ch in hay for ch in core):
            hit += 0.5
    return hit / len(terms)


def _search_check(data: bytes, query: str) -> Optional[dict]:
    """搜图候选的一次性判读：是否切题、是否可用。画风不设限。"""
    try:
        content = _vision_call(prepare_image_for_vision(data),
                               _SEARCH_CHECK_PROMPT.replace("{q}", query), label="搜图校验")
    except Exception:                     # noqa: BLE001 判不了就换下一张，宁可不发
        return None
    info = _json_obj(content)
    if not isinstance(info, dict):
        return None
    info["text"] = CTRL.sub("", str(info.get("text") or "")).strip()[:60]
    info["desc"] = CTRL.sub("", str(info.get("desc") or "")).strip()[:60]
    if (not info.get("ok") or info.get("match") is False
            or str(info.get("kind") or "") in ("素材", "其他")):
        info["ok"] = False
    if text_is_blocked(info["text"]):
        info["ok"] = False
    return info


def _search_variants(query: str, mode: str = "image") -> List[str]:
    """搜索词变体：表情包补「表情包」，普通图片原样搜。

    以前给表情包查询再追加「二次元 表情包」，必应会返回动漫插画而不是用户要的
    实拍图；现在不再自动加「二次元」，只在缺「表情包」时补一个词。
    """
    q = re.sub(r"\s+", " ", (query or "").strip())
    if not q:
        return []
    if mode == "image":
        return [q]
    q = re.sub(r"动漫(?=表情包)", "", q).strip()
    if not q:
        return []
    if "表情包" not in q:
        q += " 表情包"
    return [q]


def image_search(query: str, exclude=None) -> Optional[str]:
    """图片搜索，只要直链；exclude 里是已经发过的直链，跳过不重复。"""
    return _image_search(query, exclude)[0]


def image_search_info(query: str, exclude=None) -> tuple[Optional[str], dict]:
    """图片搜索，连同视觉判读一起返回。

    工具调用要先知道「搜到的是什么样的图」再开口，否则前台只能瞎猜画面。
    """
    return _image_search(query, exclude)


def _image_search(query: str, exclude=None) -> tuple[Optional[str], dict]:
    """图片搜索：先百度、失败退回必应；逐张下载 + 后台视觉校验，返回第一张合格直链。

    百度那个 index 页面是 JS 空壳，真正结果从 acjson 接口取；必应只取
    a.iusc 里的真实结果。每个候选都要过三关：视觉判读切题且可用、不是素材站/
    水印图、画面描述和关键词对得上；不合格换下一张或换关键词重搜，全不合格
    返回 (None, {})。画风不再设限，只核对内容是否切题。
    """
    mode = _search_mode(query)
    terms = _search_variants(query, mode)
    if not terms:
        return None, {}
    from urllib.parse import quote
    seen, seen_hash, tried = set(), set(), 0
    for term in terms:
        per = 0
        urls = _baidu_image_urls(term)
        engine = "百度"
        if not urls:
            engine = "必应"
            try:
                import httpx
                resp = httpx.get(_BING_URL.format(q=quote(term)),
                                 headers={"User-Agent": UA}, timeout=20,
                                 follow_redirects=True)
                urls = _bing_image_urls(resp.text)
            except Exception:             # noqa: BLE001
                continue
        if urls:
            logger.info("搜图候选 q=%r 引擎=%s 共=%d", term, engine, len(urls))
        for u in urls:
            if exclude and u in exclude:        # 这个会话已经发过，换下一张
                continue
            if u in seen:
                continue
            seen.add(u)
            low = urlparse(u).path.lower()
            if (not any(low.endswith(e) for e in
                        (".jpg", ".jpeg", ".png", ".webp", ".gif"))
                    and not _is_baidu_image(u)):
                continue
            if not url_is_public(u) or _is_stock(u):
                continue
            if tried >= SEARCH_MAX_CANDIDATES or per >= SEARCH_PER_VARIANT:
                break
            per += 1
            tried += 1
            data, why = download_image_ex(u)
            if not data:
                logger.info("搜图候选下载失败 q=%r：%s", term, why)
                continue
            digest = hashlib.md5(data).hexdigest()
            if digest in seen_hash:              # 不同直链可能是同一张图
                continue
            seen_hash.add(digest)
            why_bad = _screen_candidate(data)
            if why_bad:
                logger.info("搜图候选预筛淘汰 q=%r：%s", term, why_bad)
                continue
            info = _search_check(data, term)
            if not info:
                logger.info("搜图候选判读失败 q=%r", term)
                continue
            score = _subject_score(term, info, mode)
            # 表情包必须字面主体对得上；一般图片以模型 match 为准，主体分只记录，
            # 免得「北京」没写进描述就把真正的北京风景全毙掉。
            bad = not info.get("ok") or _is_stock(u, info)
            if mode == "sticker":
                bad = bad or score < 0.5
            if bad:
                logger.info("搜图候选不合格 q=%r 模式=%s：主体=%.2f kind=%s style=%s "
                            "match=%s desc=%r text=%r why=%s", term, mode, score,
                            info.get("kind"), info.get("style"), info.get("match"),
                            info.get("desc"), info.get("text"), info.get("why"))
                continue
            logger.info("搜图命中 q=%r 模式=%s 主体=%.2f desc=%r", term, mode, score,
                        info.get("desc"))
            return u, info
        if tried >= SEARCH_MAX_CANDIDATES:
            break
    logger.info("搜图无合格结果 q=%r 模式=%s 已判读 %d 张", query, mode, tried)
    return None, {}


# ---------------------------------------------------------------- TTS

_TTS_SWEPT_AT = 0.0


def tts_dir() -> Path:
    return Path(os.environ.get("RP_TTS_DIR") or "/opt/qqbot/tts_cache").expanduser()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def sweep_tts_cache(force: bool = False) -> int:
    """按年龄/容量清理 TTS 缓存（借鉴 AstrBot TempDirCleaner 的思路）。

    先删超过 RP_TTS_MAX_AGE_DAYS 的文件，再按 mtime 从旧到新删到
    RP_TTS_MAX_MB 以下。默认最多每小时跑一次，返回删除数量。
    """
    global _TTS_SWEPT_AT
    now = time.time()
    if not force and now - _TTS_SWEPT_AT < 3600:
        return 0
    _TTS_SWEPT_AT = now
    d = tts_dir()
    if not d.is_dir():
        return 0
    max_age = _env_float("RP_TTS_MAX_AGE_DAYS", 7) * 86400
    max_bytes = int(_env_float("RP_TTS_MAX_MB", 200) * 1024 * 1024)
    files = []
    for p in d.iterdir():
        if not p.is_file() or p.suffix.lower() != ".mp3":
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        files.append((st.st_mtime, st.st_size, p))
    removed = 0
    for mtime, _, path in files:
        if max_age > 0 and now - mtime > max_age:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    files = [f for f in files if f[2].exists()]
    total = sum(f[1] for f in files)
    if max_bytes > 0:
        for _, size, path in sorted(files):
            if total <= max_bytes:
                break
            try:
                path.unlink()
                total -= size
                removed += 1
            except OSError:
                pass
    return removed


async def tts_audio(text: str) -> Optional[Path]:
    """edge-tts 合成语音，带磁盘缓存。失败返回 None。"""
    text = (text or "").strip()
    if not text:
        return None
    cache = tts_dir()
    try:
        sweep_tts_cache()               # 每小时最多一次，防缓存无限膨胀
    except OSError:
        pass
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    key = hashlib.sha1(f"{tts_voice()}|{text}".encode()).hexdigest()[:16]
    out = cache / f"{key}.mp3"
    if out.exists() and out.stat().st_size > 512:
        return out
    try:
        import edge_tts
    except ImportError:
        return None
    try:
        await edge_tts.Communicate(text, tts_voice()).save(str(out))
    except Exception:                     # noqa: BLE001
        return None
    return out if out.exists() and out.stat().st_size > 512 else None


# ---------------------------------------------------------------- 渲染

def image_segment(path: Path) -> Optional[MessageSegment]:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > DOWNLOAD_LIMIT:
        return None
    return MessageSegment.image(f"base64://{base64.b64encode(raw).decode()}")


def _record_segment(path: Path) -> Optional[MessageSegment]:
    try:
        uri = path.resolve().as_uri()
    except OSError:
        return None
    return MessageSegment.record(uri)


def split_markers(text: str) -> List[Tuple]:
    """切成 [("text", 片段) | ("marker", kind, payload)] 的扁平列表。"""
    out: List[Tuple] = []
    pos = 0
    for m in MARKER_RE.finditer(text):
        if m.start() > pos:
            out.append(("text", text[pos:m.start()]))
        out.append(("marker", m.group(1), m.group(2).strip()))
        pos = m.end()
    if pos < len(text):
        out.append(("text", text[pos:]))
    return out


def rich_hint() -> str:
    """追加到人设之后的能力说明：标记用法 + 当前贴纸库标签。"""
    counts = list_tags()
    # 必须列全量：以前只给前 50 个，长尾标签（如排第 262 的「笨蛋」）模型看不到，
    # 它会直接说「库里没有这个标签」，可实际用 [[sticker:笨蛋]] 能发出来。
    tags = [t for t, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    tag_line = "、".join(tags) if tags else "（还没有贴纸，主人发来的表情包会自动入库）"
    return f"""

【动作标记——默认不用，读清下面的使用条件】
大多数回复应该只有文字。你能用的标记只有这些：
- [[sticker:标签]] 从贴纸库随机发一张贴纸（[[sticker:开心*2]] 随机发两张，最多 *3），可用标签：{tag_line}
- [[search:两三个关键词]] 搜一张贴合语境的图发出来：要表情包/梗图就在末尾写
  「表情包」；主人要照片、风景、插画、原画、壁纸时按内容原样写，别加「表情包」
  和「二次元」（例如 [[search:北京 风景照]]、[[search:原神 插画]]）
  找角色时带上发色、服饰等外观特征，别只写名字（例如 [[search:Claude娘 橙发少女 插画]]）
  本轮工具列表里如果有 search_image，要发图就调用它：它会先把画面描述返回给你，
  你按描述说话，图由插件附上；这时不要再写 [[search:...]]。只有没有这个工具时
  才用标记。
- [[voice:要说的话]] 用语音说
- [[img:https://图片直链]] 只发确定真实存在的直链（不确定就用 search）
- 撤回消息：用 recall_message 工具，target 填「你最近发出的消息」清单里的编号（如 M8），
  后台会在你这条回复发出前撤回那条；不要写 [[recall]]，也不要没调用工具就说「已经撤回」。
只在以下时机使用，其余时候一律纯文字：
①主人明确要求（"来张表情包""发张图看看""用语音说"）；
②主人只发来一个表情包时，可以回敬一个贴纸；
③情绪确实强烈到文字撑不住时，一条回复最多一个 [[sticker:标签]]，且不许连续几条回复都带；
  只有主人明确要「多来几张」时才写 [[sticker:标签*2]] 或 *3，同一条回复里不重复同一张。
  库里同类标签很多，每次都是随机抽，不要以为总发的是同一张。
【黄豆小表情 [[face:id]] 默认禁用】主人不喜欢，尤其是争议大的那几款
（滑稽、斜眼笑、doge、吃瓜、微笑这类）。只有主人自己先发黄豆斗图时才跟着用；
日常回复一个黄豆都不要加。
【搜图风格不设限】服务端不按动漫/二次元风格筛图，关键词只描述画面内容：要表情包
就在末尾带「表情包」（例如 [[search:哭泣 表情包]]）；要实拍照片、建筑、风景、插画，
按内容原样写（例如 [[search:深圳 高楼 建筑]]、[[search:北京 风景照]]），不要加
「二次元」「动漫」，也别加「图片」这种泛词。
【搜图关键词怎么取】主人转过参考图，或说「这种 / 类似这种 / 还有嘛」时，关键词必须
沿用参考图的外形（发色、服装、角色、动作），不能只用主人顺口的称呼。例：参考图是
蓝发女仆鲸鱼娘，就写 [[search:蓝发 鲸鱼娘 女仆 表情包]]；只写「蓝色大鱼」会搜回普通
卡通鲸鱼。主人纠正过一次后，后续搜索沿用纠正后的词，不要退回字面称呼。
标签尽量从上面的清单里选。主人点名要某个标签（比如「发一张笨蛋表情包」）时，
直接写 [[sticker:主人说的那个词]]：服务端会按标签名和同义词匹配，发不出来这条
会自动省略。不要凭印象提前说「库里没有这个标签」——清单很长，只是给你参考。
没有能用的贴纸，再用 [[search:关键词 表情包]]。
贴纸库已做过内容审核（含脏话、辱骂、擦边的图已被剔除），你按标签挑即可，
不用猜图上写了什么；主人没点名、标签又确实对不上时宁可不发，也不要硬塞一个。
主人说刚才那张不合适、以后别发，调用 delete_sticker 工具把库里那张真删掉
（query 留空 = 本会话最后发的那张；也可以写标签或文件名），删完再说话。"""


async def build_messages(reply: str, chunk: int = 1000, exclude=None,
                         sid: str = "") -> List[Message]:
    """把回复渲染成若干条可直接 send 的消息（文本按 chunk 切分）。

    exclude 是这个会话已经发过的图片直链：命中就跳过，并把它追加进去，
    主人说「再来一张其他的」时不会又发回同一张。
    sid 用来记「最后发出去的那张贴纸」，主人说这张不合适时能按会话找到它。
    """
    loop = asyncio.get_running_loop()
    messages: List[Message] = []
    buf = ""
    used: set = set()             # 同一条回复里不重复发同一张图

    def flush() -> None:
        nonlocal buf
        while buf:
            piece, buf = buf[:chunk], buf[chunk:]
            if piece.strip():
                messages.append(Message(MessageSegment.text(piece)))

    for part in split_markers(reply):
        if part[0] == "text":
            buf += part[1]
            continue
        _, kind, payload = part
        flush()
        seg: Optional[MessageSegment] = None
        if kind == "img":
            # 只允许模型给 base64 或公网直链；file:// 会让模型读到并发出服务器本地文件
            if payload.startswith("base64://"):
                seg = MessageSegment.image(payload)
            elif payload.startswith(("http://", "https://")) \
                    and await loop.run_in_executor(None, url_is_public, payload):
                low = urlparse(payload).path.lower()
                if any(low.endswith(e) for e in IMG_EXT):
                    seg = MessageSegment.image(payload)
        elif kind == "search":
            url = await loop.run_in_executor(None, image_search, payload, exclude)
            if url:
                seg = MessageSegment.image(url)
                if exclude is not None:
                    exclude.append(url)
        elif kind == "face":
            if payload.isdigit():
                seg = MessageSegment.face(int(payload))
        elif kind == "sticker":
            tag, _, num = payload.partition("*")
            try:
                want = max(1, min(3, int(num.strip())))
            except (TypeError, ValueError):
                want = 1
            hits = [p for p in pick_stickers(tag.strip(), want + len(used))
                    if p not in used][:want]
            for path in hits:
                used.add(path)
                one = image_segment(path)
                if one is not None:
                    messages.append(Message(one))
                    _note_sticker(sid, path)
            continue
        elif kind == "voice":
            if payload.startswith("http"):
                if await loop.run_in_executor(None, url_is_public, payload):
                    seg = MessageSegment.record(payload)
            else:
                audio = await tts_audio(payload)
                if audio:
                    seg = _record_segment(audio)
        if seg is not None:
            messages.append(Message(seg))
    flush()
    return [m for m in messages if m]
