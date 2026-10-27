# -*- coding: utf-8 -*-
"""嵌入适配器：硅基流动 BAAI/bge-m3（1024 维），零第三方依赖。

DeepSeek 官方没有 /embeddings 端点，长期记忆的语义召回必须另接嵌入服务。
默认复用已有的 SiliconFlow 配置（RP_ASR_KEY），也可以单独给 RP_EMBED_KEY。

环境变量：
  RP_EMBED          选填，1/0 总开关，默认 1
  RP_EMBED_KEY      选填，默认回退到 RP_ASR_KEY
  RP_EMBED_MODEL    选填，默认 BAAI/bge-m3
  RP_EMBED_BASE     选填，默认 https://api.siliconflow.cn/v1
  RP_EMBED_DIM      选填，默认 1024；换模型必须同步改这个值并重建向量
  RP_EMBED_BATCH    选填，单次请求最多几条，默认 16
  RP_EMBED_TIMEOUT  选填，单次请求超时秒数，默认 15

失败策略：查询向量失败只记日志并熔断一段时间，召回自动退回钩子；
回填失败等下一轮重试。任何情况下都不让记忆检索把前台回复拖死。
"""
import array
import json
import logging
import math
import os
import random
import time
import urllib.error
import urllib.request

logger = logging.getLogger("rp_memory")

OFF_VALUES = {"off", "none", "0", "false", "no", "disabled"}
MODEL = (os.environ.get("RP_EMBED_MODEL") or "BAAI/bge-m3").strip()
DIM = max(1, int(float(os.environ.get("RP_EMBED_DIM") or 1024)))
BATCH = max(1, int(float(os.environ.get("RP_EMBED_BATCH") or 16)))
TIMEOUT = max(3.0, float(os.environ.get("RP_EMBED_TIMEOUT") or 15))
BASE = (os.environ.get("RP_EMBED_BASE") or "https://api.siliconflow.cn/v1").rstrip("/")

_cache = {}          # 文本 -> 归一化向量（只缓成功结果，超量淘汰最早一条）
_fail_until = 0.0    # 熔断截止时间（time.time）
_fails = 0


class EmbeddingError(RuntimeError):
    pass


def key():
    return (os.environ.get("RP_EMBED_KEY") or os.environ.get("RP_ASR_KEY") or "").strip()


def enabled():
    if (os.environ.get("RP_EMBED") or "1").strip().lower() in OFF_VALUES:
        return False
    return bool(key())


def model_name():
    return MODEL


def dim():
    return DIM


def _normalize(vec):
    s = math.sqrt(sum(float(x) * float(x) for x in vec))
    if s <= 0:
        return None
    return [float(x) / s for x in vec]


def pack(vec):
    """归一化后压成 float32 字节：召回时只算点积，省掉每次重算模长。"""
    v = _normalize(vec or [])
    if not v or len(v) != DIM:
        raise EmbeddingError(f"维度不符：期望 {DIM}，得到 {len(v or [])}")
    return array.array("f", v).tobytes()


def dot(query, blob, dims=None):
    """归一化向量点积 = 余弦相似度；坏数据返回 0，不抛异常。"""
    dims = max(1, int(dims or DIM))
    a = array.array("f")
    try:
        a.frombytes(bytes(blob)[:dims * 4])
    except (TypeError, ValueError):
        return 0.0
    if len(a) != dims or len(query) != dims:
        return 0.0
    return sum(x * y for x, y in zip(query, a))


def _post(texts):
    payload = json.dumps({
        "model": MODEL, "input": texts, "encoding_format": "float",
    }, ensure_ascii=False).encode()
    req = urllib.request.Request(
        BASE + "/embeddings", data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key()})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        body = json.loads(r.read().decode())
    data = body.get("data") or []
    if len(data) != len(texts):
        raise EmbeddingError(f"返回条数不符：{len(data)}/{len(texts)}")
    if all(isinstance(x.get("index"), int) for x in data):
        data = sorted(data, key=lambda x: x["index"])
    return [list(map(float, x.get("embedding") or [])) for x in data]


def embed_documents(texts):
    """批量取向量：按 BATCH 切块，429/5xx 退避重试，其余错误直接抛。"""
    items = [str(t or "")[:2000] for t in texts]
    if not enabled():
        raise EmbeddingError("未配置 RP_EMBED_KEY / RP_ASR_KEY")
    out = []
    for i in range(0, len(items), BATCH):
        chunk = items[i:i + BATCH]
        last = ""
        for attempt in range(3):
            try:
                out += _post(chunk)
                break
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}: {e.read().decode(errors='ignore')[:200]}"
                if e.code not in (429, 500, 502, 503, 504):
                    raise EmbeddingError(last) from e
            except Exception as e:                      # noqa: BLE001 网络抖动
                last = f"{type(e).__name__}: {e}"
            time.sleep(min(8.0, (2.0 ** attempt) * (0.75 + random.random() * 0.5)))
        else:
            raise EmbeddingError(f"嵌入失败（{len(chunk)} 条）：{last}")
    return out


def embed_query(text):
    """查询向量；失败返回 None（熔断期间直接跳过），绝不抛给前台。"""
    global _fail_until, _fails
    t = str(text or "").strip()
    if not t or not enabled():
        return None
    if t in _cache:
        return _cache[t]
    if time.time() < _fail_until:
        return None
    try:
        vec = embed_documents([t])[0]
        norm = _normalize(vec)
        if not norm:
            return None
        if len(_cache) >= 256:
            _cache.pop(next(iter(_cache)))
        _cache[t] = norm
        _fails = 0
        return norm
    except EmbeddingError as e:
        _fails += 1
        _fail_until = time.time() + min(600.0, 30.0 * (2 ** min(_fails, 4)))
        logger.warning("查询向量失败，本次只用钩子召回：%s", e)
        return None
