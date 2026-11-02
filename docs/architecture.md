# 架构方案

## 1. 目标与边界

**核心只做记忆**：写入、召回、披露、巩固、租约、数据库，以及对外稳定的一套插件契约。  
**外围全部插件化**：QQ、桌宠、视频通话、DSH/视频分镜都通过插件接入；插件不得直接改 core 内部表，只能走 `MemoryAPI`。

兼容性优先：本轮把旧 `jinyu_memory/core` 原样迁入，插件适配层包住旧模块入口；核心行为与线上表现先保持不变，再逐步搬外围。

## 2. 总体结构

```
进程
├── JinyvRuntime
│   ├── Config（core.toml + accounts.toml + plugins/*.toml + 环境变量）
│   ├── CoreRuntime
│   │   ├── DB（var/core/rp_memory.db，旧 schema 原样）
│   │   ├── RPContextMiddleware（归档 / 水位线 / 召回 / 披露）
│   │   ├── LeaseManager（var/core/runtime.db，租约 + job_runs）
│   │   ├── AccountRegistry（lane = 机器人账号 × 平台 × 会话）
│   │   └── MemoryAPI（插件唯一记忆门面）
│   └── PluginLoader
│       ├── EventBus（topic 通配、异常隔离）
│       ├── HookRegistry（turn.before / turn.after）
│       ├── ToolRegistry（全局唯一工具名）
│       ├── ScheduleRegistry + Scheduler（按窗口领租约）
│       └── PluginRecord × N（状态 / 健康 / 降级原因）
├── integrations/dsh.py（特殊接入：检测 / 启停 / 导入 / 导出 / journal）
└── E:\Jinyv-plugins\*（独立 git 仓库，plugin.toml + __init__.py）
```

### 插件仓库布局

主仓库不持有插件实现。每个插件是独立 git 仓库，放在统一的外部目录：

```
E:\Jinyv-plugins\
├── qqbot\          # 适配层 + legacy 只读桥（远程待确认）
├── desktop-pet\    # 桌宠服务端适配层（客户端在原仓库 Jinyu-pet）
└── video-call\     # 通话适配层（服务在原仓库 jinyu-video-call）
```

dsh 不在此目录：它是 JS/cordis 特殊插件，源码留在用户 zip / `$DSH_HOME`，
主仓库只保留 `integrations/dsh.py` 桥接。加载器支持多个 `search_paths`，
按顺序扫描；`plugin id` 重复只记录冲突，不覆盖已发现插件。

### 核心职责
- `contract.py`：唯一的契约真源，插件只依赖它和 `MemoryAPI`。
- `loader.py`：发现、依赖拓扑、生命周期、注册回滚、健康汇总。
- `core/memory.py`：把旧 DB/中间件包成稳定门面；负责 lane、回复租约、外部记忆导入。
- `core/lease.py`：跨进程回复租约与定时任务幂等。
- `core/engine/`：迁移来的 13 个旧模块（见迁移表）。

### 核心不负责
- 平台消息解析、富消息渲染、贴纸、ASR、B 站、直播、桌宠/通话协议、DSH 安装。
- 这些全部落在独立插件仓库（`E:\Jinyv-plugins\*`），失败不影响 core 与其他插件。

## 3. 生命周期

```
discover → 依赖解析 → import → setup(ctx) → start(ctx) → running/degraded
                                                         ↓
                                                   stop(ctx) → stopped
```

- **发现**：按 `search_paths` 扫描多个目录下的 `*/plugin.toml`；`id` 冲突或 TOML 解析失败只记录为「发现失败」，不中断。
- **依赖**：`requires` 做拓扑排序；缺失或成环 → 该插件 `failed`，不阻塞其他插件。
- **加载**：以 Python 包方式导入，插件内部可用相对导入；未暴露 `PLUGIN` / `create_plugin()` / `setup()` → `failed`。
- **启动失败回滚**：按 `plugin_id` 撤销已注册工具、钩子、事件订阅、定时任务。
- **降级**：插件调用 `ctx.degrade(reason)`，仍完成注册，健康状态为 `degraded`；例如 NoneBot 未安装、桌宠客户端目录未配置。
- **停止**：逆序 stop，再停调度器，最后冲刷 core 归档。

## 4. 插件契约（摘要）

完整签名见 [plugin-contract.md](plugin-contract.md)。

```python
class Plugin(Protocol):
    def setup(self, ctx: PluginContext) -> None: ...
    def start(self, ctx: PluginContext) -> None: ...
    def stop(self, ctx: PluginContext) -> None: ...
    def health(self, ctx: PluginContext) -> Health: ...
```

