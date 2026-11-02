# Jinyv-memory

以「记忆核心」为主体、外围能力全部插件化的瑾瑜记忆运行时。本轮已完成可运行骨架：core + 插件加载器 + qqbot 最小闭环，并为零散外围插件落下结构、契约与降级路径。

> **远程现状（重要）**
> `https://github.com/YVFENGXS/Jinyv-memory` **不是空仓库**：它已包含旧的单进程实现（HEAD `8efba10`），而 `E:\rp_memory_project\jinyu-memory` 就是它的本地克隆（含 9 个未提交改动）。
> 因此本仓库 `E:\Jinyv-memory` 采用**独立新历史 + 不覆盖 main**：已 `git init`，建议推到新分支 `refactor/plugin-core`，**不要 force-push main**。

## 本轮结论

| 项 | 状态 |
|---|---|
| core 记忆引擎 | 已迁移现有 `jinyu_memory/core` 全部 13 个模块，改为包内相对导入 |
| 插件契约 / 加载器 | 已实现：发现、依赖排序、setup/start/stop、工具/钩子/事件/定时任务注册、失败隔离与降级 |
| qqbot 插件 | **可运行最小闭环**：消息 → 钩子 → `MemoryAPI.chat` → 归档/召回；注册 3 个工具、1 个定时任务；NoneBot2 可选 |
| desktop-pet | 结构 + 适配接口 + 健康检查；客户端源码留在独立仓库，本轮未起 WS 服务 |
| video-call | 结构 + 适配接口 + 健康检查；服务源码留在独立仓库，本轮未起通话服务 |
| dsh-video-storyboard | 结构 + 检测/导入工具 + 独立安装脚本；改造版源码**不入库** |
| 自检 / 测试 | `selfcheck` 16 项通过；`unittest` 12 项通过；DSH zip 自带 Node 测试 15/15 通过 |

## 快速开始

```powershell
# 1) 离线自检（临时数据目录，不需要 API key）
$env:PYTHONPATH = "E:\Jinyv-memory\src"
python -m jinyv_memory.cli selfcheck

# 2) 最小回归
python -m unittest discover -s tests -t . -v

# 3) 插件健康
python -m jinyv_memory.cli plugins

# 4) 本地对话（Mock 或真实模型）
python -m jinyv_memory.cli --mock chat --account owner_qq

# 5) 常驻（运行定时任务）
python -m jinyv_memory.cli run
```

真实模型：复制 `.env.example` 为 `.env`，设置 `RP_FRONT_KEY` / `RP_BACK_KEY`；本运行时不会自动读取 `.env`，请由进程管理器注入环境变量。

## 目录

```
Jinyv-memory/
├── config/
│   ├── core.toml                    # 核心参数（不含密钥）
│   ├── accounts.toml                # 多账号 / 多平台 lane
│   └── plugins/
│       ├── qqbot.toml
│       ├── desktop-pet.toml
│       ├── video-call.toml
│       └── dsh-video-storyboard.toml
├── src/jinyv_memory/
│   ├── contract.py                  # 插件契约（签名级真源）
│   ├── loader.py                    # 发现 / 依赖 / 生命周期 / 回滚
│   ├── runtime.py                   # 进程装配
│   ├── config.py  paths.py  events.py  tools.py  schedules.py  accounts.py
│   ├── cli.py                       # selfcheck / plugins / chat / run
│   └── core/
│       ├── memory.py                # 插件可见的记忆门面
│       ├── lease.py                 # 跨进程租约 + 幂等任务
│       ├── runtime.py               # core 装配
│       └── engine/                  # 迁移来的 13 个记忆模块
├── plugins/
│   ├── qqbot/                       # 可运行适配层 + 可选 NoneBot 入口
│   ├── desktop-pet/                 # 桌宠服务端适配层
│   ├── video-call/                  # 视频通话适配层
│   └── dsh-video-storyboard/        # 检测 + DSH 本地记忆导入 + 分镜 CLI 检测
├── scripts/
│   ├── install_dsh_plugin.py        # 从 zip/目录安装改造版到 DSH_HOME
│   ├── install_storyboard.sh        # 第三方分镜 CLI（独立安装）
│   └── install_external_plugins.sh  # 拉取桌宠/通话独立仓库到 external/
├── tests/                           # 12 项最小回归
├── docs/
│   ├── architecture.md              # 架构、边界、阶段、风险
│   ├── plugin-contract.md           # 契约签名、manifest、注册规则
│   ├── migration.md                 # 旧模块 → 插件映射与迁移状态
│   └── protocol/desktop-v1.md       # 协议真源（从旧仓库迁移）
└── .tmp/                            # 临时勘察（gitignore）
```

