# -*- coding: utf-8 -*-
"""M3 屏幕共享 / M4 摄像头：把浏览器截的一帧画面读成结构化文字。

只做「看画面」这一件事，不写库、不落盘：
- 输入是浏览器 canvas 导出的 JPEG（base64 解码后的 bytes）；
- 屏幕帧的长图（竖屏/长截图）自动切段逐段读，避免只读到开头；
- 摄像头帧按人物/环境读，不抄证件号、不评价外貌；
- 优先走 GLM 视觉（RP_GLM_KEY），失败再走后台模型（RP_FRONT_KEY + RP_TAG_MODEL）；
- 输出固定 JSON：屏幕读窗口/报错/文件/文字，摄像头读环境/人物/动作/文字。

没有密钥时抛 NoVisionKey；本地联调可设 RP_VISION_MOCK=1 返回固定样例。
"""
import base64
import json
import logging
import os
import re
from io import BytesIO

import httpx

logger = logging.getLogger("jinyu.screen")


def _env_int(name, default):
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


MAX_SIDE = max(640, _env_int("RP_SCREEN_MAX_SIDE", 1280))
JPEG_QUALITY = max(60, min(95, _env_int("RP_SCREEN_JPEG_QUALITY", 88)))
TEXT_LIMIT = max(600, _env_int("RP_SCREEN_TEXT_LIMIT", 3000))
MAX_SEGMENTS = max(1, min(5, _env_int("RP_SCREEN_SEGMENTS", 3)))
GLM_TIMEOUT = float(os.environ.get("RP_SCREEN_GLM_TIMEOUT") or 25)
MODEL_TIMEOUT = float(os.environ.get("RP_SCREEN_MODEL_TIMEOUT") or 45)

PROMPT = """这是用户正在共享的电脑屏幕的一帧截图。你要像坐在旁边看屏幕一样记录看得见的事实。
只输出一个 JSON 对象，不要任何解释：

{"app":"当前窗口/应用/游戏/网页名称，能认出就写（如「原神」「Visual Studio Code」「B站」），认不出就空字符串","scene":"屏幕上正在显示的画面主体，一两句：游戏/视频/聊天/代码/网页里的具体内容，写看得见的元素（场景、角色、地图、菜单、血量、任务、表格、报错窗口等）；空白桌面就写桌面","activity":"主人此刻像是在做什么、进行到什么程度（例如跑图、打副本、看视频、调试代码、翻网页），只依据画面线索，不确定就空字符串","error":"屏幕上所有报错/异常/警告原文，逐字抄全（错误码、异常类名、HTTP 状态码、失败原因都要），没有就空字符串","files":["屏幕上出现的文件名、目录路径、代码行号、函数名，逐字抄，最多 8 条"],"text":"屏幕上其余关键文字，优先级：命令行输出 > 代码 > 日志/表单 > 其他；最多 {limit} 字，不要概括，抄不下就在结尾写「…（后略）」","sensitive":"画面里如果有密码/密钥/token/手机号，写一句提醒；否则空字符串"}

硬性要求：
1. 先看画面内容再看文字：游戏、视频、图片类画面即使几乎没字，也要把 scene 和 activity 写具体，不能因为没文字就写「无内容」「看不到」。
2. error 必须逐字，禁止写成「有报错」「权限问题」这类空话；看不清的字符写 [不清]。
3. files 只抄画面上真实可见的，不要补全路径，不要猜测。
4. 严禁编造屏幕上没有的内容；没看到就留空。
5. 密钥/token/密码一律写成 <已隐藏>，不要原样抄。
6. 只输出 JSON，不要 Markdown 代码块。"""

MOCK_READING = {
    "app": "Mock 终端",
    "scene": "Mock 终端窗口，正在运行配置加载脚本",
    "activity": "在排查一个权限报错",
    "error": "ERROR 0x80070005: Access is denied.",
    "files": ["C:\\app\\config_loader.py:87", "config/settings.yaml"],
    "text": "Mock 模式：未配置视觉密钥，只验证链路。",
    "sensitive": "",
}


