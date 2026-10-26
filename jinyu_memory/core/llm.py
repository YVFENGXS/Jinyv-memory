# -*- coding: utf-8 -*-
"""真实模型适配器：OpenAI 兼容 /chat/completions，零第三方依赖。

密钥只从环境变量读，不落盘：
  RP_FRONT_KEY / RP_BACK_KEY        必填
  RP_FRONT_MODEL / RP_BACK_MODEL    选填，默认 deepseek-v4-pro / deepseek-flash
  RP_LLM_BASE                       选填，默认 https://api.deepseek.com
urllib 会读取 http(s)_proxy，适配远端沙箱的出口代理。
"""
import json
import os
import random
import time
import urllib.error
import urllib.request


OFF_VALUES = {"off", "none", "0", "false", "no", "disabled"}
MAX_RETRY_DELAY = 30.0


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
    def __init__(self, key, model, base=None, timeout=120, retries=4, thinking=None):
        if not key:
            raise ValueError("缺少 API key：请设置 RP_FRONT_KEY / RP_BACK_KEY")
        self.key, self.model = key, model
        self.base = (base or os.environ.get("RP_LLM_BASE")
                     or "https://api.deepseek.com").rstrip("/")
        self.timeout, self.retries = timeout, retries
        self.thinking = (thinking or "").strip().lower()
        self.calls = 0

    def chat(self, messages, temperature=0.7, max_tokens=1200, json_mode=False):
        payload = {"model": self.model, "messages": messages,
                   "temperature": temperature}
        if self.thinking in OFF_VALUES:                # RP_*_THINKING=off：关闭思考
            payload["thinking"] = {"type": "disabled"}
        if json_mode:                                  # 归纳体：强约束返回 JSON 对象
            payload["response_format"] = {"type": "json_object"}
        want = max_tokens
        last = None
        for i in range(self.retries):
            try:
                with urllib.request.urlopen(self._req(payload, want),
                                            timeout=self.timeout) as r:
                    body = json.loads(r.read().decode())
                choice = body["choices"][0]
                content = (choice["message"].get("content") or "").strip()
                if content:
                    self.calls += 1
                    return content
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
               thinking=os.environ.get(prefix + "_THINKING", ""))
