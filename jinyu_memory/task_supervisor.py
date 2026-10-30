# -*- coding: utf-8 -*-
"""同车道任务监督：新消息取消旧生成，但「已经发出去的话」不回滚。

同一 lane（群/私聊/社交圈）里，主人连发两条时，旧的那条生成往往已经过时：
- 旧任务还没开始发消息 → 新消息到达时取消，省一次前台调用，也避免旧答覆盖新问；
- 旧任务已经发出任何一条消息 → 不可回滚，只让新任务排队等锁，保持先后顺序。

每个生成有一个最小 run 状态机：运行中 / 已取消 / 已完成。
状态由任务本身自动收尾（done 回调），取消时先置状态再 cancel，调用方可以在
生成返回后检查 run.cancelled 做协作取消（比如生成期间被新消息顶掉就不再发送）。
只做这一件事，不接管记忆落库：mw.chat 在后台线程里跑，取消 await 不会抹掉
已经写进 turns 的那一轮（可回滚的只有「还没发送的回复」）。
"""
from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger("rp_memory")

RUNNING, CANCELLED, COMPLETED = "running", "cancelled", "completed"
RECENT_KEEP = 20                    # 只留最近这些 run 供自检，不无限增长


class Run:
    """一次生成的运行态：状态只前进不回退，sent 一旦置真就不再允许取消。"""

    __slots__ = ("lane", "task", "status", "sent", "started", "finished")

    def __init__(self, lane, task):
        self.lane = lane
        self.task = task
        self.status = RUNNING
        self.sent = False
        self.started = time.monotonic()
        self.finished = False

    @property
    def cancelled(self) -> bool:
        return self.status == CANCELLED

    def cancelled_before_send(self) -> bool:
        """协作取消判据：被取消且一条消息都没发出去，才允许整轮作废。"""
        return self.status == CANCELLED and not self.sent

    def snapshot(self) -> dict:
        return {"lane": self.lane, "status": self.status, "sent": self.sent,
                "age": round(time.monotonic() - self.started, 2)}


class LaneSupervisor:
    def __init__(self) -> None:
        self._tasks: dict[str, set[asyncio.Task]] = {}
        self._runs: dict[asyncio.Task, Run] = {}
        self._recent: list[Run] = []
        self.stats = {"started": 0, "cancelled": 0, "completed": 0, "sent": 0}

    def begin(self, lane: str) -> Run | None:
        """登记当前任务为该 lane 的在跑生成，返回它的 run（同一任务重复调用返回同一个）。"""
        task = asyncio.current_task()
        if task is None or not lane:
            return None
        run = self._runs.get(task)
        if run is not None:
            return run
        run = Run(lane, task)
        self._runs[task] = run
        self._tasks.setdefault(lane, set()).add(task)
        self.stats["started"] += 1
        task.add_done_callback(self._finish)
        self._prune(lane)
        return run

    def mark_sent(self, lane: str) -> None:
        """标记当前任务已经发出消息：之后它的取消一律拒绝。"""
        run = self._runs.get(asyncio.current_task())
        if run is not None and not run.sent:
            run.sent = True
            self.stats["sent"] += 1

    def _finish(self, task: asyncio.Task) -> None:
        """任务结束回调：取消/异常退出的都记 cancelled，正常走完记 completed。"""
        run = self._runs.pop(task, None)
        if run is None or run.finished:
            return
        run.finished = True
        if run.status == CANCELLED or task.cancelled():
            run.status = CANCELLED
            self.stats["cancelled"] += 1
        else:
            run.status = COMPLETED
            self.stats["completed"] += 1
        self._recent.append(run)
        if len(self._recent) > RECENT_KEEP:
            del self._recent[:-RECENT_KEEP]
        bucket = self._tasks.get(run.lane)
        if bucket:
            bucket.discard(task)
            if not bucket:
                self._tasks.pop(run.lane, None)

    def _prune(self, lane: str) -> None:
        tasks = self._tasks.get(lane)
        if not tasks:
            return
        for task in list(tasks):
            if task.done():
                tasks.discard(task)

    def current(self, lane: str) -> list[asyncio.Task]:
        self._prune(lane)
        return [t for t in self._tasks.get(lane, ()) if not t.done()]

    def runs(self, lane: str) -> list[Run]:
        return [r for r in self._runs.values() if r.lane == lane]

    def status(self) -> dict:
        """自检快照：活动 run 明细 + 累计计数（供日志/测试用，不影响调度）。"""
        return {"active": [r.snapshot() for r in self._runs.values()],
                "recent": [r.snapshot() for r in self._recent[-8:]],
                "stats": dict(self.stats)}

    async def preempt(self, lane: str) -> int:
        """取消该 lane 里所有「还没发过消息」的旧任务；返回取消数量。"""
        self._prune(lane)
        me = asyncio.current_task()
        victims = []
        for task in list(self._tasks.get(lane, ())):
            run = self._runs.get(task)
            if task is me or task.done() or run is None or run.sent:
                continue
            run.status = CANCELLED           # 先置状态再 cancel，调用方可能正检查它
            victims.append(task)
        if not victims:
            return 0
        for task in victims:
            task.cancel()
        await asyncio.gather(*victims, return_exceptions=True)
        bucket = self._tasks.get(lane)
        if bucket:
            bucket.difference_update(victims)
            if not bucket:
                self._tasks.pop(lane, None)
        logger.info("同车道新消息取消旧生成 lane=%s 数量=%d", lane, len(victims))
        return len(victims)


supervisor = LaneSupervisor()
