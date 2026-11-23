# Jinyv Memory

瑾瑜的**记忆中间层**

- 热窗口 / 事件卡 / 长期事实 / 归档游标
- 钩子召回、规则召回、冲突标注、承诺状态机
- 人设护栏、输出中间层（删动作 / 限长度 / 去 AI 味）
- 梯度记忆、压缩、保留策略、Dream 整理、会话轮换
- 框架无关：不依赖 NoneBot / OneBot / FastAPI

平台接入在独立仓库 **[Jinyv-plugins](https://github.com/YVFENGXS/Jinyv-plugins)**：

| 接入 | 仓库 |
|---|---|
| QQ（OneBot v11 / NapCat） | [Jinyv-plugins/qqbot](https://github.com/YVFENGXS/Jinyv-plugins) |
| 桌宠 | [Jinyu-pet](https://github.com/YVFENGXS/Jinyu-pet) + 插件仓适配层 |
| 视频通话 | [jinyu-video-call](https://github.com/YVFENGXS/jinyu-video-call) + 插件仓适配层 |
| 房间 / 舞台渲染 | [Jinyv-room](https://github.com/YVFENGXS/Jinyv-room) |

## 目录

```
jinyv_memory/
├── __init__.py          # 对外只暴露 JinyuHost
└── core/                # 记忆实现（零第三方依赖）
    ├── rp_skeleton.py    # 热窗口 / 事件卡 / 归档 / 检索 / 工具循环
    ├── host.py           # 框架中立宿主 JinyuHost
    ├── style_middleware.py  # 输出中间层（确定性，不靠 prompt）
    ├── persona.py        # 人设宪法层（由用户维护）
    ├── promise.py / retriever.py / summarizer.py / conflict.py
    ├── compression.py / gradient.py / retention.py / sealing.py
    ├── context_budget.py / scoring.py / control.py / disclosure.py
    ├── embedding.py / merge_ann.py / dream.py / intimacy.py
    ├── questions.py / gate.py / persona_guard.py / llm.py
    └── status.py / bubbles.py
tools/                   # 宿主服务、健康检查、评测、Dream、backfill
tests/                   # 核心回归 + 输出中间层回归
examples/                # 离线 demo / 命令行聊天
docs/                    # 设计、评测与实验记录
```

## 快速开始（离线，不需要密钥）

```bash
python -m pip install -r requirements.txt   # core 零依赖，等价于什么都不装
python examples/demo.py
python tests/test_regression.py
python tests/test_style_middleware.py
```

## 同进程宿主

```python
from jinyv_memory import JinyuHost

host = JinyuHost(db_path="data/rp_memory.db", sid="main")
print(host.chat("今天看到一条蓝色大肥鱼"))   # 写入一轮并生成回复
host.remember("主人喜欢蓝色", speaker="主人")  # 直接写长期词条
print(host.recall("蓝色"))                   # {"facts": [...], "cards": [...]}
host.close()                                # 冲刷尾部归档
```

`host.close()` 必须调用，否则退出时尾部归档可能丢。

## 跨进程宿主（HTTP）

```bash
export RP_HOST_TOKEN=<随机串>
export RP_FRONT_KEY=sk-... RP_BACK_KEY=sk-...
python tools/serve.py --port 8710 --db data/rp_memory.db
```

| 接口 | 作用 |
|---|---|
| `GET /health` | 存活检查（不校验令牌） |
| `POST /chat` `{"text","session"}` | 写入一轮对话，返回 `{"reply","session"}` |
| `POST /remember` `{"text","speaker","session"}` | 直接写长期词条 |
| `GET /recall?q=&session=&limit=` | 只读召回 |

除 `/health` 外都要 `Authorization: Bearer <RP_HOST_TOKEN>`。
**同一个 `RP_DB` 只能有一个写入进程**，多进程会抢归档游标。

## 环境变量

核心变量见 [.env.example](.env.example)。最常用的：

| 变量 | 说明 |
|---|---|
| `RP_DB` | SQLite 记忆库路径，默认 `data/rp_memory.db` |
| `RP_FRONT_KEY` / `RP_BACK_KEY` | 前台 / 后台模型密钥 |
| `RP_MAX_TOKENS` | 前台模型上下文窗口（不是输出上限） |
| `RP_HOST_TOKEN` | HTTP 宿主服务令牌 |
| `RP_STYLE_MIDDLE` | 输出中间层开关，默认开 |
| `RP_SESSION_EPOCH_HOURS` | 闲置多少小时轮换会话，默认 48 |

## 硬约束

- `archive_cursor` 只在事件卡落库成功后推进。
- 单张卡区间长度 ≤ `archive_batch`；退出冲刷才允许吃尾巴。
- 账本是权威源，后台模型只能提议，不能直接改状态。
- `core/persona.py` 是人设宪法层，由用户维护，不要自行改写。
- 头尾双锚（人设 + 当前状态账本）永不截断。

## 测试

```bash
python tests/test_regression.py        # 记忆语义全量回归
python tests/test_style_middleware.py  # 输出中间层
python tools/eval_memory.py --min-recall 0.8 --min-holdout 0.7
```

Python 3.10–3.13。3.14 起标准库新增 `compression` 包，会与本仓库
`core/compression.py` 重名；在 3.14 上请用虚拟环境或把 core 目录置前。

## License

MIT
