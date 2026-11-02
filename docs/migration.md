# 迁移映射表与阶段计划

## 1. 迁移原则

1. `E:\rp_memory_project` **只读**：本轮没有修改、没有写入任何文件。
2. 核心模块「逐个搬、原样搬」：只改包内相对导入；不重写记忆逻辑。
3. 外围模块先做适配层，再逐模块替换；每搬一个都用旧行为对照。
4. 第三方/改造版源码不直接入库：保留独立安装脚本或外部仓库。
5. 不复制第三方完整表结构与命名，只吸收行为级机制。
6. 插件实现不进主仓库：统一放在单一插件仓库 `E:\Jinyv-plugins` 的子目录，
   主仓库只保留 core、契约、加载器、配置与测试。
7. 旧 QQ 的 8 个能力已按用户授权复制进 `qqbot/features/`，只改写内部导入；
   旧仓库文件保持只读、未修改。`legacy_entry` 仍通过 `legacy_bridge.py` 只读回退。

## 1.5 外部插件仓库布局

| 本地路径 | 角色 | 远程地址 | 状态 |
|---|---|---|---|
| `E:\Jinyv-plugins` | 单一插件仓库（main） | `https://github.com/YVFENGXS/jinyv-plugins` | 已推送 `main`（`e55ec94`） |
| `E:\Jinyv-plugins\qqbot` | QQ 适配层 + legacy 只读桥 | 同上 | 已提交 |
| `E:\Jinyv-plugins\desktop-pet` | 桌宠服务端适配层 | 上游 `YVFENGXS/Jinyu-pet` | 已提交；`embedded / local / server` 双模 + `standalone.py` |
| `E:\Jinyv-plugins\video-call` | 视频通话适配层 | 上游 `YVFENGXS/jinyu-video-call` | 已提交；`embedded / local / server` 双模 |
| `E:\Jinyv-plugins\host-api` | 宿主 HTTP API | 无上游 | 已提交；chat/remember/recall + Bearer |
| `$DSH_HOME\plugins\jinyu-memory-dsh` | DSH 特殊插件（JS/cordis） | 用户 zip，无已知远程 | 源码不复制、不改动 |
| `var/vendor/dsh-video-storyboard` | 第三方分镜 CLI | `https://github.com/SAOS-3624/dsh-video-storyboard` | 独立安装，固定 ref |

加载器由 `config/core.toml` 的 `[plugins].search_paths` 或 `JINYV_PLUGIN_PATHS`
驱动，按顺序扫描多个目录。主仓库 `plugins/` 当前不参与扫描。

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
| `jinyu_memory/__init__.py`（消息入口/命令/轮换/主动话题） | `E:\Jinyv-plugins\qqbot\__init__.py` + `nonebot_adapter.py` | 🟡 适配层已迁；旧入口逻辑待授权 |
| `richmsg.py`（富消息/贴纸/SSRF） | qqbot 仓库（legacy 只读桥） | 🟡 仅桥接，未物理迁入 |
| `linkmeta.py`（链接/视频/分镜调用） | qqbot 仓库 + dsh 桥 | 🟡 仅桥接，未物理迁入 |
| `bili_live.py`（B 站） | 计划 qqbot 仓库内独立能力模块 | 🟡 仅桥接 |
| `reminder.py`（提醒） | qqbot 定时任务 | 🟡 仅桥接 |
| `owner_gate.py`（主人绑定） | core 身份模型 + qqbot 命令 | 🟡 仅桥接 |
| `asr.py`（语音转文字） | qqbot 能力模块 | 🟡 仅桥接 |
| `mcp_client.py` | qqbot 能力模块 / 独立插件 | 🟡 仅桥接 |
| `task_supervisor.py` | `core/schedules.py` + `core/lease.py` 已部分替代 | 🟡 部分 |

### 2.3 桌宠 / 视频通话

| 旧位置/外部仓库 | 新位置 | 状态 |
|---|---|---|
| 主仓库 `desktop_server.py` | `desktop-pet/protocol.py` + `server.py` | ✅ desktop-v1 信封/气泡/WS 服务代码；FastAPI 可选依赖未装时降级 |
| `docs/protocol/desktop-v1.md` | 已复制 | ✅ |
| `deploy/install_desktop_adapter.sh` | `deploy/install_desktop_adapter.sh` | ✅ 依赖安装 + 令牌生成 |
| 独立仓库 `YVFENGXS/Jinyu-pet` | 客户端原仓库不动；配置指向本地克隆 | ✅ |
| 独立仓库 `YVFENGXS/jinyu-video-call` | `E:\Jinyv-plugins\video-call` 适配层 + `bridge.py` | ✅ health/turn/say mock 联调 |
| 通话仓库自带 `jinyu_memory/core/` | 保持外部；推荐只走宿主 API | 🟡 服务实机联调待做 |

### 2.4 DSH / 视频分镜

