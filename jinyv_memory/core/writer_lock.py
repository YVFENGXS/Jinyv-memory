# -*- coding: utf-8 -*-
"""单一写入者闸门：同一 RP_DB 只允许一个进程持有写锁。

进程内可开多个 JinyuHost（多会话），共用同一把锁；跨进程时第二个进程会在
启动阶段被拒绝。读服务（如记忆星图、只读导出）不受影响。

关闭方式：RP_WRITER_LOCK=0。
"""
from __future__ import annotations

import atexit
import os
from typing import Dict

try:
    import fcntl
except ImportError:  # Windows 没有 fcntl：退化为不加锁
    fcntl = None

_HELD: Dict[str, int] = {}
_HANDLES: Dict[str, object] = {}


def lock_path(db_path: str) -> str:
    return os.path.realpath(str(db_path)) + ".writer.lock"


def acquire(db_path: str, enabled: bool = True) -> bool:
    """非阻塞抢锁；同进程重复调用只加引用计数。抢不到抛 RuntimeError。"""
    if not enabled or fcntl is None:
        return False
    key = lock_path(db_path)
    if key in _HELD:
        _HELD[key] += 1
        return True
    handle = open(key, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError(
            "另一个进程正在写这个记忆库（单一写入者）："
            f"{db_path}。确认没有第二个 qqbot/host-api 后重试；"
            "确需并行请设 RP_WRITER_LOCK=0。")
    _HANDLES[key] = handle
    _HELD[key] = 1
    atexit.register(release, db_path)
    return True


def release(db_path: str) -> None:
    key = lock_path(db_path)
    count = _HELD.get(key, 0)
    if count > 1:
        _HELD[key] = count - 1
        return
    handle = _HANDLES.pop(key, None)
    _HELD.pop(key, None)
    if handle is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
