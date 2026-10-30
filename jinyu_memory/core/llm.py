# -*- coding: utf-8 -*-
"""真实模型适配器：OpenAI 兼容 /chat/completions，零第三方依赖。

密钥只从环境变量读，不落盘：
  RP_FRONT_KEY / RP_BACK_KEY        必填
  RP_FRONT_MODEL / RP_BACK_MODEL    选填，默认 deepseek-v4-pro / deepseek-flash
  RP_LLM_BASE                       选填，默认 https://api.deepseek.com
urllib 会读取 http(s)_proxy，适配远端沙箱的出口代理。
"""
import json
import logging
import os
import random
import re
import time
import urllib.error
import urllib.request


OFF_VALUES = {"off", "none", "0", "false", "no", "disabled"}
MAX_RETRY_DELAY = 30.0
IODUMP_CHARS = 600                     # 单条消息/输出在日志里最多展示这么多字
OUTPUT_GROW_CAP = 4800                 # 被 finish_reason=length 截断时，最多加到这么多再认命
logger = logging.getLogger("jinyu_memory.llm")

# ---------- DSML：工具调用被服务商当正文吐出来时的兜底 ----------
# 部分模型/网关不走标准 tool_calls，而是在正文里写
#   < | | DSML | | calls>< | | DSML | | invoke name="x">…</…>
# 这种标记原样发到 QQ 就是一屏乱码：能还原成工具调用就照常执行，还原不了也必须删干净。
# DeepSeek 的特殊 token 用全角竖线「｜」(U+FF5C)，半角 | 也要认，否则标记会漏进聊天。
_PIPE = r"[|｜]\s*[|｜]\s*DSML\s*[|｜]\s*[|｜]"
_DSML_OPEN = r"<\s*" + _PIPE + r"\s*"      # 模型常写成「< | | DSML | | invoke」，空格不能漏
_DSML_CLOSE = r"<\s*/\s*" + _PIPE + r"\s*"
_DSML_BLOCK_RE = re.compile(
    _DSML_OPEN + r"(?:calls|tool_calls)\s*>(.*?)" + _DSML_CLOSE
    + r"(?:calls|tool_calls)\s*>", re.S | re.I)
_DSML_INVOKE_RE = re.compile(
    _DSML_OPEN + r'invoke\s+name="([^"]*)"\s*>(.*?)' + _DSML_CLOSE + r"invoke\s*>",
    re.S | re.I)
_DSML_PARAM_RE = re.compile(
    _DSML_OPEN + r"parameter\s+(.*?)>(.*?)" + _DSML_CLOSE + r"parameter\s*>",
    re.S | re.I)
_DSML_TAG_RE = re.compile(
    r"(?:" + _DSML_OPEN + r"|" + _DSML_CLOSE + r")[a-zA-Z_]+\s*(?:[^>]*)?>", re.I)


def _dsml_attr(attrs: str, key: str) -> str:
    m = re.search(rf'{key}\s*=\s*"([^"]*)"', attrs or "")
    return m.group(1) if m else ""


def parse_dsml_calls(text: str):
    """正文里的 DSML 调用 → (calls, 清干净的正文)。

    有完整的 invoke 就还原成标准 tool_calls，让调用方照常执行；
    即使解析不全，正文里的标记也会被删掉——绝不把标记发给用户。
    """
    raw = str(text or "")
    if "DSML" not in raw:
        return [], raw
    calls = []
    for m in _DSML_INVOKE_RE.finditer(raw):
        name = (m.group(1) or "").strip()
        args = {}
        for p in _DSML_PARAM_RE.finditer(m.group(2) or ""):
            key = _dsml_attr(p.group(1), "name")
            if not key:
                continue
            val = p.group(2).strip()
            if _dsml_attr(p.group(1), "string").lower() == "false":
                try:                       # string="false" 是数字/布尔，还原成真实类型
                    val = json.loads(val)
                except ValueError:
                    pass
            args[key] = val
        if name:
            calls.append({"id": f"dsml-{len(calls)}", "type": "function",
                          "function": {"name": name,
                                       "arguments": json.dumps(args, ensure_ascii=False)}})
    cleaned = _DSML_INVOKE_RE.sub("", raw)       # 整块调用（含参数值）先删干净
    cleaned = _DSML_BLOCK_RE.sub("", cleaned)    # 再去 calls 外壳
    dangling = re.search(_DSML_OPEN, cleaned)    # 没闭合的标记：后面半截参数全丢
    if dangling:
        cleaned = cleaned[:dangling.start()]
    cleaned = _DSML_TAG_RE.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return calls, cleaned