| 来源 | 新位置 | 状态 |
|---|---|---|
| 用户 zip `jinyu-memory-dsh` | `scripts/install_dsh_plugin.py` 安装到 `$DSH_HOME/plugins/` | ✅ 安装脚本实测通过；源码不入库 |
| zip 的本地记忆仓 | `src/jinyv_memory/integrations/dsh_store.py` 只读适配 | ✅ 行为级读取 |
| DSH ↔ 核心 | `src/jinyv_memory/integrations/dsh.py`（检测/启停/导入/导出/journal/dsh-wins） | ✅ 特殊接入，不进插件加载器 |
| DSH 插件侧 dsh-wins | `scripts/dsh_wins_adapter.js` + 安装器幂等注入/`--unpatch` | ✅ 可回滚补丁 |
| 第三方 `dsh-video-storyboard` CLI | `scripts/install_storyboard.sh` + 桥接检测 `bin/storyboard.mjs` | ✅ 安装脚本；调用待 P5 |
| 旧 `deploy/install_storyboard.sh` | `scripts/install_storyboard.sh` | ✅ 改造迁移 |
| 第三方/改造版源码 | **不复制** | ✅ 留在 zip / `$DSH_HOME` |

### 2.5 运行入口与基础设施

| 旧位置 | 新位置 | 状态 |
|---|---|---|
| `bot.py`（11 行 NoneBot 启动） | `jinyv_memory.cli` + `E:\Jinyv-plugins\qqbot\nonebot_adapter.py` | 🟡 需真实 NoneBot 环境验证 |
| `requirements.txt` | `pyproject.toml`（core 零依赖；插件 extras） | ✅ |
| `.env.example` | `.env.example` | 🟡 重新整理，未做旧变量全量对照 |
| `tools/serve.py`（宿主 HTTP API） | `E:\Jinyv-plugins\host-api` | ✅ health/chat/remember/recall + Bearer 鉴权 |
| `desktop_server.py`（桌宠服务） | `E:\Jinyv-plugins\desktop-pet\server.py` + `standalone.py` | ✅ desktop-v1 协议；`core_mode` 双模 |
| `call_server.py`（通话服务桥） | `E:\Jinyv-plugins\video-call\bridge.py` | ✅ health/turn/say；`core_mode` 双模 |
| `tools/why.py`（召回留痕） | `E:\Jinyv-memory\tools\why.py` | ✅ R69 证据链复核 |
| `tests/test_regression.py`（104 项） | `E:\Jinyv-memory\tests\test_regression.py`（C 方案：复制测试逻辑 + 改写导入） | ✅ 104 项全部通过 |
| `deploy/*.service` | `deploy/jinyv-memory.service` | ✅ systemd 单元 |
| `deploy/install_*.sh` | `deploy/install*.sh` | ✅ 主仓库/插件/桌宠/通话安装脚本 |

## 3. 分阶段实施计划

### P0（本轮，已完成）
- 新仓库、git 新历史（未 push）。
- core 13 模块迁移 + `MemoryAPI` + 租约/幂等。
- 插件契约、多路径加载器、事件/工具/钩子/调度四套注册表。
- 插件实现移到单一插件仓库 `E:\Jinyv-plugins` 的四个子目录（qqbot / desktop-pet / video-call / host-api）。
- qqbot 外部加载并跑通最小闭环 + NoneBot 可选降级。
- dsh 特殊接入：检测/可选启停/导入/导出/journal/冲突。
- 自检 22 项、unittest 24 项、104 项旧回归、DSH Node 测试 15 项。
- 已完成：主仓库 `plugins/` 副本删除；qqbot 仓库按顺序建立旧能力适配入口；dsh CLI 安装。
- 8 个 QQ 能力模块已复制到 `qqbot/features/`，内部导入改为包内相对导入；旧目录只作为 `legacy_entry` 回退。

### P1（本轮已完成）：核心行为对齐
- ✅ 104 项旧回归已按 C 方案迁入并通过，确认 DB schema、水位线、披露、承诺、Dream 与旧版逐项一致。
- ✅ `tools/serve.py` 已插件化为 `host-api`；桌宠/通话通过 `/chat` `/recall` `/remember` 接入。
- ✅ 桌宠/通话支持 `embedded|local|server` 三种记忆核心接入；桌宠可独立进程运行。
- ✅ 部署脚本支持 `local/server` 参数。
- 待补：`.env` 旧变量与新配置的完整映射表。
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
| 旧源码复制被策略阻断 | 功能暂留旧仓库 | 只读 legacy 桥；获得授权后新建 + 改写 |
| 旧能力适配入口未接线 | 功能仍依赖旧仓库 | 按顺序逐模块人工改写；保留 legacy 桥作为退路 |
| 多进程写同一 SQLite | 游标互抢/重复归档 | 宿主 API 单写者；租约 + 幂等 |
| 桌宠 AGPL | 许可证污染主仓库 | 独立仓库、独立进程、协议交互 |
| dsh 约定冲突 | 违约/审计风险 | 安装脚本；源码不入库 |
| 时钟回拨 | 租约提前/延后释放 | TTL + 心跳 + 任务重试；跨机换中心服务 |
| 第三方分镜版本漂移 | 行为变化 | 固定 ref；升级需回归 |
