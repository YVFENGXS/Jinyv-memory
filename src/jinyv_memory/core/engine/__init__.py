# -*- coding: utf-8 -*-
"""从现有 jinyu-memory 迁移的记忆引擎（行为等价，仅改为包内相对导入）。

模块分工：
- rp_skeleton    SQLite / 归档中间件 / 水位线 / 多账号车道
- retriever      事件卡召回与打分
- summarizer     后台归纳与校验
- disclosure     四级披露
- dream          离线巩固（章节卡 / 临时事实清理）
- promise        承诺生命周期
- conflict       冲突标注
- embedding      可选向量召回
- gate           证据闸门
- persona        人设单源
- persona_guard  人设硬规则校验
- llm            OpenAI 兼容客户端
- host           框架中立宿主（兼容旧调用方式）
"""
from . import (  # noqa: F401
    conflict,
    disclosure,
    dream,
    embedding,
    gate,
    llm,
    persona,
    persona_guard,
    promise,
    retriever,
    rp_skeleton,
    summarizer,
)