## 已迁移 / 未迁移

**已迁移（行为等价）**
- `core/` 的 DB、归档、召回、披露、巩固、承诺、冲突、向量、水位线：原样迁入 `src/jinyv_memory/core/engine/`，仅把内部绝对导入改为相对导入。
- `docs/protocol/desktop-v1.md`。
- `deploy/install_storyboard.sh` → `scripts/install_storyboard.sh`（改为仓库内默认目录，仍不 vendoring）。
- `examples/demo.py` 的 Mock 行为 → `core/mock_llm.py` + 自检/测试。

**部分迁移**
- `jinyu_memory/__init__.py`（196 KB QQ 入口）→ `plugins/qqbot`：本轮只迁消息→记忆闭环、工具、钩子、降级；富消息、贴纸、ASR、B 站、提醒、主动话题、人设命令等仍待 P2。
- `desktop_server.py` → `plugins/desktop-pet`：本轮只有协议接口（`handle_hello/handle_text`）与健康检查，未起 WebSocket 服务。
- `call_server.py`（独立仓库）→ `plugins/video-call`：本轮只有适配接口与健康检查，未起服务。
- 第三方 `dsh-video-storyboard`：只检测 CLI；改造版 `jinyu-memory-dsh`：只提供安装脚本与本地记忆导入，不 vendor。

**未迁移**
- `richmsg.py`、`linkmeta.py`、`bili_live.py`、`reminder.py`、`owner_gate.py`、`asr.py`、`mcp_client.py`、`task_supervisor.py` 的完整功能。
- `tools/serve.py` 的宿主 HTTP API（视频通话/桌宠的 `RP_HOST_BASE` 依赖它）。
- `tests/test_regression.py` 的 104 项回归（本轮 12 项覆盖骨架与闭环）。
- 旧仓库 9 个未提交改动涉及的模块行为对比。

## 待确认决策

1. **dsh 改造版是否入库**：当前选择「插件 + 独立安装脚本」，不改旧约定。若要求开箱即跑，可改为直接入库（需你明确同意）。
2. **远程推送**：是否现在推 `refactor/plugin-core` 新分支？是否允许以后替换 `main`（需 force-push，风险高）？
3. **引擎副本策略**：本轮把旧 `core/` 复制进新仓库以保行为等价；后续是保持 vendored + 定期同步，还是用 subtree/submodule？
4. **QQ 完整迁移优先级**：richmsg / linkmeta / reminder / owner_gate / bili_live 谁先迁？
5. **桌宠 / 通话接入方式**：是否统一用 `external/` 目录 + 适配层，还是保留各自系统路径部署？
6. **视频通话记忆模式**：是否强制只走宿主 API（`RP_HOST_BASE/TOKEN`），禁止通话进程直连 SQLite？
7. **多账号身份模型**：短期继续用 lane 字符串（`qq-u…` / `qq-g…`），还是尽快落 principals/identities 表？
8. **Python 基线**：保持 3.10（需 `tomli`）还是抬到 3.11（内置 `tomllib`）？
9. **第三方分镜 ref**：继续固定 `6ef68acd…`，还是升级到新版本？

详见 [架构方案](docs/architecture.md)、[插件契约](docs/plugin-contract.md)、[迁移表](docs/migration.md)。
