# -*- coding: utf-8 -*-
"""上下文水位与工作预算（C1-01）。

1M 只是模型宣称上限，不是日常水位分母：
    WORK_BUDGET = min(MODEL_MAX, PROVIDER_MAX, 成本预算, 延迟预算)
    USABLE      = WORK_BUDGET - RESERVE_OUTPUT - RESERVE_FIXED
    HIGH        = USABLE * 0.75   # 开始压缩
    LOW         = USABLE * 0.55   # 停止压缩
    HARD        = WORK_BUDGET     # 绝不能超

所有值都从环境变量读取，默认与方案第 15 节一致；未开启压缩时
调用方仍走原有 WARN/PRE/HARD/FUSE 水位线，互不影响。
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _num(name, default, cast=float):
    try:
        return cast(float(os.environ.get(name) or default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Budget:
    model_max: int = 1_000_000
    provider_max: int = 1_000_000
    work_budget: int = 65_536
    reserve_output: int = 4_096
    reserve_fixed: int = 6_000
    high_ratio: float = 0.75
    low_ratio: float = 0.55

    @property
    def usable(self) -> int:
        return max(1, self.work_budget - self.reserve_output - self.reserve_fixed)

    @property
    def high(self) -> int:
        return int(self.usable * self.high_ratio)

    @property
    def low(self) -> int:
        return int(self.usable * self.low_ratio)

    @property
    def hard(self) -> int:
        return self.work_budget

    def level(self, used: int) -> str:
        """compress / low / normal / over。"""
        if used >= self.hard:
            return "over"
        if used >= self.high:
            return "compress"
        if used <= self.low:
            return "low"
        return "normal"

    def section_budgets(self) -> dict:
        """方案 5.4 的分区预算（占 USABLE 比例）。"""
        u = self.usable
        return {
            "open_atoms_and_recent": int(u * 0.45),
            "closed_cards": int(u * 0.20),
            "recent_summary": int(u * 0.10),
            "autonomy_digest": int(u * 0.05),
            "safety": int(u * 0.10),
        }


def load_budget() -> Budget:
    model_max = int(_num("RP_CONTEXT_MODEL_MAX", 1_000_000))
    provider_max = int(_num("RP_CONTEXT_PROVIDER_MAX", model_max))
    work = int(_num("RP_CONTEXT_WORK_BUDGET",
                    os.environ.get("RP_MAX_TOKENS") or 65_536))
    work = min(work, model_max, provider_max)
    out = int(_num("RP_CONTEXT_RESERVE_OUTPUT",
                   os.environ.get("RP_OUTPUT_RESERVE") or 4_096))
    fixed = int(_num("RP_CONTEXT_RESERVE_FIXED", 6_000))
    # 预留不能吃光预算，否则 USABLE<=0、水位永远 HARD
    if out + fixed >= work:
        fixed = max(0, work // 10)
        out = max(0, work - fixed - 1)
    high = max(0.05, min(0.99, _num("RP_CONTEXT_HIGH", 0.75)))
    low = max(0.05, min(high, _num("RP_CONTEXT_LOW", 0.55)))
    return Budget(model_max=model_max, provider_max=provider_max,
                  work_budget=work, reserve_output=out, reserve_fixed=fixed,
                  high_ratio=high, low_ratio=low)


def usage_tokens(usage) -> dict:
    """把 API 返回的 usage 归一成固定字段；缺字段按 0。"""
    usage = usage or {}
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    cached = int(usage.get("cache_hit_tokens")
                 or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                 or 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "cache_hit_tokens": cached, "total_tokens": prompt + completion}


def should_compress(used: int, budget: Budget) -> bool:
    return used >= budget.high


def should_stop(used: int, budget: Budget) -> bool:
    return used <= budget.low
