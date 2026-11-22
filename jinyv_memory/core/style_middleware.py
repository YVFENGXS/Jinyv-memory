# -*- coding: utf-8 -*-
"""模型输出与发送之间的确定性中间层（P0）。

不依赖 prompt，也不改记忆/召回。只做：
- 回合分类：closing / ack / share / comfort / invite / question / request / chat
- 动作白名单：没被邀请就删动作；允许时最多一个，且不放在开头
- 长度 / 句数 / 段落硬门禁
- 删结尾总结句、短回复去句号、禁止不合时宜的追问
- 与最近回复的重复门禁
- 不合格时可选重采样（best-of-2）
- journal + JSONL 留痕，供后续训练判别器 / 改写器

调用方：rp_skeleton.chat() 在最终回复落库前调用 apply()。
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

logger = logging.getLogger("rp_memory.style")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(float(os.environ.get(name) or default))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


ON = _env_bool("RP_STYLE_MIDDLE", True)
BESTN = _env_int("RP_STYLE_BESTN", 2, 1, 4)
JSONL = _env_bool("RP_STYLE_JSONL", True)
MAX_LOG_BYTES = 20 * 1024 * 1024
DUP_RATIO = float(os.environ.get("RP_STYLE_DUP_RATIO") or 0.55)
ACTION_MODE = (os.environ.get("RP_STYLE_ACTION") or "strict").strip().lower()

CJK = re.compile(r"[\u4e00-\u9fff]")
ACTION = re.compile(r"[（(][^）)]{0,120}[）)]")
PROTECT = re.compile(r"\[\[[^\]]{0,160}\]\]")
TEMPLATE = re.compile(
    r"(总的来说|总而言之|总之|简而言之|说到底|值得注意的是|"
    r"首先|其次|最后|不仅.{0,20}而且|愿你|希望你|记住)")
SUMMARY_END = re.compile(
    r"^(总之|总的来说|简而言之|说到底|所以|记住|以后|下次|希望|愿你)")
CLOSING = re.compile(
    r"(晚安|睡了|先这样|拜拜|再见|不聊了|我走了|下线|去忙|先不聊|回头聊|明天见|我去睡)")
ACK = re.compile(
    r"^(嗯+|哦+|好+|行|对|是|可以|知道了|收到|ok|OK|哈哈+|草|呢|在|没事)"
    r"[哦啊呀啦嘛吧]*[。.!！?？…~]*$")
GREETING = re.compile(
    r"^(在吗|在不在|在么|睡了吗|醒了吗|忙吗|有空吗)[。.!！?？…~]*$")
COMFORT = re.compile(
    r"难过|委屈|想哭|哭了|难受|累了|好累|有点累|很累|太累|真累|累死|"
    r"疼|害怕|焦虑|失眠|睡不着|心慌")
SHARE = re.compile(r"(今天|刚才|我在|我发现|我跟你说|我遇到|我跟你讲|你知道吗)")
QUESTION = re.compile(r"(\?|？|吗|呢|怎么|为什么|哪|谁|多少|几时|什么时候)")
REQUEST = re.compile(r"(帮我|给我|查|找|写|做|改|翻译|搜|看一下|讲讲|说说|解释|推荐)")
LONG_REQUEST = re.compile(
    r"(详细|展开|多说|写一段|写一篇|解释|说明|分析|总结|翻译|代码|步骤|列表|多少|几个|区别|对比)")
INVITE = re.compile(
    r"(抱|抱抱|抱一下|亲亲|摸摸|摸头|击掌|碰拳|握手|贴贴|蹭蹭|靠一下|躺|睡|拍|戳|揉|捏)")

_SENT_SPLIT = re.compile(r"(?<=[。！？!?…~])")
_PARA_SPLIT = re.compile(r"\n+")

_lock = threading.Lock()
_announced = False


def _limits(kind: str) -> dict:
    table = {
        "closing": dict(chars=12, sents=1, paras=1, action=False, question=False),
        "ack": dict(chars=12, sents=1, paras=1, action=False, question=False),
        "share": dict(chars=45, sents=2, paras=2, action=False, question=False),
        "comfort": dict(chars=70, sents=3, paras=2, action=True, question=False),
        "invite": dict(chars=30, sents=2, paras=1, action=True, question=False),
        "question": dict(chars=120, sents=3, paras=2, action=False, question=True),
        "request": dict(chars=220, sents=4, paras=3, action=False, question=True),
        "long": dict(chars=800, sents=10, paras=6, action=False, question=True),
        "chat": dict(chars=90, sents=3, paras=2, action=False, question=True),
    }
    return table.get(kind, table["chat"])


def classify(query: str) -> str:
    q = str(query or "").strip()
    if not q:
        return "chat"
    if CLOSING.search(q):
        return "closing"
    if GREETING.fullmatch(q) or ACK.fullmatch(q):
        return "ack"
    if COMFORT.search(q):
        return "comfort"
    if INVITE.search(q) and len(CJK.findall(q)) <= 8:
        return "invite"
    if LONG_REQUEST.search(q):
        return "long"
    if QUESTION.search(q):
        return "question"
    if REQUEST.search(q):
        return "request"
    if SHARE.search(q):
        return "share"
    return "chat"


def _sents(text: str) -> list:
    return [p for p in _SENT_SPLIT.split(text or "") if p.strip()]


def _paras(text: str) -> list:
    return [p for p in _PARA_SPLIT.split(text or "") if p.strip()]


def _protect(text: str) -> tuple:
    marks = []

    def repl(m):
        marks.append(m.group(0))
        return f"\x00{len(marks) - 1}\x00"

    return PROTECT.sub(repl, text or ""), marks


def _restore(text: str, marks: list) -> str:
    def repl(m):
        index = int(m.group(1))
        return marks[index] if 0 <= index < len(marks) else ""

    return re.sub(r"\x00(\d+)\x00", repl, text or "")


def _action_budget(kind: str, query: str, recent: list) -> int:
    lim = _limits(kind)
    invited = bool(INVITE.search(query or ""))
    if not lim["action"]:
        if ACTION_MODE == "strict":
            return 0
        # normal：普通聊天允许一个动作，但上一轮已有动作时不再叠加
        if recent and ACTION.search(recent[0] or ""):
            return 0
        return 1
    if kind == "invite" or invited:
        return 1
    if recent and ACTION.search(recent[0] or ""):
        return 0
    return 1


def _clean_actions(text: str, budget: int) -> str:
    text = text or ""
    m = ACTION.match(text.strip())
    if m:
        action = m.group(0)
        rest = text.strip()[m.end():].lstrip()
        if budget <= 0:
            text = rest
        elif rest:
            text = rest.rstrip() + " " + action
        else:
            text = action
    count = 0

    def repl(match):
        nonlocal count
        count += 1
        return match.group(0) if count <= budget else ""

    return ACTION.sub(repl, text)


def _drop_summary(text: str) -> str:
    parts = _sents(text)
    if len(parts) >= 2 and SUMMARY_END.match(parts[-1].strip()):
        return "".join(parts[:-1]).rstrip()
    return text


def _drop_question(text: str, kind: str) -> str:
    if _limits(kind)["question"]:
        return text
    parts = [p for p in _sents(text) if not QUESTION.search(p)]
    return "".join(parts).rstrip()


def _cap_sentences(text: str, limit: int) -> str:
    parts = _sents(text)
    if len(parts) <= limit:
        return text
    return "".join(parts[:limit]).rstrip()


def _cap_paras(text: str, limit: int) -> str:
    parts = _paras(text)
    return "\n".join(parts[:limit])


def _clip_chars(text: str, limit: int) -> str:
    if len(CJK.findall(text)) <= limit:
        return text
    out = []
    count = 0
    for ch in text:
        out.append(ch)
        if CJK.match(ch):
            count += 1
        if count >= limit:
            break
    clipped = "".join(out)
    cut = max(clipped.rfind(x) for x in "。！？…，、")
    if cut >= limit // 2:
        clipped = clipped[:cut + 1]
    return clipped.rstrip("，、") + "…"


def _strip_final_period(text: str, kind: str) -> str:
    if kind in ("closing", "ack") or len(CJK.findall(text)) <= 20:
        return re.sub(r"。\s*$", "", text or "").rstrip()
    return text


def _norm(text: str) -> str:
    return re.sub(r"[\s，。！？!?…~、,.\"'“”‘’（）()\[\]【】]+", "", text or "").lower()


def _dup_ratio(text: str, recent: list) -> float:
    a = _norm(text)
    best = 0.0
    for old in (recent or [])[:8]:
        b = _norm(str(old or ""))
        if not a or not b:
            continue
        best = max(best, difflib.SequenceMatcher(None, a, b).ratio())
    return best


def _fallback(kind: str, query: str) -> str:
    q = str(query or "")
    if kind == "closing":
        if "晚安" in q or "睡" in q:
            return "晚安"
        if "拜" in q or "再见" in q:
            return "拜拜"
        return "嗯，去吧"
    if kind == "ack":
        if GREETING.fullmatch(q):
            return "在呢"
        return "嗯嗯"
    if kind == "invite":
        if "摸" in q:
            return "摸吧"
        return "过来吧"
    return "嗯"


def clean(text: str, kind: str, query: str, recent: list) -> str:
    lim = _limits(kind)
    budget = _action_budget(kind, query, recent)
    text = _drop_summary(text)
    text = _cap_paras(text, lim["paras"])
    text = _drop_question(text, kind)
    text = _cap_sentences(text, lim["sents"])
    text = _clean_actions(text, budget)
    text = _clip_chars(text, lim["chars"])
    text = _strip_final_period(text, kind)
    text = re.sub(r"[ \t]+", " ", text or "").strip()
    if not text:
        text = _fallback(kind, query)
    return text


def _score(text: str, kind: str, query: str, recent: list, budget: int) -> tuple:
    lim = _limits(kind)
    score = 0
    tags = []
    if ACTION.match((text or "").strip()):
        score += 3
        tags.append("action_open")
    actions = len(ACTION.findall(text or ""))
    if actions > budget:
        score += 2
        tags.append("action_over")
    if len(_paras(text)) > lim["paras"]:
        score += 2
        tags.append("para_over")
    if len(_sents(text)) > lim["sents"]:
        score += 2
        tags.append("sent_over")
    if len(CJK.findall(text or "")) > lim["chars"]:
        score += 2
        tags.append("char_over")
    if TEMPLATE.search(text or ""):
        score += 3
        tags.append("template")
    parts = _sents(text)
    if len(parts) >= 2 and SUMMARY_END.match(parts[-1].strip()):
        score += 2
        tags.append("summary_end")
    if not lim["question"] and QUESTION.search(text or ""):
        score += 2
        tags.append("question")
    if (text or "").strip().endswith("。"):
        score += 1
        tags.append("period")
    ratio = _dup_ratio(text, recent)
    if ratio >= DUP_RATIO and len(CJK.findall(text or "")) >= 8:
        score += 5
        tags.append(f"dup:{ratio:.2f}")
    if (text or "").count("主人") >= 2:
        score += 1
        tags.append("owner_dense")
    if kind == "invite" and not ACTION.search(text or "") \
            and len(CJK.findall(text or "")) <= 2:
        score += 3
        tags.append("invite_cold")
    return score, tags


def _recent(db, sid: str, cid: str, limit: int = 8) -> list:
    if db is None:
        return []
    try:
        hi = int(db.max_turn(sid, cid))
        rows = db.get_turns(sid, cid, lo=max(1, hi - 20), hi=hi)
        out = [str(r.get("content") or "") for r in rows
               if str(r.get("role") or "") == "assistant"]
        return list(reversed(out[-limit:]))
    except Exception:
        return []


def _log_path() -> Optional[Path]:
    raw = (os.environ.get("RP_STYLE_LOG_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser()
    db = (os.environ.get("RP_DB") or "").strip()
    if not db:
        return None
    return Path(db).expanduser().with_name("style_middle.jsonl")


def _log(sid: str, kind: str, query: str, original: str,
         cands: list, best: dict) -> None:
    try:
        logger.info(
            "中间层 sid=%s kind=%s 候选=%d 终分=%d 原=%d字 终=%d字 标签=%s",
            sid, kind, len(cands), best["score"], len(original),
            len(best["text"]), ",".join(best["tags"][:8]) or "-")
    except Exception:
        pass
    if not JSONL:
        return
    path = _log_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
                backup = path.with_name(path.name + ".1")
                try:
                    if backup.exists():
                        backup.unlink()
                    path.replace(backup)
                except OSError:
                    pass
            record = {
                "ts": int(time.time()),
                "sid": str(sid),
                "kind": kind,
                "query": str(query or "")[:200],
                "original": str(original or "")[:600],
                "final": str(best.get("text") or "")[:600],
                "score": best.get("score", 0),
                "tags": best.get("tags", []),
                "candidates": [
                    {"text": str(c.get("text") or "")[:600],
                     "score": c.get("score", 0),
                     "tags": c.get("tags", [])}
                    for c in (cands or [])[:4]
                ],
                "version": "p0",
            }
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:                       # noqa: BLE001 写日志失败不能影响回复
        logger.warning("中间层写 JSONL 失败 path=%s：%s", path, exc)


def apply(reply, query, sid: str = "", cid: str = "", db=None,
          regenerate: Optional[Callable[[], str]] = None,
          allow_resample: bool = True,
          recent: Optional[Iterable[str]] = None) -> str:
    """对一条模型回复做干预，返回最终要发送的文本。

    regenerate 只在首条不合格且 allow_resample 时调用；调用方应保证它
    不执行工具、不产生副作用（例如直接用同一组 messages 再请求一次）。
    """
    global _announced
    if not ON:
        return reply
    text = str(reply or "").strip()
    if not text or "[[pass]]" in text:
        return reply
    if not _announced:
        logger.info("中间层已启用 version=p0 bestn=%d action=%s", BESTN, ACTION_MODE)
        _announced = True
    try:
        recent_list = list(recent) if recent is not None else _recent(db, sid, cid)
        kind = classify(query)
        budget = _action_budget(kind, query, recent_list)
        original = text

        protected, marks = _protect(original)
        cleaned = _restore(clean(protected, kind, query, recent_list), marks)

        def evaluate(value: str) -> dict:
            score, tags = _score(value, kind, query, recent_list, budget)
            return {"text": value, "score": score, "tags": tags}

        cands = [evaluate(original), evaluate(cleaned)]
        best_score = min(c["score"] for c in cands)
        if allow_resample and regenerate and BESTN > 1 and best_score > 0:
            for _ in range(BESTN - 1):
                try:
                    extra = str(regenerate() or "").strip()
                except Exception as exc:          # noqa: BLE001 重采样失败不阻断
                    logger.warning("中间层重采样失败 sid=%s：%s", sid, exc)
                    break
                if not extra or "[[pass]]" in extra:
                    continue
                p2, m2 = _protect(extra)
                ctext = _restore(clean(p2, kind, query, recent_list), m2)
                cands.append(evaluate(ctext))

        cands.sort(key=lambda c: (c["score"], len(c["text"])))
        best = cands[0]
        need_fallback = best["score"] > 0 and kind in ("closing", "ack")
        if kind == "closing" and not re.search(
                r"(晚安|拜拜|再见|明天见|好梦|去睡|早点睡|睡吧)", best["text"]):
            need_fallback = True
        if kind == "invite" and not ACTION.search(best["text"]) \
                and len(CJK.findall(best["text"])) <= 2:
            need_fallback = True
        if need_fallback:
            fallback = _fallback(kind, query)
            if fallback:
                cands.append(evaluate(fallback))
                cands.sort(key=lambda c: (c["score"], len(c["text"])))
                best = cands[0]

        _log(sid, kind, query, original, cands, best)
        return best["text"]
    except Exception as exc:                       # noqa: BLE001 中间层失败退回原文
        logger.warning("中间层异常 sid=%s：%s", sid, exc)
        return reply
