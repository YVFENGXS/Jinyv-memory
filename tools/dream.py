# -*- coding: utf-8 -*-
"""手动跑一次 Dream 整理（默认只预览，不写库）。

  python tools/dream.py --db data/rp_memory.db            # 预览：过期条数 + 待整理会话
  python tools/dream.py --db data/rp_memory.db --apply    # 真跑：软删过期 + 合并同义词条

说明：
- 预览不调后台模型（省 token），只给候选；真跑才走 merge_facts 的判重链路。
- 不传 --apply 时任何行都不会被修改，也不占用 dream_runs 的当日坑位。
"""
import argparse
import os
import sys
from pathlib import Path

# 仓库里是 jinyu_memory/core，线上插件目录是 core 平铺，两种都挂上。
_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT, _ROOT / "jinyu_memory", _ROOT / "core",
           _ROOT / "jinyu_memory" / "core", _ROOT / "examples"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import dream
from llm import LLM
from rp_skeleton import DB, RPContextMiddleware


def main():
    ap = argparse.ArgumentParser(description="Dream 轻量整理（过期临时事实 + 同义词条）")
    ap.add_argument("--db", default=str(_ROOT / "data" / "rp_memory.db"))
    ap.add_argument("--cid", default="jinyu")
    ap.add_argument("--apply", action="store_true", help="不加则只预览")
    ap.add_argument("--sessions", type=int, default=8)
    ap.add_argument("--min-facts", type=int, default=20)
    ap.add_argument("--day", default=None, help="占坑日期 YYYY-MM-DD，默认今天")
    args = ap.parse_args()

    key = os.environ.get("RP_BACK_KEY") or ""
    if args.apply and not key:
        sys.exit("缺少 RP_BACK_KEY：真跑需要后台模型判重；只预览可以不带 key")
    back = LLM(key, os.environ.get("RP_BACK_MODEL") or "deepseek-flash",
               os.environ.get("RP_LLM_BASE") or None, role="back") if key else None
    db = DB(args.db)
    mw = RPContextMiddleware(None, back, db)      # 只用到后台判重，不需要前台模型

    stats = dream.run(mw, args.cid, dry_run=not args.apply, day=args.day,
                      min_facts=args.min_facts, max_sessions=args.sessions)
    if stats.get("skipped"):
        print(f"跳过：{stats['skipped']}")
        return
    print(f"日期={stats['day']} 模式={'预览' if stats['dry_run'] else '执行'}")
    print(f"过期临时事实 {len(stats['expired'])} 条"
          + ("（预览，未修改）" if stats["dry_run"] else "（已标记退场）"))
    for fact in stats["expired"][:10]:
        print("  -", fact[:60])
    print(f"待整理会话 {stats['sessions']} 个"
          + ("（预览不调模型）" if stats["dry_run"] else f"，合并 {stats['groups']} 组"))


if __name__ == "__main__":
    main()
