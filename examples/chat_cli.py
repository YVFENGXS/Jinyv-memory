# -*- coding: utf-8 -*-
"""带人设的命令行对话（真实模型）：python examples/chat_cli.py

环境变量：
  RP_FRONT_KEY / RP_BACK_KEY   必填
  RP_FRONT_MODEL / RP_BACK_MODEL / RP_LLM_BASE   选填
  RP_DB（默认 data/rp_memory.db）/ RP_SID / RP_CONTINUITY
命令：
  /new   开新会话（同 continuity，跨会话记忆生效）
  /quit  退出（冲刷尾部归档）
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jinyu_memory" / "core"))

import os

from host import JinyuHost


def main():
    if not (os.environ.get("RP_FRONT_KEY") and os.environ.get("RP_BACK_KEY")):
        sys.exit("缺少密钥：请先 export RP_FRONT_KEY / RP_BACK_KEY")
    host = JinyuHost(db_path=os.environ.get("RP_DB") or "data/rp_memory.db",
                     sid=os.environ.get("RP_SID") or None)
    print(f"[会话 {host.sid}｜人设 {host.persona['name']}｜/new 换会话，/quit 退出]")
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
                print(f"[新会话 {host.new_session()}]")
                continue
            print(f"{host.persona['name']} > {host.chat(text)}")
    finally:
        host.close()
        print(f"\n[退出] 归档失败区间 {len(host.db.list_failures())} 个"
              f"｜水位线 {host.mw.watermarks}")


if __name__ == "__main__":
    main()
