# 迁移映射表与阶段计划

## 1. 迁移原则

1. `E:\rp_memory_project` **只读**：本轮没有修改、没有写入任何文件。
2. 核心模块「逐个搬、原样搬」：只改包内相对导入；不重写记忆逻辑。
3. 外围模块先做适配层，再逐模块替换；每搬一个都用旧行为对照。
4. 第三方/改造版源码不直接入库：保留独立安装脚本或外部仓库。
5. 不复制第三方完整表结构与命名，只吸收行为级机制。

## 2. 模块映射

### 2.1 记忆核心（已迁移，行为等价）

| 旧位置 | 新位置 | 方式 | 状态 |
|---|---|---|---|
| `jinyu_memory/core/rp_skeleton.py` | `src/jinyv_memory/core/engine/rp_skeleton.py` | 原样复制，内部导入改相对 | ✅ 已迁移 |
| `core/retriever.py` | `core/engine/retriever.py` | 原样复制 | ✅ |
| `core/summarizer.py` | `core/engine/summarizer.py` | 原样复制 | ✅ |
| `core/disclosure.py` | `core/engine/disclosure.py` | 原样复制 | ✅ |
| `core/dream.py` | `core/engine/dream.py` | 原样复制 | ✅ |
| `core/promise.py` | `core/engine/promise.py` | 原样复制 | ✅ |
| `core/conflict.py` | `core/engine/conflict.py` | 原样复制 | ✅ |
| `core/embedding.py` | `core/engine/embedding.py` | 原样复制 | ✅ |
| `core/gate.py` | `core/engine/gate.py` | 原样复制 | ✅ |
| `core/persona.py` | `core/engine/persona.py` | 原样复制 | ✅ |
| `core/persona_guard.py` | `core/engine/persona_guard.py` | 原样复制 | ✅ |
| `core/llm.py` | `core/engine/llm.py` | 原样复制 | ✅ |
| `core/host.py` | `core/engine/host.py` | 原样复制；新增 `MemoryAPI` 作为插件门面 | ✅ |
| `examples/demo.py` | `core/mock_llm.py` + `tests/` + `cli selfcheck` | 行为改编为离线 Mock | ✅ |
| `docs/protocol/desktop-v1.md` | `docs/protocol/desktop-v1.md` | 复制 | ✅ |

### 2.2 QQ 插件

| 旧位置 | 新位置 | 状态 |
|---|---|---|
| `jinyu_memory/__init__.py`（消息入口/命令/轮换/主动话题） | `plugins/qqbot/__init__.py` + `nonebot_adapter.py` | 🟡 部分：消息→记忆闭环、工具、钩子、降级 |
| `richmsg.py`（富消息/贴纸/SSRF） | 待迁入 qqbot 子模块 | ⬜ P2 |
| `linkmeta.py`（链接/视频/分镜调用） | qqbot 子模块 + dsh 适配层 | ⬜ P2/P5 |
| `bili_live.py`（B 站） | 计划独立 `plugins/bili-live` | ⬜ P2 |
| `reminder.py`（提醒） | qqbot 定时任务或独立插件 | ⬜ P2 |
| `owner_gate.py`（主人绑定） | core 身份模型 + qqbot 命令 | ⬜ P2 |
| `asr.py`（语音转文字） | qqbot 子模块 | ⬜ P2 |
| `mcp_client.py` | 独立 `plugins/mcp-tools` | ⬜ P2 |
| `task_supervisor.py` | `core/schedules.py` + `core/lease.py` 已部分替代 | 🟡 部分 |

### 2.3 桌宠 / 视频通话

| 旧位置/外部仓库 | 新位置 | 状态 |
|---|---|---|
| 主仓库 `desktop_server.py` | `plugins/desktop-pet/__init__.py` | 🟡 结构 + `handle_hello/handle_text` + 健康检查；未起 WS |
| `docs/protocol/desktop-v1.md` | 已复制 | ✅ |
| `deploy/install_desktop_adapter.sh` | 待迁 `scripts/install_desktop_adapter.sh` | ⬜ P3 |
| 独立仓库 `YVFENGXS/Jinyu-pet` | `scripts/install_external_plugins.sh` 拉取到 `external/` | ✅ 脚本 |
| 独立仓库 `YVFENGXS/jinyu-video-call` | `plugins/video-call/` 适配层；服务源码不复制 | 🟡 结构 + 适配接口 |
| 通话仓库自带 `jinyu_memory/core/` | 保持外部；推荐只走 `RP_HOST_BASE/TOKEN` | ⬜ P4 |

### 2.4 DSH / 视频分镜

