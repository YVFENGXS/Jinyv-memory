#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安装 DSH 改造版（jinyu-memory-dsh）到 DSH_HOME/plugins，不把源码提交进主仓库。

用法：
  python scripts/install_dsh_plugin.py --source <zip|目录>
  python scripts/install_dsh_plugin.py --source <zip> --target <目录> --force
  python scripts/install_dsh_plugin.py --source <zip> --dry-run

默认目标：$DSH_HOME/plugins/jinyu-memory-dsh（DSH_HOME 缺省 ~/.dsh）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

PLUGIN_NAME = "jinyu-memory-dsh"


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
    if not (directory / "lib" / "index.js").exists():
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安装 DSH 改造版记忆插件")
    parser.add_argument("--source", required=True, help="zip 或已解压目录")
    parser.add_argument("--target", default=str(_default_target()), help="安装目录")
    parser.add_argument("--force", action="store_true", help="覆盖已有安装")
    parser.add_argument("--dry-run", action="store_true", help="只检查，不写文件")
    args = parser.parse_args(argv)

    source = Path(args.source).expanduser().resolve()
    target = Path(args.target).expanduser().resolve()
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
    finally:
        if temp is not None:
            temp.cleanup()

    print(f"已安装：{target}")
    print("下一步：")
    print("  1) 重启 DSH，确认插件列表出现 jinyu-memory")
    print("  2) 在 config/integrations/dsh.toml 设置 plugin_dir =", target)
    print("  3) 保持源码在安装目录，不提交进 Jinyv-memory 仓库")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
