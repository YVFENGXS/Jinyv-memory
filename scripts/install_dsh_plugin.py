#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安装 DSH 改造版（jinyu-memory-dsh）到 DSH_HOME/plugins，不把源码提交进主仓库。

安装时会以可回滚方式注入 dsh-wins 适配：
- 备份 lib/index.js -> lib/index.js.jinyv-orig
- 复制 scripts/dsh_wins_adapter.js -> lib/jinyv-dsh-wins.js
- 在 index.js 注入 import 与 installDshWins(ctx, createStore)
- 写 .jinyv-dsh-wins.json 标记；重复执行不会重复注入
- --unpatch 恢复备份并删除适配文件

用法：
  python scripts/install_dsh_plugin.py --source <zip|目录>
  python scripts/install_dsh_plugin.py --source <zip> --target <目录> --force
  python scripts/install_dsh_plugin.py --patch-only --target <目录>
  python scripts/install_dsh_plugin.py --unpatch --target <目录>
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

PLUGIN_NAME = "jinyu-memory-dsh"
PATCH_VERSION = "1.0"
PATCH_MARKER = ".jinyv-dsh-wins.json"
ADAPTER_SOURCE = Path(__file__).with_name("dsh_wins_adapter.js")
ADAPTER_REL = Path("lib") / "jinyv-dsh-wins.js"
INDEX_REL = Path("lib") / "index.js"
BACKUP_REL = Path("lib") / "index.js.jinyv-orig"
IMPORT_LINE = "import { installDshWins } from './jinyv-dsh-wins.js'"
CALL_LINE = "  installDshWins(ctx, createStore)"


def _default_target() -> Path:
    home = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh")
    return home / "plugins" / PLUGIN_NAME


def _validate(directory: Path) -> tuple[bool, str]:
    package = directory / "package.json"
    if not package.exists():
        return False, f"缺少 package.json：{directory}"
    try:
        data = json.loads(package.read_text(encoding="utf-8"))
    except ValueError as exc:
        return False, f"package.json 解析失败：{exc}"
    if str(data.get("name") or "") != PLUGIN_NAME:
        return False, f"package.json name={data.get('name')!r}，期望 {PLUGIN_NAME!r}"
    if not (directory / INDEX_REL).exists():
        return False, "缺少 lib/index.js 入口"
    if not (directory / "cordis.patch.yml").exists():
        return False, "缺少 cordis.patch.yml（DSH 插件补丁）"
    return True, ""


def _unzip(source: Path) -> tuple[Path, tempfile.TemporaryDirectory]:
    temp = tempfile.TemporaryDirectory(prefix="jinyv-dsh-install-")
    with zipfile.ZipFile(source) as archive:
        archive.extractall(temp.name)
    root = Path(temp.name)
    entries = [item for item in root.iterdir() if item.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0], temp
    return root, temp


