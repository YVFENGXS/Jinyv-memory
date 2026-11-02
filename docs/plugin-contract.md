# 插件契约（签名级）

真源文件：[`src/jinyv_memory/contract.py`](../src/jinyv_memory/contract.py)。本文件是给插件作者的说明书；两者不一致时以代码为准。

## 1. 目录与 manifest

```
plugins/<插件目录>/
├── plugin.toml
├── __init__.py        # 包入口，可用相对导入
└── ...                # 其他模块
```

```toml
[plugin]
id = "qqbot"                       # 必填，全局唯一；决定配置文件名与注册归属
name = "QQ Bot (OneBot v11)"
version = "0.1.0"
kind = "platform-adapter"          # platform-adapter / integration / extension
entry = "__init__.py"              # 必须是包入口，才能相对导入
description = "..."
requires = []                       # 依赖的其他插件 id
optional_modules = ["nonebot"]      # 仅元数据，加载器不强制安装
provides = ["platform.qq"]
default_enabled = true
```

## 2. 插件对象

模块必须暴露以下之一：

- `PLUGIN`：实现了 `setup` 的对象；
- `create_plugin() -> Plugin`；
- 模块级 `setup(ctx)`（可选 `start/stop/health`）。

```python
class Plugin(Protocol):
    def setup(self, ctx: PluginContext) -> None: ...
    def start(self, ctx: PluginContext) -> None: ...
    def stop(self, ctx: PluginContext) -> None: ...
    def health(self, ctx: PluginContext) -> Health: ...
```

- `setup` 必选；`start / stop / health` 可选。
- `setup` 里只做注册与轻量初始化；需要外部依赖的探测放在 `start`，失败用 `ctx.degrade` 而不是抛异常。
- `start` 抛异常 → 插件 `failed`，已注册能力整体回滚。

## 3. PluginContext

```python
@dataclass
class PluginContext:
    plugin_id: str
    manifest: PluginManifest
    config: PluginConfig
    paths: Paths
    memory: MemoryAPI
    accounts: AccountRegistry
    leases: LeaseManager
    events: EventBus
    hooks: HookRegistry
    tools: ToolRegistry
    schedules: ScheduleRegistry
    logger: logging.Logger
    runtime: Any
    def degrade(self, reason: str) -> None: ...
    @property data_dir -> str
```

`ctx.runtime` 目前是 `{"root": str, "version": str, "host": "jinyv-memory"}`，只读。

## 4. 工具

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str                                  # ^[a-z][a-z0-9_.-]{1,63}$
    description: str
    parameters: Mapping[str, Any]               # JSON Schema
    handler: Callable[[Mapping[str, Any], TurnContext | None], Any]
    plugin_id: str = ""
    timeout_seconds: float = 15.0
```

```python
ctx.tools.register(ToolSpec(...), ctx.plugin_id)
```

- 工具名全局唯一；冲突时 `PluginError`，插件加载失败。
- handler 可以返回 awaitable；注册表会在无事件循环的线程里 `asyncio.run`。
- 调用结果：

```python
@dataclass(frozen=True)
class ToolResult:
    ok: bool
    value: Any = None
    error: str = ""
```

## 5. 对话钩子

```python
@dataclass
class TurnContext:
    platform: str
    account: str
    bot_id: str
    session_id: str
    character_id: str
    speaker: str
    text: str
    source: str
    messages: list[dict]
    metadata: dict