| 来源 | 新位置 | 状态 |
|---|---|---|
| 用户 zip `jinyu-memory-dsh` | `scripts/install_dsh_plugin.py` 安装到 `$DSH_HOME/plugins/` | ✅ 安装脚本实测通过；源码不入库 |
| zip 的本地记忆仓 | `plugins/dsh-video-storyboard/store.py` 只读适配 + `dsh.import_session` | ✅ 行为级导入 |
| 第三方 `dsh-video-storyboard` CLI | `scripts/install_storyboard.sh` + 插件检测 `bin/storyboard.mjs` | ✅ 安装脚本；调用待 P5 |
| 旧 `deploy/install_storyboard.sh` | `scripts/install_storyboard.sh` | ✅ 改造迁移 |
| 第三方/改造版源码 | **不入库** | ✅ 遵守旧约定 |

### 2.5 运行入口与基础设施

| 旧位置 | 新位置 | 状态 |
|---|---|---|
| `bot.py`（11 行 NoneBot 启动） | `jinyv_memory.cli` + `plugins/qqbot/nonebot_adapter.py` | 🟡 需真实 NoneBot 环境验证 |
| `requirements.txt` | `pyproject.toml`（core 零依赖；插件 extras） | ✅ |
| `.env.example` | `.env.example` | 🟡 重新整理，未做旧变量全量对照 |
| `tools/serve.py`（宿主 HTTP API） | 计划 `plugins/host-api` 或 core 内置 HTTP | ⬜ P1 |
| `tests/test_regression.py`（104 项） | 计划 `tests/test_regression.py` 逐项迁移 | ⬜ P1 |
| `deploy/*.service` | 计划 `deploy/` | ⬜ P6 |

## 3. 分阶段实施计划

### P0（本轮，已完成）
- 新仓库、git 新历史（未 push）。
- core 13 模块迁移 + `MemoryAPI` + 租约/幂等。
- 插件契约、加载器、事件/工具/钩子/调度四套注册表。
- qqbot 最小闭环 + NoneBot 可选降级。
- desktop-pet / video-call / dsh-video-storyboard 骨架与健康检查。
- 自检 16 项、unittest 12 项、DSH Node 测试 15 项。

### P1：核心行为对齐
- 迁 104 项回归，确认 DB schema、水位线、披露、承诺、Dream 与旧版逐项一致。
- `tools/serve.py` 宿主 API 插件化（视频通话/桌宠依赖）。
- `.env` 旧变量与新配置的映射表；启动脚本兼容。
- 验收：旧回归全绿；qqbot 闭环不变。

### P2：QQ 外围逐模块搬
建议顺序（待确认）：`richmsg` → `linkmeta` → `reminder` → `owner_gate` → `asr` → `bili_live`。
- 每搬一个：旧/新同输入对照，记录差异；不允许顺手重构。
- 回复租约状态机：主号 primary 抢锁回复，观察号 observer 只记不回。
- 验收：每模块等价 + 回归新增对应用例。

### P3：桌宠
- 包装旧 `desktop_server.py` 为插件服务；协议 fixtures；配对/令牌。
- 客户端独立仓库只通过 `external/` 或系统路径接入。
- 验收：hello / 文本 / 断线重连 / 健康。

### P4：视频通话
- 宿主桥（`RP_HOST_BASE/TOKEN`）优先；禁止通话进程直连 SQLite。
- 跨窗口发话、记忆上下文、通话一轮落库。
- 验收：与 QQ 共享 lane 的召回一致，无双写。

### P5：DSH / 分镜
- 安装脚本在真实 DSH 环境验收；导入幂等与披露边界。
- 第三方分镜 CLI 调用包装（超时/失败回退四帧）。
- 验收：Node 测试 + 真实视频。

### P6：收尾
- 移除旧入口依赖、补部署文档、代码审查、推送新分支。

## 4. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 旧仓库继续改动 | 迁移副本分叉 | 定同步节奏；P1 前冻结核心行为 |
| 远程 main 非空 | 覆盖会丢历史 | 只推新分支；合并走 PR |
| QQ 入口耦合深 | 迁移回归量大 | 逐模块搬；先工具/钩子后命令 |
| 多进程写同一 SQLite | 游标互抢/重复归档 | 宿主 API 单写者；租约 + 幂等 |
| 桌宠 AGPL | 许可证污染主仓库 | 独立仓库、独立进程、协议交互 |
| dsh 约定冲突 | 违约/审计风险 | 安装脚本；源码不入库 |
| 时钟回拨 | 租约提前/延后释放 | TTL + 心跳 + 任务重试；跨机换中心服务 |
| 第三方分镜版本漂移 | 行为变化 | 固定 ref；升级需回归 |