def strip_dsml(text: str) -> str:
    """只删标记、不解析：发送前的最后一道保险。"""
    raw = str(text or "")
    if "DSML" not in raw:
        return raw
    return parse_dsml_calls(raw)[1]


def _brief(value, limit=IODUMP_CHARS):
    """日志用：把消息内容压成一行；图片块只显示 [图片]，不写 base64。"""
    if isinstance(value, str):
        text = " ".join(value.split())
        return text[:limit] + ("…" if len(text) > limit else "")
    if isinstance(value, list):
        parts = []
        for item in value:
            if not isinstance(item, dict):
                parts.append(str(item))
            elif item.get("type") == "image_url":
                parts.append("[图片]")
            else:
                parts.append(str(item.get("text") or item.get("content") or ""))
        return _brief(" ".join(p for p in parts if p), limit)
    return _brief(str(value), limit)


def _messages_brief(messages):
    return " | ".join(f"{m.get('role') or '?'}={_brief(m.get('content'))}"
                     for m in messages if isinstance(m, dict))


def retry_delay(attempt, retry_after=None):
    """指数退避 + 抖动；服务端明确给了 Retry-After 就照它（都封顶 30s）。"""
    if retry_after is not None:
        try:
            return max(0.0, min(float(retry_after), MAX_RETRY_DELAY))
        except (TypeError, ValueError):
            pass
    base = min(MAX_RETRY_DELAY, 2.0 ** (attempt + 1))
    return min(MAX_RETRY_DELAY, base * (0.75 + random.random() * 0.5))


def _retry_after(error):
    try:
        raw = error.headers.get("Retry-After") if error.headers else None
    except Exception:                                   # noqa: BLE001
        return None
    try:
        return float(raw) if raw else None
    except (TypeError, ValueError):
        return None