```

```python
ctx.hooks.register_before(ctx.plugin_id, handler, priority=0)
ctx.hooks.register_after(ctx.plugin_id, handler, priority=0)
```

- before：`handler(turn) -> TurnContext | None`，可返回新 context；异常只记入 `turn.metadata["hook_errors"]`，不阻断其他钩子。
- after：`handler(turn, reply) -> None`，只观察，不改回复。
- 本轮钩子为同步；异步入口由平台插件自己用 `asyncio.to_thread` 调用。

## 6. 事件总线

```python
ctx.events.subscribe(topic, handler, plugin_id=None, priority=0) -> unsubscribe
ctx.events.emit(topic, payload=None) -> list[EventResult]
```

- topic 支持 fnmatch：`turn.*`、`qqbot.*`。
- 单个订阅者异常不影响其他订阅者；结果为 `EventResult(topic, plugin_id, ok, value, error)`。

## 7. 定时任务

```python
@dataclass(frozen=True)
class ScheduleSpec:
    job_id: str
    interval_seconds: float          # >= 1
    handler: Callable[[], Any]
    plugin_id: str = ""
    run_on_start: bool = False
    lease_ttl: float = 60.0
```

- 调度器按 `int(now // interval)` 生成 `run_key`，用 `job_runs` 跨进程去重。
- 任务失败 → `failed`，最多重试 3 次；进程崩溃/超时后可被接管。
- 业务幂等仍由任务自己保证（例如提醒状态机）。

## 8. 记忆接口

```python
class MemoryAPI:
    def lane(key: str | None = None) -> Lane
    def open_session(lane: Lane | str | None = None) -> Lane
    def close_session(lane: Lane | str | None = None) -> None
    def chat(lane, text, *, speaker="", source="", persona=None,
             store_input=None, store_reply=None,
             reply_lease=False, lease_owner="") -> str
    def remember(lane, text, *, speaker="", importance=0.7, turn=None) -> int
    def write_turn(lane, role: str, content: str, *, source="") -> int
    def turns(lane, *, lo=1, hi=None)
    def import_external(lane, cards, facts=()) -> dict[str, int]
    def recall(lane, query="", limit=6, *, speaker="", scene=None,
               apply_disclosure=True) -> RecallResult
    def recent_cards(lane, limit=6)
    def status(lane=None) -> dict
    def consolidate(lane=None, *, dry_run=False, day=None) -> dict
    def flush(timeout=None) -> None
    def shutdown(timeout=None) -> None
```

约束：
- 插件不得直接访问 `ctx.memory.db` / `middleware`（保留给 core 与迁移期工具）。
- 外部导入必须走 `import_external`，由 core 生成幂等键并映射到现有表。
- 多入口共享记忆时复用同一 `session_id`，但保留各自 `bot_id`。

## 9. 配置与密钥

```python
ctx.config.get("key", default)
ctx.config.require("key")
ctx.config.secret("api_key", env_var="QQBOT_API_KEY")
ctx.data_dir
```

- 插件配置：`config/plugins/<id>.toml`，环境变量覆盖 `JINYV_PLUGIN_<ID>_<KEY>`。
- 密钥优先级：显式环境变量 → `secrets/plugins/<id>/<name>` 文件。
- 禁止把密钥写进 TOML、数据库、日志或插件数据目录。

## 10. 健康与降级

```python
@dataclass(frozen=True)
class Health:
    state: str          # ok / degraded / failed
    reason: str
    details: Mapping[str, Any]
```

- `ctx.degrade("nonebot2 未安装")`：插件保持 `degraded`，但工具/钩子仍可用。
- `start` 抛异常：`failed`，注册回滚。
- 加载器健康汇总包含 `state / health / reason / details / error`，可用 `python -m jinyv_memory.cli plugins` 查看。

## 11. 最小插件示例

```python
# plugins/hello/__init__.py
from jinyv_memory.contract import Health, PluginContext, ToolSpec
from jinyv_memory.contract import PluginManifest

def create_plugin():
    return HelloPlugin()

class HelloPlugin:
    def setup(self, ctx: PluginContext) -> None:
        ctx.tools.register(ToolSpec(
            name="hello.echo",
            description="回显一句话",
            parameters={"type": "object", "properties": {"text": {"type": "string"}},
                        "required": ["text"]},
            handler=lambda args, turn: {"echo": args.get("text", "")},
        ), ctx.plugin_id)

    def health(self, ctx: PluginContext) -> Health:
        return Health.ok()
```