`PluginContext` 提供六类能力：
- `ctx.memory`：`open_session / chat / remember / recall / write_turn / import_external / status / consolidate / flush`
- `ctx.tools.register(ToolSpec)`：工具名全局唯一，`handler(args, turn)`。
- `ctx.hooks.register_before(plugin_id, fn)` / `register_after(plugin_id, fn)`：每轮对话前后。
- `ctx.schedules.register(ScheduleSpec)`：`interval_seconds >= 1`，按窗口领 `job_runs` 租约。
- `ctx.events.subscribe(topic, handler)` / `emit`：支持 `turn.*` 通配，异常隔离。
- `ctx.config` / `ctx.data_dir` / `ctx.config.secret(name)`：插件配置、隔离数据目录、密钥读取。

manifest 字段：`id / name / version / entry / kind / description / requires / optional_modules / provides / default_enabled`。

## 5. 配置、密钥、数据隔离

| 类型 | 位置 | 规则 |
|---|---|---|
| 核心配置 | `config/core.toml` | 参数可被 `JINYV_CORE_<KEY>` 覆盖 |
| 账号车道 | `config/accounts.toml` | `[accounts.<key>]`；`session` 不填时按平台/私聊/群聊推导 |
| 插件搜索路径 | `config/core.toml` `[plugins].search_paths` | 相对主仓库根；`JINYV_PLUGIN_PATHS` 可覆盖（os.pathsep 分隔） |
| 插件配置 | `config/plugins/<id>.toml` | 可被 `JINYV_PLUGIN_<ID>_<KEY>` 覆盖 |
| 插件数据 | `var/plugins/<id>/` | 每插件独立，禁止跨插件写 |
| 核心数据 | `var/core/` | `rp_memory.db` + `runtime.db` |
| 密钥 | 环境变量或 `secrets/plugins/<id>/<name>` | 不写 TOML、不入库、不进 git |

`var/`、`secrets/`、`external/`、`.tmp/` 均在 `.gitignore` 中。

## 6. 多账号、租约、幂等

**车道（lane）** = 机器人账号 × 平台 × 会话，包含 `platform / account / bot_id / character_id / session_id / continuity_id / role / owner`。

- 同一人格的 QQ 私聊、桌宠、视频通话可以共享 `session_id=qq-u10001`，但 `bot_id` 保留写入来源。
- `role=primary` 可主动回复；`observer` 只记不回，避免多入口抢话。
- `MemoryAPI.chat` 对同一 `(session_id, character_id)` 加进程内 `RLock`，保持旧行为「同会话严格串行」。
- `reply_lease=True` 时抢 `reply:<cid>:<sid>` 跨进程租约；抢不到抛 `LeaseBusy`，由调用方决定沉默或观察。
- 归档幂等继续由 `event_cards.idempotency_key UNIQUE` 兜底；失败区间写 `archive_failures`，游标不推进。
- 定时任务用 `job_runs(job_id, run_key)` 去重；失败最多重试 3 次，超时可接管；租约过期自动释放。
- 已知限制：租约时间基于 `time.time()`，系统时钟回拨会拉长/缩短持有时间；SQLite 仍是单机方案，跨机需中心服务。

## 7. 外围插件接入

### qqbot
- 传输层可选：`nonebot_adapter.py` 只在装了 NoneBot2 + OneBot v11 时导入；否则 `ctx.degrade`。
- 本轮闭环：`handle_message` → before 钩子召回 → `MemoryAPI.chat`（落 turns / 拼上下文 / 生成 / 归档）→ after 钩子 → `qqbot.turn.completed` 事件。
- 工具：`qqbot.remember` / `qqbot.recall` / `qqbot.status`。
- 未迁：富消息、贴纸、ASR、链接/视频、B 站、提醒、主人绑定、主动话题（P2）。

### desktop-pet（独立仓库 `YVFENGXS/Jinyu-pet`）
- 客户端是 Electron 薄壳，**不复制源码**；主仓库只提供服务端适配层与协议真源。
- 本轮接口：`handle_hello` / `handle_text` / `desktop.status` / `desktop.say`（推送未实现，明确返回未连接）。
- 健康：`client_dir` 未配置或依赖缺失 → `degraded`。
- 协议：`docs/protocol/desktop-v1.md`（旧仓库迁移，客户端只钉版本）。

### video-call（独立仓库 `YVFENGXS/jinyu-video-call`）
- 服务自带一份 `jinyu_memory/core` 作为本地模式依赖；主仓库**不复制**其舞台/ASR/TTS/视觉代码。
- 推荐部署：通话服务只走宿主 API（`RP_HOST_BASE` + `RP_HOST_TOKEN`），单写者留在主进程；否则两个进程直连同一 SQLite 会互抢游标。
- 本轮接口：`memory_context` / `record_turn` / `video_call.status` / `video_call.say`（跨窗口发话未实现）。
- 健康：`repo_dir` 未配置或依赖缺失 → `degraded`。

