# -*- coding: utf-8 -*-
"""带人设的命令行对话（真实模型）：python examples/chat_cli.py

环境变量：
  RP_FRONT_KEY / RP_BACK_KEY   必填
  RP_FRONT_MODEL / RP_BACK_MODEL / RP_LLM_BASE   选填
  RP_DB（默认 chat_memory.db）/ RP_SID / RP_CONTINUITY
命令：
  /new   开新会话（同 continuity，跨会话记忆生效）
  /quit  退出（冲刷尾部归档）
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jinyu_memory" / "core"))

import os
import sys
import uuid

from llm import from_env
from persona import PERSONA
from rp_skeleton import DB, RPContextMiddleware

# 人设单源在 persona.py，命令行与 QQ 插件共用同一份


def main():
    if not (os.environ.get("RP_FRONT_KEY") and os.environ.get("RP_BACK_KEY")):
        sys.exit("缺少密钥：请先 export RP_FRONT_KEY / RP_BACK_KEY")
    db = DB(os.environ.get("RP_DB") or "chat_memory.db")
    mw = RPContextMiddleware(from_env("front"), from_env("back"), db,
                             max_tokens=64000, output_reserve=4096, profile="cl100k")
    cont = os.environ.get("RP_CONTINUITY") or "whale-girl-main"
    cid = "whale_girl"
    sid = os.environ.get("RP_SID") or "cli-" + uuid.uuid4().hex[:8]
    mw.open_session(sid, cid, cont)
    print(f"[会话 {sid}｜人设 {PERSONA['name']}｜/new 换会话，/quit 退出]")
    try:
        while True:
            try:
                text = input("你 > ").strip()
            except EOFError:
                break
            if not text:
                continue
            if text in ("/quit", "/exit"):
                break
            if text == "/new":
                mw.close_session(sid, cid)
                sid = "cli-" + uuid.uuid4().hex[:8]
                mw.open_session(sid, cid, cont)
                print(f"[新会话 {sid}]")
                continue
            print(f"{PERSONA['name']} > {mw.chat(text, PERSONA, sid, cid)}")
    finally:
        mw.shutdown()
        print(f"\n[退出] 归档失败区间 {len(db.list_failures())} 个｜水位线 {mw.watermarks}")


if __name__ == "__main__":
    main()


