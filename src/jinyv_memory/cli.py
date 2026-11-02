# -*- coding: utf-8 -*-
"""命令行入口：

  python -m jinyv_memory.cli selfcheck        # 离线自检（临时数据目录）
  python -m jinyv_memory.cli plugins          # 插件发现/健康
  python -m jinyv_memory.cli chat             # 无 QQ 的本地对话
  python -m jinyv_memory.cli run              # 常驻，运行定时任务
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _runtime(args):
    from .runtime import JinyvRuntime

    return JinyvRuntime(
        args.root,
        data_dir=args.data_dir,
        mock_llm=args.mock,
    )


def _print_plugins(runtime) -> None:
    rows = runtime.loader.health()
    print(f"{'PLUGIN':<24} {'STATE':<12} {'HEALTH':<10} REASON")
    for row in rows:
        print(f"{row['id']:<24} {row['state']:<12} {row['health']:<10} {row['reason']}")


def cmd_plugins(args) -> int:
    runtime = _runtime(args)
    runtime.load_plugins()
    runtime.start()
    try:
        print(json.dumps(runtime.health(), ensure_ascii=False, indent=2))
    finally:
        runtime.shutdown()
    return 0


def cmd_dsh(args) -> int:
    runtime = _runtime(args)
    runtime.core.memory.open_session(args.account or "desktop")
    try:
        if args.dsh_command == "status":
            result = runtime.dsh.status()
        elif args.dsh_command == "import":
            result = runtime.dsh.import_session(args.session, args.account)
        elif args.dsh_command == "export":
            result = runtime.dsh.export_session(args.session, args.account, args.limit)
        elif args.dsh_command == "sync":
            result = runtime.dsh.sync_session(args.session, args.account, args.limit)
        else:
            raise SystemExit(f"未知 dsh 子命令：{args.dsh_command}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        runtime.core.shutdown()


def cmd_chat(args) -> int:
    runtime = _runtime(args)
    runtime.load_plugins()
    runtime.start()
    record = runtime.loader.get("qqbot")
    if record is None or record.plugin is None:
        print("qqbot 插件未加载", file=sys.stderr)
        runtime.shutdown()
        return 2
    plugin = record.plugin
    print(f"Jinyv-memory 本地对话（lane={args.account}；Ctrl-C 退出）")
    try:
        while True:
            try:
                text = input("你> ").strip()
            except EOFError:
                break
            if not text:
                continue
            if text in {"/quit", "/exit"}:
                break
            reply = plugin.handle_message(text, account=args.account, speaker="主人")
            print(f"瑾瑜> {reply}")
    except KeyboardInterrupt:
        pass
    finally:
        runtime.shutdown()
    return 0


def cmd_run(args) -> int:
    runtime = _runtime(args)
    runtime.load_plugins()
    runtime.start()
    print(json.dumps(runtime.health(), ensure_ascii=False, indent=2))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        runtime.shutdown()
    return 0


def cmd_selfcheck(args) -> int:
    """离线闭环：外部插件发现 → 跑 10 轮 → 租约/幂等/DSH 双向桥接。"""
    checks = 0

    def check(condition: bool, message: str) -> None:
        nonlocal checks
        if not condition:
            raise AssertionError(f"自检失败：{message}")
        checks += 1
        print(f"  [ok] {message}")

    data_dir = Path(tempfile.mkdtemp(prefix="jinyv-selfcheck-"))
    runtime = _runtime(argparse.Namespace(root=args.root, data_dir=data_dir, mock=True))
    runtime.load_plugins()
    runtime.start()
    print("== 插件发现 ==")
    _print_plugins(runtime)
    ids = {row["id"] for row in runtime.loader.health()}
    check({"qqbot", "desktop-pet", "video-call"} <= ids, "三个 Python 插件从外部目录被发现")
    check(any("Jinyv-plugins" in str(path) for path in runtime._plugin_dirs),
          "插件搜索路径包含外部目录")
    qqbot_record = runtime.loader.get("qqbot")
    check(qqbot_record is not None and qqbot_record.plugin is not None, "qqbot 插件已加载")
    check(runtime.loader.tools.get("qqbot.recall") is not None, "qqbot 注册了召回工具")
    check(runtime.loader.schedules.get("qqbot.heartbeat") is not None, "qqbot 注册了定时任务")

    script = [
        "客人来了，说这把旧伞是我去年落下的",
        "外面下雨了，茶凉了一半",
        "他问起那场雨夜的事，我没有回答",
        "他把伞修好了才送回来",
        "我收下伞，欠他一个人情",
        "他提起城里的剑客，我按住了柜台下的短刀",
        "他说茶要趁热喝，像个旧友",
        "我第一次把铺子的秘密告诉了他",
        "他走的时候雨停了",
        "我望着那把旧伞，把它挂回了墙上",
    ]
    plugin = qqbot_record.plugin
    hook_before = 0

    def count_before(turn):
        nonlocal hook_before
        hook_before += 1

    runtime.loader.hooks.register_before("selfcheck", count_before)
    replies = [plugin.handle_message(text, account="owner_qq", speaker="客人") for text in script]
    check(all(replies), "10 轮均产生回复（QQ 适配层闭环）")
    check(hook_before == 10, "before 钩子每轮触发")
    runtime.core.memory.flush()

    status = runtime.core.memory.status("owner_qq")
    check(status["turns"] == 20, f"turns=20（实际 {status['turns']}）")
    check(status["cursor"] == 8, f"归档游标=8（实际 {status['cursor']}）")
    cards = runtime.core.memory.recent_cards("owner_qq", limit=10)
    ranges = sorted((card["turn_start"], card["turn_end"]) for card in cards)
    check(ranges == [(1, 2), (3, 4), (5, 6), (7, 8)], f"归档区间确定（实际 {ranges}）")

    recall = runtime.core.memory.recall("owner_qq", "那把旧伞", limit=3)
    check(bool(recall.cards or recall.facts), "召回返回相关记忆")
    tool_result = runtime.loader.tools.call("qqbot.recall",
                                            {"query": "旧伞", "limit": 3})
    check(tool_result.ok and (tool_result.value["cards"] or tool_result.value["facts"]),
          "工具调用可读记忆")

    lease_a = runtime.core.leases.acquire("selfcheck:reply", "owner-a", ttl=30)
    lease_b = runtime.core.leases.acquire("selfcheck:reply", "owner-b", ttl=30)
    check(lease_a is not None and lease_b is None, "回复租约互斥")
    runtime.core.leases.release(lease_a)
    lease_c = runtime.core.leases.acquire("selfcheck:reply", "owner-c", ttl=30)
    check(lease_c is not None, "租约释放后可重新获取")
    runtime.core.leases.release(lease_c)

    claim_a = runtime.core.leases.claim_job("selfcheck.job", "1", "job-a", ttl=30)
    claim_b = runtime.core.leases.claim_job("selfcheck.job", "1", "job-b", ttl=30)
    check(claim_a and not claim_b, "定时任务同窗口只执行一次")
    runtime.core.leases.finish_job("selfcheck.job", "1", True)

    dsh_home = data_dir / "dsh-home"
    session_dir = dsh_home / "jinyu-memory" / "sessions" / "sess-a"
    session_dir.mkdir(parents=True)
    (session_dir / "turns.jsonl").write_text(
        json.dumps({"idx": 1, "turn": 1, "user": "今天好累", "assistant": "早点睡"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (session_dir / "cards.json").write_text(json.dumps([{
        "id": "C-demo",
        "summary": "主人今天很累，瑾瑜提醒他早点睡",
        "hooks": ["主人", "早点睡"],
        "relHooks": [],
        "importance": 0.7,
        "boundary": "both",
        "turnStart": 1,
        "turnEnd": 1,
        "createdAt": "2026-11-01T12:00:00.000Z",
    }], ensure_ascii=False), encoding="utf-8")
    (session_dir / "facts.json").write_text(json.dumps([{
        "id": "F-demo", "text": "主人最近很累", "tags": ["主人", "作息"],
        "kind": "事件", "importance": 0.8, "turn": 1,
        "createdAt": "2026-11-01T12:00:00.000Z",
    }], ensure_ascii=False), encoding="utf-8")
    from .integrations.dsh import DshBridge

    dsh_bridge = DshBridge.from_file(
        runtime.paths.config_dir / "integrations" / "dsh.toml",
        memory=runtime.core.memory,
        paths=runtime.paths,
        overrides={
            "dsh_home": str(dsh_home),
            "plugin_dir": "",
            "storyboard_dir": "",
            "account": "desktop",
            "sync_mode": "both",
        },
    )
    check(dsh_bridge.status()["sessions"] == 1, "DSH 桥检测到本地记忆仓")
    first = dsh_bridge.import_session("sess-a")
    check(first["cards_inserted"] == 1 and first["facts_inserted"] == 1,
          "DSH 会话导入核心记忆")
    second = dsh_bridge.import_session("sess-a")
    check(second["skipped"] == 2 and second["conflicts"] == 0,
          "DSH 重复导入按 journal 幂等跳过")

    runtime.core.memory.remember("desktop", "瑾瑜喜欢米饭", speaker="瑾瑜", importance=0.8)
    export_first = dsh_bridge.export_session("sess-a")
    check(export_first["cards_new"] >= 1, "核心卡片导出到 DSH")
    export_second = dsh_bridge.export_session("sess-a")
    check(export_second["cards_new"] == 0 and export_second["facts_new"] == 0,
          "DSH 重复导出幂等")
    check((session_dir / ".jinyv-bridge.json").exists(), "桥接 journal 已落盘")
    check(dsh_bridge.start() is False, "未配置托管命令时 dsh 不替用户启动进程")

    runtime.shutdown()
    print(f"\nSELFCHECK OK：{checks} 项检查通过；临时数据目录 {data_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m jinyv_memory.cli")
    parser.add_argument("--root", default=str(ROOT), help="仓库根目录")
    parser.add_argument("--data-dir", default=None, help="运行数据目录")
    parser.add_argument("--mock", action="store_true", help="使用离线假模型")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("selfcheck")
    sub.add_parser("plugins")
    sub.add_parser("run")
    chat = sub.add_parser("chat")
    chat.add_argument("--account", default="owner_qq")
    dsh = sub.add_parser("dsh")
    dsh_sub = dsh.add_subparsers(dest="dsh_command", required=True)
    dsh_sub.add_parser("status")
    for name in ("import", "export", "sync"):
        item = dsh_sub.add_parser(name)
        item.add_argument("--session", required=True, help="DSH 会话目录名或 id")
        item.add_argument("--account", default=None, help="核心 lane")
        if name in {"export", "sync"}:
            item.add_argument("--limit", type=int, default=50)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "selfcheck": cmd_selfcheck,
        "plugins": cmd_plugins,
        "chat": cmd_chat,
        "run": cmd_run,
        "dsh": cmd_dsh,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