def _copy(source: Path, target: Path, force: bool, dry_run: bool) -> None:
    if target.exists():
        if not force:
            raise SystemExit(f"目标已存在：{target}（确认覆盖请加 --force）")
        if not dry_run:
            shutil.rmtree(target)
    if dry_run:
        print(f"[dry-run] 将复制 {source} -> {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)


def _write_text_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _apply_policy_patch(target: Path, dry_run: bool) -> str:
    """可回滚地注入 dsh-wins：备份 index.js、复制 adapter、加两行注入。"""
    index = target / INDEX_REL
    adapter = target / ADAPTER_REL
    marker = target / PATCH_MARKER
    backup = target / BACKUP_REL
    if not index.exists():
        raise SystemExit(f"缺少插件入口：{index}")
    if not ADAPTER_SOURCE.exists():
        raise SystemExit(f"缺少适配文件：{ADAPTER_SOURCE}")

    text = index.read_text(encoding="utf-8")
    if IMPORT_LINE in text and CALL_LINE in text:
        if not dry_run:
            adapter.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ADAPTER_SOURCE, adapter)
        print("[dsh-wins] 补丁已存在，跳过重复注入")
        return "already"

    anchor_import = "import { createStore } from './store.js'"
    anchor_apply = "export function apply(ctx) {"
    if anchor_import not in text or anchor_apply not in text:
        raise SystemExit("插件 index.js 结构与预期不符，拒绝打补丁")
    if dry_run:
        print(f"[dry-run] 将备份 {index} 并注入 dsh-wins 适配")
        return "dry-run"

    if not backup.exists():
        shutil.copy2(index, backup)
    patched = text.replace(
        anchor_import, f"{anchor_import}\n{IMPORT_LINE}", 1,
    ).replace(
        anchor_apply, f"{anchor_apply}\n{CALL_LINE}", 1,
    )
    adapter.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ADAPTER_SOURCE, adapter)
    _write_text_atomic(index, patched)
    marker.write_text(
        json.dumps({
            "version": PATCH_VERSION,
            "policy": "dsh-wins",
            "patched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "adapter": str(ADAPTER_REL),
            "backup": str(BACKUP_REL),
            "rollback": "python scripts/install_dsh_plugin.py --unpatch --target <此目录>",
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("[dsh-wins] 已注入适配并备份 index.js")
    return "patched"


def _revert_policy_patch(target: Path) -> None:
    index = target / INDEX_REL
    backup = target / BACKUP_REL
    adapter = target / ADAPTER_REL
    marker = target / PATCH_MARKER
    if not backup.exists():
        raise SystemExit(f"找不到补丁备份：{backup}")
    shutil.copy2(backup, index)
    for path in (adapter, marker, backup):
        if path.exists():
            path.unlink()
    print("[dsh-wins] 已回滚补丁并恢复原 index.js")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安装 DSH 改造版记忆插件")
    parser.add_argument("--source", help="zip 或已解压目录")
    parser.add_argument("--target", default=str(_default_target()), help="安装目录")
    parser.add_argument("--force", action="store_true", help="覆盖已有安装")
    parser.add_argument("--dry-run", action="store_true", help="只检查，不写文件")
    parser.add_argument("--unpatch", action="store_true", help="回滚 dsh-wins 补丁")
    parser.add_argument("--patch-only", action="store_true", help="只对已安装目录打补丁")
    args = parser.parse_args(argv)

    target = Path(args.target).expanduser().resolve()
    if args.unpatch:
        _revert_policy_patch(target)
        return 0
    if args.patch_only:
        _apply_policy_patch(target, args.dry_run)
        return 0
    if not args.source:
        raise SystemExit("安装需要 --source；只打补丁请用 --patch-only")

    source = Path(args.source).expanduser().resolve()
    if not source.exists():
        raise SystemExit(f"来源不存在：{source}")

    temp = None
    if source.is_file() and source.suffix.lower() == ".zip":
        staged, temp = _unzip(source)
    elif source.is_dir():
        staged = source
    else:
        raise SystemExit("--source 必须是 .zip 或目录")

    try:
        ok, reason = _validate(staged)
        if not ok:
            raise SystemExit(f"来源不是有效的 {PLUGIN_NAME}：{reason}")
        _copy(staged, target, args.force, args.dry_run)
        if not args.dry_run:
            ok, reason = _validate(target)
            if not ok:
                raise SystemExit(f"安装后校验失败：{reason}")
            _apply_policy_patch(target, dry_run=False)
    finally:
        if temp is not None:
            temp.cleanup()

    print(f"已安装：{target}")
    print("下一步：")
    print("  1) 重启 DSH，确认插件列表出现 jinyu-memory")
    print("  2) 在 config/integrations/dsh.toml 设置 plugin_dir =", target)
    print("  3) 回滚 dsh-wins 补丁：")
    print("     python scripts/install_dsh_plugin.py --unpatch --target", target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
