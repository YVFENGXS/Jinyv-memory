# -*- coding: utf-8 -*-
"""可选 NoneBot2 入口。只有装了 nonebot2 + onebot v11 适配器才会加载。

消息路由规则与旧插件一致：私聊全收，群聊只在被 @ 时回复。
具体的记忆、披露、归档逻辑全部在 core；这里只做消息 ↔ lane 的映射。
"""
from __future__ import annotations

import asyncio

from jinyv_memory.contract import PluginContext


def install(ctx: PluginContext, plugin) -> None:
    from nonebot import on_message
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent, PrivateMessageEvent

    matcher = on_message(priority=10, block=False)

    @matcher.handle()
    async def _handle(event: MessageEvent) -> None:
        text = event.get_plaintext().strip()
        if not text:
            return
        if isinstance(event, GroupMessageEvent):
            if not event.is_tome():
                return
            session_id = f"qq-g{event.group_id}"
        elif isinstance(event, PrivateMessageEvent):
            session_id = f"qq-u{event.get_user_id()}"
        else:
            return
        sender = event.sender
        speaker = str(getattr(sender, "card", "") or getattr(sender, "nickname", "")
                      or event.get_user_id())
        lane = ctx.accounts.match_session("qq", session_id)
        reply = await asyncio.to_thread(
            plugin.handle_message,
            text,
            account=lane.key if lane else None,
            session_id=session_id,
            speaker=speaker,
            source="qq",
        )
        if reply:
            await matcher.send(reply)

    ctx.logger.info("QQ NoneBot matcher 已注册")