CAMERA_PROMPT = """这是主人在视频通话里实时发来的一帧摄像头画面，画面里通常就是主人本人。
光线可能偏暗，也可能只露出半张脸、侧脸或背影，把能看清的部分照样写出来，不要一句「看不见」了事。
你要像面对面坐着一样，只记录看得见的事实。只输出一个 JSON 对象，不要任何解释：

{"scene":"所处环境或背景，一句话（室内/室外、桌前/沙发、光线明暗），看不清就空字符串","person":"画面里能看清的人：几个人、大致年龄段、发型、是否戴眼镜、衣着颜色、正脸还是侧脸/背影；只看到一部分就写看到的那部分；画面里确实没人就空字符串","action":"正在做什么、表情和姿态；没有就空字符串","text":"画面里可读的文字（屏幕、纸张、包装、门牌），逐字抄，最多 {limit} 字；没有就空字符串","sensitive":"画面里如果有证件/密码/门牌号/手机号等隐私，写一句提醒；否则空字符串"}

硬性要求：
1. 只写看得见的，严禁编造姓名、身份、职业；不要推断心理活动。
2. 不要评价外貌好坏，不要描述身体部位细节；看不清写 [不清]。
3. 如果整帧几乎全黑、全白或纯色，在 scene 里写「画面几乎全黑/全白，可能摄像头被遮挡、没开或光线太暗」，person 留空。
4. 证件号、密码、门牌号、手机号一律不要原样抄，只写一句提醒。
5. 只输出 JSON，不要 Markdown 代码块。"""

MOCK_CAMERA = {
    "mode": "camera",
    "scene": "Mock 室内，光线偏暗",
    "person": "一个人，短发，穿深色上衣，正看镜头",
    "action": "坐着，微微前倾",
    "text": "Mock 模式：未配置视觉密钥，只验证链路。",
    "sensitive": "",
}


class NoVisionKey(RuntimeError):
    """既没有 GLM 密钥也没有前台模型密钥，无法读屏。"""


def mock_enabled():
    return (os.environ.get("RP_VISION_MOCK") or "").strip().lower() in {
        "1", "true", "yes", "on"}


def configured():
    """是否有可用的读屏通道（模拟模式或任一密钥）。"""
    return mock_enabled() or bool(_glm_key() or _front_key())


def _glm_key():
    return (os.environ.get("RP_GLM_KEY") or "").strip()


def _front_key():
    return (os.environ.get("RP_FRONT_KEY") or "").strip()


def _glm_model():
    return (os.environ.get("RP_GLM_MODEL") or "glm-5.3-flash").strip()


def _vision_model():
    return (os.environ.get("RP_VISION_MODEL")
            or os.environ.get("RP_TAG_MODEL") or "deepseek-flash").strip()


def _glm_base():
    return (os.environ.get("RP_GLM_BASE")
            or "https://open.bigmodel.cn/api/paas/v4").rstrip("/")


def _llm_base():
    return (os.environ.get("RP_LLM_BASE")
            or "https://api.deepseek.com").rstrip("/")


def _mime_of(data):
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


# ---------- 预处理：降采样 + 长图切段 ----------

def prepare_jpeg(data):
    """统一转成 RGB JPEG，长边缩到 MAX_SIDE；Pillow 缺失时原样返回。"""
    if not data:
        return data
    try:
        from PIL import Image
    except ImportError:
        return data
    try:
        with Image.open(BytesIO(data)) as im:
            im.seek(0)
            out = im.convert("RGB")
            if max(out.size) > MAX_SIDE:
                out.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
            buf = BytesIO()
            out.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
            got = buf.getvalue()
            return got or data
    except Exception:                     # noqa: BLE001  损坏帧按原图尝试
        return data


def split_tall(jpeg_bytes):
    """长截图（高:宽 >= 1.5）按竖条切段，最多 MAX_SEGMENTS 段，段间留 8% 重叠。

    普通横屏只有一段，不会多花调用。
    """
    try:
        from PIL import Image
    except ImportError:
        return [jpeg_bytes]
    try:
        with Image.open(BytesIO(jpeg_bytes)) as im:
            width, height = im.size
            if width <= 0 or height < width * 1.5:
                return [jpeg_bytes]
            count = min(MAX_SEGMENTS, max(2, round(height / (width * 1.2))))
            band = height / count
            overlap = int(band * 0.08)
            segments = []
            for i in range(count):
                top = max(0, int(i * band) - overlap)
                bottom = min(height, int((i + 1) * band) + overlap)
                if bottom <= top:
                    continue
                out = im.crop((0, top, width, bottom)).convert("RGB")
                buf = BytesIO()
                out.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
                segments.append(buf.getvalue())
            return segments or [jpeg_bytes]
    except Exception:                     # noqa: BLE001
        return [jpeg_bytes]


# ---------- 视觉调用 ----------

