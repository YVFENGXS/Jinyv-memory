# -*- coding: utf-8 -*-
"""输出中间层（core/style_middleware.py）回归。

重点验证「不靠 prompt」的确定性干预：
- 回合分类（closing / ack / invite）
- 没被邀请时删动作、短回复去句号
- 结尾总结句、模板句、重复句降分
"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_CORE = _ROOT / "jinyv_memory" / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

os.environ["RP_STYLE_MIDDLE"] = "1"
os.environ["RP_STYLE_JSONL"] = "0"
os.environ["RP_STYLE_BESTN"] = "1"

import style_middleware as sm


def test_classify():
    assert sm.classify("晚安") == "closing"
    assert sm.classify("抱抱我") == "invite"
    assert sm.classify("在吗") == "ack"
    assert sm.classify("嗯") == "ack"
    assert sm.classify("今天有点累") == "comfort"
    assert sm.classify("帮我查一下明天的天气") == "request"
    assert sm.classify("详细解释一下这个概念") == "long"
    print("S1 回合分类: OK")


def test_closing_strips_action_and_summary():
    raw = "（尾巴轻轻卷了一下）主人今天辛苦了。早点睡，明天才有精神。总之，瑾瑜会一直在这儿。"
    out = sm.apply(raw, "晚安", recent=[])
    assert "总之" not in out, out
    assert len(out) <= 20, out
    assert ("晚安" in out) or ("睡" in out) or ("好梦" in out), out
    print("S2 晚安短回复: OK", out)


def test_uninvited_action_removed():
    out = sm.apply("（尾巴晃了晃）我在。", "在吗", recent=[])
    assert "尾巴" not in out, out
    print("S3 未受邀动作删除: OK", out)


def test_invite_action_allowed():
    out = sm.apply("（尾鳍轻轻拍了拍水面）过来吧。", "抱抱我", recent=[])
    assert out, out
    assert "过来" in out or "抱" in out, out
    print("S4 受邀动作保留: OK", out)


def test_duplicate_suppressed():
    out = sm.apply("在呢，瑾瑜一直在。", "在吗", recent=["在呢，瑾瑜一直在。"])
    assert "瑾瑜一直在" not in out, out
    print("S5 重复门禁: OK", out)


if __name__ == "__main__":
    test_classify()
    test_closing_strips_action_and_summary()
    test_uninvited_action_removed()
    test_invite_action_allowed()
    test_duplicate_suppressed()
    print("\nstyle middleware tests passed")
