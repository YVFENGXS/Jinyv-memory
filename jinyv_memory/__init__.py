# -*- coding: utf-8 -*-
"""瑾瑜记忆中间层（framework-agnostic core）。

本包只负责记忆本身：
- 热窗口 / 事件卡 / 长期事实 / 召回 / 归档游标
- 人设护栏、输出中间层、压缩与轮换

平台接入（QQ、KOOK、桌宠、通话、DSH）在独立仓库 Jinyv-plugins：
https://github.com/YVFENGXS/Jinyv-plugins

同进程宿主：
    from jinyv_memory import JinyuHost

    host = JinyuHost(db_path="data/rp_memory.db", sid="main")
    print(host.chat("在吗"))
    host.close()

跨进程宿主：`python tools/serve.py --port 8710 --db data/rp_memory.db`
（HTTP 接口 /chat /remember /recall，除 /health 外都要 Bearer 令牌）
"""
from __future__ import annotations

import sys
from pathlib import Path

__version__ = "0.4.0"

# core 内部是按扁平模块互相导入的（import llm / from persona import ...），
# 把 core 目录放进 sys.path 才能以 `from jinyv_memory import JinyuHost` 使用。
CORE = Path(__file__).resolve().parent / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from host import JinyuHost  # noqa: E402

__all__ = ["JinyuHost", "CORE", "__version__"]