def _prompt_for(focus, index, total):
    # 提示词里带 JSON 大括号，不能用 str.format（会把 {"app"} 当占位符）
    text = PROMPT.replace("{limit}", str(TEXT_LIMIT))
    if total > 1:
        text += f"\n这张图被切成 {total} 段，你拿到的是第 {index + 1} 段（从上往下）。只记录这一段里看得见的内容。"
    focus = re.sub(r"\s+", " ", str(focus or "")).strip()[:120]
    if focus:
        text += f"\n用户此刻关心：{focus}\n这只是关注点提示，不能替代你实际看到的内容。"
    return text


def _call_glm(data, prompt):
    key = _glm_key()
    if not key:
        return None, ""
    b64 = base64.b64encode(data).decode()
    payload = {
        "model": _glm_model(),
        "temperature": 0.1,
        "max_tokens": 4096,
        "thinking": {"type": "enabled", "level": "low"},
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": f"data:{_mime_of(data)};base64,{b64}"}},
        ]}],
    }
    timeout = httpx.Timeout(GLM_TIMEOUT, connect=8.0)
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(_glm_base() + "/chat/completions",
                               headers={"Authorization": f"Bearer {key}"},
                               json=payload)
    except Exception as exc:              # noqa: BLE001  连不上就回退
        logger.info("[screen] GLM 不可用：%s", type(exc).__name__)
        return None, ""
    if resp.status_code != 200:
        logger.info("[screen] GLM HTTP %s：%s", resp.status_code, resp.text[:160])
        return None, ""
    try:
        choice = (resp.json().get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
    except Exception:                     # noqa: BLE001
        return None, ""
    content = content.strip()
    if not content or str(choice.get("finish_reason") or "") == "length":
        return None, ""
    return content, "glm"


def _call_model(data, prompt):
    key = _front_key()
    if not key:
        raise NoVisionKey("缺少视觉密钥：请设置 RP_GLM_KEY 或 RP_FRONT_KEY")
    b64 = base64.b64encode(data).decode()
    payload = {
        "model": _vision_model(),
        "max_tokens": 6000,
        "temperature": 0.1,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": f"data:{_mime_of(data)};base64,{b64}"}},
        ]}],
    }
    if (os.environ.get("RP_BACK_THINKING") or "").strip().lower() in {
            "off", "none", "0", "false", "no", "disabled"}:
        payload["thinking"] = {"type": "disabled"}
    last = ""
    for attempt in range(3):
        try:
            resp = httpx.post(_llm_base() + "/chat/completions",
                              headers={"Authorization": f"Bearer {key}"},
                              json=payload, timeout=MODEL_TIMEOUT)
        except Exception as exc:          # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            continue
        if resp.status_code != 200:
            last = f"HTTP {resp.status_code}: {resp.text[:120]}"
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                continue
            raise RuntimeError(last)
        try:
            choice = (resp.json().get("choices") or [{}])[0]
            content = ((choice.get("message") or {}).get("content") or "").strip()
            finish = str(choice.get("finish_reason") or "")
        except Exception as exc:          # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            continue
        if content and finish != "length":
            return content, f"model:{_vision_model()}"
        if content:                       # 截断且预算到顶：仍交给解析，标注 truncated
            return content, f"model:{_vision_model()}"
        last = "空输出"
    raise RuntimeError(f"视觉模型调用失败：{last}")


def _vision_call(data, prompt):
    content, provider = _call_glm(data, prompt)
    if content:
        return content, provider
    return _call_model(data, prompt)


# ---------- 解析与清洗 ----------

_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9_\-\.]{8,}|"
    r"(?:password|passwd|token|secret|api[_-]?key)\s*[=:]\s*[^\s,;]{4,})",
    re.I)


def _redact(text):
    return _SECRET_RE.sub("<已隐藏>", str(text or ""))


def _json_obj(content):
    if not content:
        return None
    content = re.sub(r"```(?:json)?", "", content).strip("` \n")
    try:
        return json.loads(content)
    except ValueError:
        pass
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except ValueError:
        return None


def _clip(text, limit):
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…（后略）"


