# -*- coding: utf-8 -*-
"""本地观测终端：在你自己的电脑上透视云服务器上的机器人。

用法（在仓库根目录）：
    python tools/monitor.py follow      实时滚动：谁在说话 / 前台生成了什么 / 后台归纳了什么
    python tools/monitor.py status      服务状态：systemd / NapCat / WS 连接 / 最近活动 / 报错数
    python tools/monitor.py sessions    记忆台账：每个会话的轮数、卡片数、承诺、热窗口、失败
    python tools/monitor.py qr          拉取最新登录二维码并自动打开（等刷新，约 1~2 分钟）
    python tools/monitor.py restart     重启 NoneBot 服务（默认 qqbot 单元，不动 NapCat）

必须设置环境变量：RP_SSH_HOST（服务器地址）、RP_SSH_USER（登录用户，
默认 ubuntu）；RP_SSH_KEY 默认读 ~/.ssh/id_ed25519。
本机需要 OpenSSH 客户端，且已把公钥装进服务器。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOST = os.environ.get("RP_SSH_HOST", "")
USER = os.environ.get("RP_SSH_USER", "ubuntu")
KEY = os.environ.get("RP_SSH_KEY", str(Path.home() / ".ssh" / "id_ed25519"))
UNIT = os.environ.get("RP_UNIT", "qqbot")
QR_REMOTE = os.environ.get(
    "RP_QR_REMOTE",
    "/root/Napcat/opt/QQ/resources/app/app_launcher/napcat/cache/qrcode.png")
QR_LOCAL = Path(__file__).resolve().parent.parent / "login_qrcode.png"

os.system("")                                  # Windows 终端开启 ANSI 颜色
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

# ---------------------------------------------------------------- 颜色
C = {"dim": "\033[2m", "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
     "cyan": "\033[36m", "white": "\033[97m", "bold": "\033[1m", "reset": "\033[0m"}


def paint(s: str, *styles) -> str:
    return "".join(C[x] for x in styles) + s + C["reset"]


def ssh(script: str, timeout: int = 90) -> str:
    """在服务器上跑一段 bash，返回 stdout 文本。"""
    cmd = ["ssh", "-i", KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
           "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=NUL",
           f"{USER}@{HOST}", script]
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"ssh 失败 rc={p.returncode}: {err[:300]}")
    return p.stdout.decode("utf-8", "replace")


# ---------------------------------------------------------------- follow

def _classify(line: str) -> str:
    """一行日志 → 带颜色的展示行。"""
    if "[jinyu]" in line:
        for kw, style in (("前台生成", ("green",)), ("归纳完成", ("cyan",)),
                          ("会话轮换", ("cyan",)), ("继承", ("cyan",)),
                          ("贴纸入库", ("cyan",)), ("connected", ("cyan",))):
            if kw in line:
                return paint(line, *style)
        if any(k in line for k in ("失败", "违禁", "异常", "Traceback", "仍违规",
                                   "低置信", "归档落库")):
            return paint(line, "red")
        if "WARNING" in line:
            return paint(line, "yellow")
        return paint(line, "dim")
    if "群消息" in line or "私聊消息" in line:
        return paint(line, "white", "bold")
    if "ERROR" in line or "Traceback" in line:
        return paint(line, "red", "bold")
    if "WebSocket" in line or "connected" in line:
        return paint(line, "cyan")
    return line


def cmd_follow() -> None:
    cmd = ["ssh", "-i", KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
           "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=NUL",
           f"{USER}@{HOST}", f"sudo journalctl -u {UNIT} -f -n 40 -o cat"]
    print(paint(f"实时观测 {USER}@{HOST}（Ctrl+C 退出）", "bold"))
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    assert p.stdout is not None
    try:
        for raw in p.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line:
                print(_classify(line))
    except KeyboardInterrupt:
        pass
    finally:
        p.terminate()


# ---------------------------------------------------------------- status

def cmd_status() -> None:
    out = ssh(
        f"echo ACTIVE=$(systemctl is-active {UNIT})"
        f"; echo SINCE=$(systemctl show {UNIT} -p ActiveEnterTimestamp --value)"
        f"; echo SCREEN=$(sudo screen -ls | grep -c napcat)"
        f"; echo WS=$(sudo journalctl -u {UNIT} --no-pager -o cat | "
        f"grep -E 'Bot .* connected' | tail -1)"
        f"; echo FAIL=$(sudo journalctl -u {UNIT} --since '24 hours ago' --no-pager -o cat | "
        f"grep -cE 'ERROR|生成失败|归档失败' || true)"
        f"; echo ---RECENT---"
        f"; sudo journalctl -u {UNIT} --no-pager -o cat | grep -F '[jinyu]' | tail -12")
    lines = out.strip().splitlines()
    fields = {}
    rest = []
    for ln in lines:
        if ln.startswith("---"):
            rest = lines[lines.index(ln) + 1:]
            break
        k, _, v = ln.partition("=")
        fields[k] = v
    active = fields.get("ACTIVE", "?")
    print(paint("── 服务状态 " + "─" * 40, "bold"))
    print(f"  NoneBot   {paint('运行中', 'green') if active == 'active' else paint(active, 'red')}"
          f"（自 {fields.get('SINCE', '?')} 起）")
    nap = fields.get("SCREEN", "0")
    print(f"  NapCat    {'在 screen 里运行' if nap != '0' else paint('没有 screen 会话！', 'red')}")
    print(f"  WS        {fields.get('WS', '?')}")
    print(f"  24h 报错  {fields.get('FAIL', '?')} 条")
    print(paint("── 最近活动 " + "─" * 40, "bold"))
    for ln in rest:
        if ln:
            print("  " + _classify(ln))


# ---------------------------------------------------------------- sessions

def cmd_sessions() -> None:
    out = ssh(
        "DB=/opt/qqbot/data/rp_memory.db"
        "; echo '@@@meta'; sudo sqlite3 $DB \"SELECT session_id||'|'||character_id||'|'"
        "||closed||'|'||seq FROM session_meta;\""
        "; echo '@@@epoch'; sudo sqlite3 $DB \"SELECT base_sid||'|'||character_id||'|'"
        "||epoch||'|'||last_active FROM session_epoch;\""
        "; echo '@@@turns'; sudo sqlite3 $DB \"SELECT session_id||'|'||character_id||'|'"
        "||COUNT(*)||'|'||MAX(turn_idx)||'|'||MAX(created_at) FROM turns"
        " GROUP BY session_id, character_id;\""
        "; echo '@@@cards'; sudo sqlite3 $DB \"SELECT session_id||'|'||character_id||'|'"
        "||COUNT(*) FROM event_cards GROUP BY session_id, character_id;\""
        "; echo '@@@cursor'; sudo sqlite3 $DB \"SELECT session_id||'|'||character_id||'|'"
        "||summarized_until_turn FROM archive_cursor;\""
        "; echo '@@@ledger'; sudo sqlite3 $DB \"SELECT session_id||'|'||character_id||'|'"
        "||ledger_json FROM state_ledger;\""
        "; echo '@@@fail'; sudo sqlite3 $DB \"SELECT session_id||'|'||COUNT(*)"
        " FROM archive_failures GROUP BY session_id;\""
        "; echo '@@@facts'; sudo sqlite3 $DB 'SELECT COUNT(*) FROM durable_facts;'")
    sec, meta, epoch, turns, cards, fail, facts = {}, [], [], [], [], [], 0
    cursor: dict = {}
    ledgers: dict = {}
    cur = sec
    for ln in out.splitlines():
        if ln.startswith("@@@"):
            cur = ln[3:]
            sec[cur] = []
            continue
        if cur and ln.strip():
            sec[cur].append(ln)
    for ln in sec.get("meta", []):
        meta.append(ln.split("|"))
    for ln in sec.get("epoch", []):
        epoch.append(ln.split("|"))
    for ln in sec.get("turns", []):
        p = ln.split("|")
        turns.append((p[0], p[1], int(p[2]), int(p[3]), p[4]))
    for ln in sec.get("cards", []):
        p = ln.split("|")
        cards.append((p[0], int(p[2])))
    for ln in sec.get("cursor", []):
        p = ln.split("|")
        cursor[(p[0], p[1])] = int(p[2])
    for ln in sec.get("ledger", []):
        p = ln.split("|", 2)
        try:
            ledgers[(p[0], p[1])] = json.loads(p[2])
        except ValueError:
            pass
    for ln in sec.get("fail", []):
        p = ln.split("|")
        fail.append((p[0], int(p[1])))
    if sec.get("facts"):
        try:
            facts = int(sec["facts"][0])
        except ValueError:
            facts = 0

    print(paint("── 会话台账 " + "─" * 48, "bold"))
    closed_meta = {(m[0], m[1]): m[2] for m in meta}
    ep_map = {(e[0], e[1]): e[2] for e in epoch}
    last_ep = {(e[0], e[1]): e[3] for e in epoch}
    fail_map = dict(fail)
    card_map = dict(cards)
    if not turns:
        print("  库里还没有对话记录")
        return
    hdr = f"  {'会话':<24}{'轮数':>4} {'卡':>3} {'热':>3} {'待承诺':>4} {'已兑':>4} {'strict':>6}  {'最后活动':<19}{'状态'}"
    print(paint(hdr, "dim"))
    for sid, cid, n, max_turn, at in sorted(turns, key=lambda t: t[4], reverse=True):
        lg = ledgers.get((sid, cid)) or {}
        ps = lg.get("promises") or []
        res = lg.get("resolved") or []
        strict = sum(1 for p in ps if p.get("strict"))
        cur_turn = cursor.get((sid, cid), 0)
        hot = max_turn - cur_turn
        is_ep = "#" in sid
        base = sid.rsplit("#", 1)[0] if is_ep else sid
        base_act = last_ep.get((base, cid), "")
        st = "closed" if closed_meta.get((sid, cid)) == "1" else "活跃"
        style = ("dim",) if st == "closed" else ("green",)
        fl = f"  {paint('失败x' + str(fail_map[sid]), 'red')}" if sid in fail_map else ""
        print(f"  {paint(sid, *style):<34}{n:>4} {card_map.get(sid, 0):>3} {hot:>3} "
              f"{len(ps):>4} {len(res):>4} {strict:>6}  {(at or base_act)[:19]:<19}{st}{fl}")
    if facts:
        print(f"\n  长期事实库 {facts} 条（跨 epoch 继承）")


# ---------------------------------------------------------------- qr / restart

def cmd_qr() -> None:
    print("等待二维码刷新（NapCat 每 1~2 分钟换一张，最多等 150s）…")
    out = ssh(
        f"f={QR_REMOTE}"
        "; old=$(sudo stat -c %Y $f)"
        "; i=0; while [ $i -lt 75 ]; do sleep 2; i=$((i+1))"
        "; new=$(sudo stat -c %Y $f); if [ $new != $old ]; then echo REFRESHED; break; fi; done"
        "; sudo cp $f /tmp/mon_qr.png; sudo chmod 644 /tmp/mon_qr.png"
        "; if [ $i -ge 75 ]; then echo STALE; fi", timeout=200)
    if "STALE" in out:
        print(paint("150s 内二维码没刷新：可能已经登录成功，或 NapCat 没在等扫码。"
                    "先用 python monitor.py status 看看。", "yellow"))
    ssh("sudo chmod 644 /tmp/mon_qr.png")
    subprocess.run(["scp", "-i", KEY, "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=NUL",
                    f"{USER}@{HOST}:/tmp/mon_qr.png", str(QR_LOCAL)], check=True,
                   capture_output=True)
    print(f"二维码已保存 {QR_LOCAL}")
    try:
        os.startfile(str(QR_LOCAL))            # Windows 自动打开图片
    except (AttributeError, OSError):
        print("请手动打开上面的文件扫码（手机 QQ 须登录机器人账号）。")


def cmd_restart() -> None:
    print("重启 NoneBot（NapCat 不动，WS 会自动重连）…")
    ssh(f"sudo systemctl restart {UNIT}")
    time.sleep(4)
    st = ssh(f"systemctl is-active {UNIT}").strip()
    print(paint(f"服务状态: {st}", "green") if st == "active" else paint(f"服务状态: {st}", "red"))
    print("约 30~60s 后 WS 重连；用 python monitor.py follow 观察。")


# ---------------------------------------------------------------- main

def main() -> None:
    cmds = {"follow": cmd_follow, "status": cmd_status, "sessions": cmd_sessions,
            "qr": cmd_qr, "restart": cmd_restart}
    name = sys.argv[1] if len(sys.argv) > 1 else "follow"
    if name in ("-h", "--help", "help") or name not in cmds:
        print(__doc__)
        return
    if not HOST:
        print(paint("缺少 RP_SSH_HOST：先 export RP_SSH_HOST=<服务器地址>", "red"))
        return
    try:
        cmds[name]()
    except KeyboardInterrupt:
        pass
    except FileNotFoundError:
        print("找不到 ssh/scp：请确认本机已装 OpenSSH 客户端。")
    except RuntimeError as exc:
        print(paint(str(exc), "red"))
        print(f"排查：检查 RP_SSH_HOST / RP_SSH_USER / RP_SSH_KEY={KEY}，或先试 "
              f"ssh -i {KEY} {USER}@{HOST} true")


if __name__ == "__main__":
    main()