class LLM:
    def __init__(self, key, model, base=None, timeout=120, retries=4, thinking=None,
                 role=""):
        if not key:
            raise ValueError("缺少 API key：请设置 RP_FRONT_KEY / RP_BACK_KEY")
        self.key, self.model = key, model
        self.base = (base or os.environ.get("RP_LLM_BASE")
                     or "https://api.deepseek.com").rstrip("/")
        self.timeout, self.retries = timeout, retries
        self.thinking = (thinking or "").strip().lower()
        self.role = role
        self.calls = 0

    def chat(self, messages, temperature=0.7, max_tokens=1200, json_mode=False):
        payload = {"model": self.model, "messages": messages,
                   "temperature": temperature}
        if self.thinking in OFF_VALUES:                # RP_*_THINKING=off：关闭思考
            payload["thinking"] = {"type": "disabled"}
        if json_mode:                                  # 归纳体：强约束返回 JSON 对象
            payload["response_format"] = {"type": "json_object"}
        return strip_dsml((self._complete(payload, max_tokens).get("content") or "").strip())

    def chat_tools(self, messages, tools, temperature=0.7, max_tokens=1200):
        """带工具的一轮补全：返回 (content, tool_calls)。

        和 chat 的差别只有 payload 里多 tools/tool_choice：多步循环由
        rp_skeleton 负责，这里只把模型这次想调的工具原样交出去。
        """
        if not tools:
            return self.chat(messages, temperature, max_tokens), []
        payload = {"model": self.model, "messages": messages,
                   "temperature": temperature, "tools": tools,
                   "tool_choice": "auto"}
        if self.thinking in OFF_VALUES:
            payload["thinking"] = {"type": "disabled"}
        message = self._complete(payload, max_tokens)
        content = ((message.get("content") or "").strip())
        calls = message.get("tool_calls") or []
        if calls and content and "DSML" in content:
            content = strip_dsml(content)          # 标准调用 + 正文残留标记：只留正文
        if not calls and content:
            # 网关回的是正文里的 DSML 时，还原成标准调用，别让标记直接进聊天记录
            parsed, cleaned = parse_dsml_calls(content)
            if parsed:
                logger.info("[jinyu] DSML 工具调用已还原 %d 个：%s",
                            len(parsed),
                            ", ".join(c["function"]["name"] for c in parsed))
                return cleaned, parsed
            if cleaned != content:
                logger.info("[jinyu] 丢弃不完整的 DSML 标记 %d 字", len(content) - len(cleaned))
                return cleaned, []
        return content, calls

    def _complete(self, payload, max_tokens):
        """带重试的补全，返回原始 message（可能只有 tool_calls 没有 content）。"""
        want = max_tokens
        last = None
        started = time.time()
        if self.role == "back":                        # 后台模型的输入也进 monitor 日志
            logger.info("[jinyu] 后台模型 入参 模型=%s %s",
                        self.model, _messages_brief(payload["messages"]))
        for i in range(self.retries):
            try:
                with urllib.request.urlopen(self._req(payload, want),
                                            timeout=self.timeout) as r:
                    body = json.loads(r.read().decode())
                choice = body["choices"][0]
                message = choice.get("message") or {}
                content = (message.get("content") or "").strip()
                finish = str(choice.get("finish_reason") or "")
                # 被输出上限截断时不能当完整回答发出去（长图逐字念、长清单最容易撞上）：
                # 加一档预算重来，到顶才认命返回。
                if content and finish == "length" and want < OUTPUT_GROW_CAP:
                    want = min(want * 2, OUTPUT_GROW_CAP)
                    last = f"输出截断 finish=length，预算加到 {want}"
                    continue
                if content or message.get("tool_calls"):
                    self.calls += 1
                    if self.role == "back":
                        logger.info("[jinyu] 后台模型 出参 模型=%s 耗时=%.1fs %s",
                                    self.model, time.time() - started,
                                    _brief(content or "[tool_calls]"))
                    return message
                # 推理模型可能把预算全花在 reasoning 上；截断则加倍重试
                last = f"空输出 finish={choice.get('finish_reason')}"
                if choice.get("finish_reason") == "length" and want < 8000:
                    want *= 2
                    continue
                raise RuntimeError(last)
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="ignore")[:300]
                last = f"HTTP {e.code}: {detail}"
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(retry_delay(i, _retry_after(e)))
                    continue
                raise RuntimeError(last) from e
            except RuntimeError:
                raise
            except Exception as e:                      # 超时/网络抖动：退避后重试
                last = f"{type(e).__name__}: {e}"
                time.sleep(retry_delay(i))
        raise RuntimeError(f"LLM 调用失败（{self.retries} 次）：{last}")

    def _req(self, payload, max_tokens):
        body = dict(payload, max_tokens=max_tokens)
        return urllib.request.Request(
            self.base + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.key})


def from_env(role):
    """role='front' | 'back'"""
    prefix = "RP_FRONT" if role == "front" else "RP_BACK"
    default = "deepseek-v4-pro" if role == "front" else "deepseek-flash"
    return LLM(os.environ.get(prefix + "_KEY", ""),
               os.environ.get(prefix + "_MODEL", default),
               thinking=os.environ.get(prefix + "_THINKING", ""),
               role=role)