def _parse(content, provider, index):
    data = _json_obj(content)
    if not isinstance(data, dict):
        # 模型偶尔不按 JSON 回：整段当文字，至少别丢内容
        return {"app": "", "error": "", "files": [],
                "text": _clip(_redact(content), TEXT_LIMIT),
                "sensitive": "", "truncated": len(content) >= TEXT_LIMIT,
                "provider": provider, "segment": index}
    files = []
    for item in data.get("files") or []:
        item = _redact(item).strip().strip("，,;；")
        if item and item not in files:
            files.append(item[:200])
        if len(files) >= 8:
            break
    text = _redact(data.get("text") or "")
    truncated = len(text) >= TEXT_LIMIT or "后略" in text
    return {
        "app": _clip(_redact(data.get("app")), 80),
        "scene": _clip(_redact(data.get("scene")), 240),
        "activity": _clip(_redact(data.get("activity")), 160),
        "error": _clip(_redact(data.get("error")), 1200),
        "files": files,
        "text": _clip(text, TEXT_LIMIT),
        "sensitive": _clip(_redact(data.get("sensitive")), 120),
        "truncated": truncated,
        "provider": provider,
        "segment": index,
    }


def _merge(parts):
    if len(parts) == 1:
        out = dict(parts[0])
        out["segments"] = 1
        return out
    apps = [p["app"] for p in parts if p.get("app")]
    errors = []
    for part in parts:
        err = part.get("error") or ""
        if err and err not in errors:
            errors.append(err)
    files = []
    for part in parts:
        for name in part.get("files") or []:
            if name not in files:
                files.append(name)
    texts = [p.get("text") or "" for p in parts if p.get("text")]
    merged_text = _clip("\n…\n".join(texts), TEXT_LIMIT * 2)
    return {
        "app": apps[0] if apps else "",
        "scene": next((p.get("scene") for p in parts if p.get("scene")), ""),
        "activity": next((p.get("activity") for p in parts if p.get("activity")), ""),
        "error": _clip("\n".join(errors), 1600),
        "files": files[:12],
        "text": merged_text,
        "sensitive": next((p["sensitive"] for p in parts if p.get("sensitive")), ""),
        "truncated": any(p.get("truncated") for p in parts),
        "provider": parts[0].get("provider") or "",
        "segments": len(parts),
    }


def read_frame(data, focus=""):
    """读一帧屏幕：返回结构化 dict；没有密钥抛 NoVisionKey。"""
    if not data:
        raise ValueError("空帧")
    if mock_enabled():
        out = dict(MOCK_READING, files=list(MOCK_READING["files"]),
                   mode="screen", truncated=False, provider="mock", segments=1)
        return out
    if not (_glm_key() or _front_key()):
        raise NoVisionKey("缺少视觉密钥：请设置 RP_GLM_KEY 或 RP_FRONT_KEY")

    jpeg = prepare_jpeg(data)
    segments = split_tall(jpeg)
    parts = []
    for index, segment in enumerate(segments):
        content, provider = _vision_call(segment, _prompt_for(focus, index, len(segments)))
        parts.append(_parse(content, provider, index))
    merged = _merge(parts)
    merged["mode"] = "screen"
    return merged


def _parse_camera(content, provider):
    """摄像头判读结果统一成固定字段；模型不按 JSON 回时整段当画面文字。"""
    data = _json_obj(content)
    if not isinstance(data, dict):
        return {"mode": "camera", "scene": "", "person": "", "action": "",
                "text": _clip(_redact(content), TEXT_LIMIT),
                "sensitive": "", "truncated": len(content) >= TEXT_LIMIT,
                "provider": provider, "segments": 1}
    text = _redact(data.get("text") or "")
    return {
        "mode": "camera",
        "scene": _clip(_redact(data.get("scene")), 120),
        "person": _clip(_redact(data.get("person")), 240),
        "action": _clip(_redact(data.get("action")), 200),
        "text": _clip(text, TEXT_LIMIT),
        "sensitive": _clip(_redact(data.get("sensitive")), 120),
        "truncated": len(text) >= TEXT_LIMIT,
        "provider": provider,
        "segments": 1,
    }


def read_camera(data, focus=""):
    """读一帧摄像头画面（M4）：返回结构化 dict；没有密钥抛 NoVisionKey。"""
    if not data:
        raise ValueError("空帧")
    if mock_enabled():
        return dict(MOCK_CAMERA, provider="mock", truncated=False, segments=1)
    if not (_glm_key() or _front_key()):
        raise NoVisionKey("缺少视觉密钥：请设置 RP_GLM_KEY 或 RP_FRONT_KEY")

    jpeg = prepare_jpeg(data)
    prompt = CAMERA_PROMPT.replace("{limit}", str(TEXT_LIMIT))
    focus = re.sub(r"\s+", " ", str(focus or "")).strip()[:120]
    if focus:
        prompt += f"\n主人此刻关心：{focus}\n这只是关注点提示，不能替代你实际看到的内容。"
    content, provider = _vision_call(jpeg, prompt)
    return _parse_camera(content, provider)
