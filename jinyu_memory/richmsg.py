# -*- coding: utf-8 -*-
"""富消息渲染与贴纸库：把回复文本里的标记变成 OneBot v11 消息段。

支持的标记（写在回复正文里即可）：

    [[img:https://example.com/a.jpg]]   发送网络图片
    [[search:关键词]]                    服务端搜图（必应），发第一张可用结果
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


def pick_sticker(tag: str, allow_empty_random: bool = False) -> Optional[Path]:
    """按标签挑一张通过安全判读的贴纸；没有命中的标签返回 None（绝不乱发）。

    空标签只有管理员显式请求（/sticker 不带参数）时才随机；模型输出的空标记
    一律视为无匹配，避免「标签对不上就随便塞一张」。
    """
    files = usable_stickers()
    if not files:
        return None
    tag = (tag or "").strip().lower()
    if not tag:
        return random.choice(files) if allow_empty_random else None
    store = _load_tags()
    hits = [p for p in files
            if any(_related_tag(tag, t.lower()) for t in store.get(p.name, []))]
    return random.choice(hits) if hits else None


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
    return True


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
2. character 填图中角色的名字：动漫/游戏/影视/知名IP角色（如 初音未来、柴郡、玛奇玛）
   或现实公众人物。只写最出名的名字，多个角色只写最主要的一个；普通真人随手拍、
   纯风景/物件/截图认不出角色的填空字符串；
3. text 必须把图中所有文字抄全，包括语气词、脏话，不要因为不雅而省略或打码；
4. suitable 仅对表情包有意义：图中文字含辱骂、人身攻击、性暗示、脏话，或者画面模糊、
   属于一次性截图/私人隐私的，填 false；
5. tags 写情绪或使用场景（开心/无语/生气/撒娇/震惊/嫌弃 等），不要写「表情包」。"""


VISION_MAX_SIDE = 1280          # 视觉模型不需要原图分辨率，缩到这个长边足够看清文字
VISION_JPEG_QUALITY = 85
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


def _vision_call(data: bytes, prompt: str) -> str:
    """一次视觉请求：推理型模型会把预算花在思考上，截断就加倍重试。"""
    key = _front_key()
    if not key:
        raise RuntimeError("no_key")
    import httpx
    base = (os.environ.get("RP_LLM_BASE") or "https://api.deepseek.com").rstrip("/")
    b64 = base64.b64encode(data).decode()
    want = 6000
    content = ""
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
            return content
        if finish == "length" and want < 16000:
            want *= 2
            continue
        if content:
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
    text = CTRL.sub("", str(parsed.get("text") or "")).strip()[:60]
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


def _record_meta(name: str, info: dict, tags: List[str]) -> None:
    """把判读结果写进 meta.json；已存在的图也要写，否则手工/重复入库的图永远没有审核记录。"""
    store = _load_meta()
    store[name] = {"kind": info.get("kind") or "表情包",
                   "desc": info.get("desc") or "",
                   "text": info.get("text") or "",
                   "character": info.get("character") or "",
                   "tags": tags,
                   "suitable": True,
                   "blocked": False,
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
    with _STORE_LOCK:                      # 配额检查 → 入库 → 记 meta，必须原子
        d = sticker_dir()
        known = any(p.stem == md5 for p in d.iterdir()) if d.is_dir() else False
        tags = list(info.get("tags") or []) \
            + ([info["character"]] if info.get("character") else []) \
            + _text_tags(info.get("text") or "") + list(extra_tags or [])
        tags = list(dict.fromkeys(t for t in tags if t))[:12]
        if known:
            path = add_sticker_bytes(data, tags)
            if path is not None:
                _record_meta(path.name, info, tags or ["未分类"])
            return path
        if not force and autosave_quota_left() <= 0:
            return None
        path = add_sticker_bytes(data, tags or ["未分类"])
        if path is None:
            return None
        if not force:
            autosave_consume()
        _record_meta(path.name, info, tags or ["未分类"])
        return path


# ---------------------------------------------------------------- 搜图

_BING_URL = "https://cn.bing.com/images/search?q={q}&form=HDRSC2&first=1"
_MURL_RE = re.compile(r'murl&quot;:&quot;(https?://[^&"]+)')
_MURL_RE2 = re.compile(r'"murl"\s*:\s*"(https?://[^"]+)"')


def image_search(query: str) -> Optional[str]:
    """必应图片搜索，返回第一个通过 SSRF 校验的图片直链。失败返回 None。

    用户偏好动漫/二次元风格：查询缺「表情包」字样时自动补
    「动漫表情包」后缀，保证搜出来的图贴合人设。
    """
    query = (query or "").strip()
    if not query:
        return None
    if "表情包" not in query:
        query += " 动漫表情包"
    from urllib.parse import quote
    try:
        import httpx
        resp = httpx.get(_BING_URL.format(q=quote(query)),
                         headers={"User-Agent": UA}, timeout=20,
                         follow_redirects=True)
        page = resp.text
    except Exception:                     # noqa: BLE001
        return None
    urls = [html_mod.unescape(m) for m in _MURL_RE.findall(page)]
    urls += _MURL_RE2.findall(page)
    seen = set()
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        low = urlparse(u).path.lower()
        if not any(low.endswith(e) for e in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
            continue
        if url_is_public(u):
            return u
    return None


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
    tags = [t for t, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:50]
    tag_line = "、".join(tags) if tags else "（还没有贴纸，主人发来的表情包会自动入库）"
    return f"""

【动作标记——默认不用，读清下面的使用条件】
大多数回复应该只有文字。你能用的标记只有这些：
- [[sticker:标签]] 从贴纸库发一个贴纸，可用标签：{tag_line}
- [[search:两三个关键词 动漫表情包]] 搜一张贴合语境的图发出来
- [[voice:要说的话]] 用语音说
- [[img:https://图片直链]] 只发确定真实存在的直链（不确定就用 search）
只在以下时机使用，其余时候一律纯文字：
①主人明确要求（"来张表情包""发张图看看""用语音说"）；
②主人只发来一个表情包时，可以回敬一个贴纸；
③情绪确实强烈到文字撑不住时，一条回复最多一个 [[sticker:标签]]，且不许连续几条回复都带。
【黄豆小表情 [[face:id]] 默认禁用】主人不喜欢，尤其是争议大的那几款
（滑稽、斜眼笑、doge、吃瓜、微笑这类）。只有主人自己先发黄豆斗图时才跟着用；
日常回复一个黄豆都不要加。
【审美要求】搜图和选贴纸必须偏向动漫/二次元风格：search 的关键词末尾要带上
「动漫表情包」（例如 [[search:哭泣 动漫表情包]]）；真人照片、风景照、写实图都不合格。
标签尽量从上面的清单里选；没有合适的就用 [[search:关键词 动漫表情包]]。
贴纸库已做过内容审核（含脏话、辱骂、擦边的图已被剔除），你按标签挑即可，
不用猜图上写了什么；标签对不上的时候宁可不发，也不要硬塞一个。"""


async def build_messages(reply: str, chunk: int = 1000) -> List[Message]:
    """把回复渲染成若干条可直接 send 的消息（文本按 chunk 切分）。"""
    loop = asyncio.get_running_loop()
    messages: List[Message] = []
    buf = ""

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
            url = await loop.run_in_executor(None, image_search, payload)
            if url:
                seg = MessageSegment.image(url)
        elif kind == "face":
            if payload.isdigit():
                seg = MessageSegment.face(int(payload))
        elif kind == "sticker":
            path = pick_sticker(payload)
            if path:
                seg = image_segment(path)
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