### dsh-video-storyboard（特殊接入，不是 Python 插件）
- **勘察结论**：用户提供的 zip 是 `jinyu-memory-dsh`（DSH 宿主的本地记忆插件，JS/cordis），不是第三方分镜工具 `dsh-video-storyboard` 的源码。
- **接入类型**：`src/jinyv_memory/integrations/dsh.py`，不进入插件加载器，也不复制源码到主仓库或 `Jinyv-plugins`。
- **检测**：`package.json name=jinyu-memory-dsh`、`bin/storyboard.mjs`、`$DSH_HOME/jinyu-memory/sessions` 会话数。
- **启停**：仅当 `auto_start=true` 且配置 `start_command` 时由主进程 `Popen` 托管；默认不启动第三方进程。
- **导入**：`cards.json / facts.json` → `event_cards / durable_facts`；核心 UNIQUE 键 + journal 双端哈希，重复导入不产生副本。
- **导出**：核心卡片/事实 → DSH 同结构文件；`.jinyv-bridge.json` 记录 `core_sig / dsh_sig`，重复导出幂等。
- **冲突**：同一逻辑条目双端同时变化 → 默认 `core-wins`，冲突写入 journal；DSH 单端变化保留用户改动并记录。事实按文本 append-only，不做原地覆盖。
- **安装**：`scripts/install_dsh_plugin.py --source <zip|dir>` 校验后安装到 `$DSH_HOME/plugins/jinyu-memory-dsh`；第三方 CLI 由 `scripts/install_storyboard.sh` 固定 ref 安装。
- 已验证：dsh zip 自带 Node 测试 15/15；桥接导入/导出/幂等/冲突测试通过。

## 8. 分阶段实施

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0（本轮） | core 迁移 + 契约 + 多路径加载器 + 外部 qqbot 闭环 + 外部适配器 + dsh 桥接 + 自检/测试 | `selfcheck` 22 项、`unittest` 14 项通过 |
| P0.5（本轮） | 主仓库 `plugins/` 副本已删除；qqbot 仓库已按顺序建立旧能力适配入口；dsh 支持 CLI 安装 | `git status` 干净；适配入口状态可见 |
| P1 | 把 104 项旧回归迁到新结构；`tools/serve.py` 宿主 HTTP API 插件化；环境变量完全对齐 | 旧回归全绿；QQ/桌宠/通话不回归 |
| P2 | qqbot 完整迁移：richmsg / linkmeta / reminder / owner_gate / asr / bili_live / 主动话题 | 逐模块行为等价对照 |
| P3 | desktop-v1 服务端插件（包装 `desktop_server.py`），Jinyu-pet 联调 | 协议 fixtures + 断线重连 |
| P4 | video-call 宿主桥 + 独立服务联调；禁止双写 SQLite | 通话一轮落库、召回一致 |
| P5 | DSH 改造版安装/导入验收；第三方分镜 CLI 检测与调用 | Node 测试 + 导入幂等 |
| P6 | 清理旧入口、文档、推送分支 | 评审通过 |

## 9. 风险点

1. **引擎副本漂移**：旧仓库仍在改动（9 个未提交文件），复制进新仓库后可能分叉。需要定同步策略（vendored + 同步脚本 / subtree）。
2. **远程非空**：不能直接覆盖 main；新历史推新分支最安全，force-push 会丢旧历史。
3. **QQ 全量迁移量大**：196 KB 单文件耦合 NoneBot/OneBot、富消息、贴纸、B 站、提醒；必须逐模块对照回归。
4. **多进程双写**：桌宠/通话若直连同一 SQLite，会互抢归档游标；必须坚持宿主 API 或单写者。
5. **许可证**：Jinyu-pet 为 AGPL-3.0（含第三方形象资产），主仓库 MIT；两者只能独立仓库/独立进程 + 协议交互，不能合并源码。
6. **DSH 约定冲突**：改造版是用户改过的第三方衍生代码，直接入库会破坏「不提交第三方源码」约定；当前用安装脚本 + 核心桥接规避。
7. **旧源码迁移被策略阻断**：执行环境禁止把旧 QQ 实现整块复制进新仓库；当前 qqbot 仓库只有 `legacy_bridge.py` 只读引用，物理迁移需用户明确授权。
8. **旧能力物理改写受限**：安全策略禁止整块复制旧 QQ 实现；当前为每项能力建立只读适配入口，真正改写需逐模块人工进行或改用 submodule。
9. **租约时钟**：时钟回拨、进程崩溃、SQLite busy 都是现实故障；已做 TTL/重试/接管，但跨机需中心化方案。
10. **Windows 兼容**：路径大小写、文件占用、Node ≥20、无 bash 环境；安装脚本提供 Python 与 bash 两种入口。

## 10. 远程仓库策略

远程 `main` 仍是旧实现。新架构已推送到独立分支 `refactor/plugin-core`（`45f739f`），
**没有 force-push `main`**。是否合并到 `main` 走 PR 评审，不做强制推送。
