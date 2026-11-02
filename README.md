# Jinyv-memory

以「记忆核心」为主体、外围能力插件化的瑾瑜记忆运行时。本轮已把插件实现移出主仓库：

- 主仓库只保留 **core、插件契约、加载器、配置、测试**。
- 插件是 **独立 git 仓库**，统一放在 `E:\Jinyv-plugins\`，加载器通过多路径发现。
- dsh 是 **特殊接入类型**（JS/cordis），不进入 Python 插件契约，由核心侧桥接。
- 桌宠 `Jinyu-pet`、通话 `jinyu-video-call`、dsh `jinyu-memory-dsh` 的原仓库/源码保持不动。

> **远程现状**
> `https://github.com/YVFENGXS/Jinyv-memory` 的 `main` 仍是旧实现（HEAD `8efba10`）。
> 新架构已推送到新分支 `refactor/plugin-core`（分支最新提交），**没有 force-push main**。

## 一、当前仓库布局

```
E:\Jinyv-memory/                      # 主仓库：core / 契约 / 加载器 / 配置 / 测试
├── src/jinyv_memory/
│   ├── contract.py                   # 插件契约
│   ├── loader.py                     # 多路径发现 / 生命周期 / 回滚
│   ├── runtime.py                    # 装配 core + 插件 + dsh 桥
│   ├── config.py paths.py events.py tools.py schedules.py accounts.py
│   ├── cli.py                        # selfcheck / plugins / chat / run / dsh
│   ├── core/
│   │   ├── memory.py                 # MemoryAPI（含 export_memory）
│   │   ├── lease.py                  # 跨进程租约 + 幂等任务
│   │   └── engine/                   # 迁移来的 13 个记忆模块
│   └── integrations/
│       ├── dsh.py                    # DSH 特殊接入桥（检测/启停/导入/导出）
│       └── dsh_store.py              # DSH 本地记忆仓只读适配
├── config/
│   ├── core.toml                     # [plugins].search_paths = ["../Jinyv-plugins"]
│   ├── accounts.toml
│   ├── plugins/*.toml                # 插件配置（实现不在主仓库）
│   └── integrations/dsh.toml         # dsh 特殊接入配置
├── plugins/                          # 仅说明；上一轮实现副本待你确认后删除
├── tests/                            # 14 项回归
└── docs/                             # 架构 / 契约 / 迁移 / desktop-v1 协议

E:\Jinyv-plugins/                     # 独立插件仓库集合
├── qqbot/                            # git 仓库：QQ 适配层 + legacy 只读桥
├── desktop-pet/                      # git 仓库：桌宠服务端适配层
└── video-call/                       # git 仓库：视频通话适配层
# dsh 源码不在 Jinyv-plugins：保留在用户 zip / $DSH_HOME/plugins/jinyu-memory-dsh
```

### 仓库地址与本地路径

| 仓库 | 远程地址 | 本地路径 | 状态 |
|---|---|---|---|
| 主仓库 | `https://github.com/YVFENGXS/Jinyv-memory` | `E:\Jinyv-memory` | 新历史，未推送 |
| QQ 插件 | **待确认**（建议 `.../Jinyv-qqbot`） | `E:\Jinyv-plugins\qqbot` | 已建独立仓库并提交 |
| 桌宠适配器 | **待确认**（建议 `.../Jinyv-plugin-desktop-pet`） | `E:\Jinyv-plugins\desktop-pet` | 已建独立仓库并提交 |
| 桌宠客户端 | `https://github.com/YVFENGXS/Jinyu-pet` | `E:\rp_memory_project\rp-desktop-pet` | 原仓库不动 |
| 视频通话适配器 | **待确认**（建议 `.../Jinyv-plugin-video-call`） | `E:\Jinyv-plugins\video-call` | 已建独立仓库并提交 |
| 视频通话服务 | `https://github.com/YVFENGXS/jinyu-video-call` | `E:\rp_memory_project\jinyu-video-call` | 原仓库不动 |
| dsh 改造版 | 用户 zip，无已知远程 | `$DSH_HOME/plugins/jinyu-memory-dsh` | 源码不复制、不改动 |
| 第三方分镜 | `https://github.com/SAOS-3624/dsh-video-storyboard` | `var/vendor/dsh-video-storyboard` | 独立安装，固定 ref |

## 二、本轮完成情况

| 项 | 状态 |
|---|---|
| 插件契约 / 加载器 | 已完成；支持多路径扫描、依赖排序、工具/钩子/事件/定时任务、失败隔离与降级 |
| qqbot 插件仓库 | 已完成独立仓库；外部加载后跑通 10 轮闭环 |
| desktop-pet / video-call | 已完成独立适配器仓库；可被发现、注册、健康检查、正常降级 |
| dsh 特殊接入 | 已完成检测、可选启停、导入/导出、journal 幂等、core-wins 冲突记录 |
| QQ 旧能力迁移 | 已建 6 项能力的插件侧适配入口（按顺序）；旧源码未复制，仍由 `legacy_bridge.py` 只读引用 |
| 主仓库 plugins 清理 | ✅ 已删除四个实现副本，仅保留说明文件 |
| dsh 安装 | ✅ `jinyv-memory dsh install --source <zip|目录>` 可安装到 DSH_HOME |
| 推送 | ✅ 已推送 `refactor/plugin-core`（分支最新提交）；`main` 未动 |
| 自检 / 测试 | selfcheck 22 项、unittest 14 项通过；dsh zip 自带 Node 测试 15/15 通过 |

## 三、快速开始

```powershell
$env:PYTHONPATH = "E:\Jinyv-memory\src"

# 离线自检：外部插件发现 + qqbot 闭环 + dsh 桥接
python -m jinyv_memory.cli selfcheck

# 14 项回归
python -m unittest discover -s tests -t . -v

# 插件健康（含 dsh 特殊接入）
python -m jinyv_memory.cli plugins

# 本地对话
python -m jinyv_memory.cli --mock chat --account owner_qq

# dsh 安装到 DSH_HOME 与桥接
python -m jinyv_memory.cli dsh install --source <zip|目录>
python -m jinyv_memory.cli dsh status
python -m jinyv_memory.cli dsh import --session <会话目录>
python -m jinyv_memory.cli dsh export --session <会话目录>
python -m jinyv_memory.cli dsh sync --session <会话目录>
```

插件搜索路径：

```toml
# config/core.toml
[plugins]
search_paths = ["../Jinyv-plugins"]
# 或环境变量 JINYV_PLUGIN_PATHS（Windows 用 ; 分隔）
```

## 四、dsh 特殊接入语义

dsh 不是 Python 插件，加载器不会扫描它。核心侧 `integrations/dsh.py` 负责：

1. **检测**：`package.json name=jinyu-memory-dsh`、`bin/storyboard.mjs`、本地记忆仓会话数。
2. **启停**：只有配置 `start_command` 且 `auto_start=true` 才托管进程；默认不替用户启动。
3. **导入**：DSH `cards.json/facts.json` → 核心 `event_cards/durable_facts`，核心唯一键兜底幂等。
4. **导出**：核心记忆 → DSH 同结构文件；用 `.jinyv-bridge.json` 记录双端哈希，重复导出不产生副本。
5. **冲突**：同一逻辑条目双端同时变化时默认 `core-wins`，冲突写入 journal，不静默覆盖；DSH 单端变化保留用户改动并记录状态。
6. **不复制源码**：DSH 源码只存在于用户 zip / `$DSH_HOME`，主仓库与 `Jinyv-plugins` 都不持有副本。

## 五、迁移状态

**已迁移**
- 旧 `core/` 13 个模块 → `src/jinyv_memory/core/engine/`（仅改内部导入，行为等价）。
- 旧 `docs/protocol/desktop-v1.md`。
- 旧 `deploy/install_storyboard.sh` → `scripts/install_storyboard.sh`。
- 本轮 qqbot 适配层 → `E:\Jinyv-plugins\qqbot`。

**独立仓库 / 适配层**
- qqbot：消息闭环、工具、钩子、定时任务、NoneBot 可选降级。
- desktop-pet：`handle_hello/handle_text` + 健康检查；客户端在原仓库。
- video-call：`memory_context/record_turn` + 健康检查；服务在原仓库。
- dsh：核心桥接；第三方分镜只检测与安装。

**未完成 / 受限**
- QQ 旧能力（`richmsg / linkmeta / bili_live / reminder / owner_gate / asr / mcp_client / task_supervisor`、主动话题/命令/轮换）仍在旧仓库；qqbot 仓库已按顺序建立适配入口 `features/__init__.py`，但**源码没有复制**。执行环境安全策略仍拒绝整块复制旧实现，需要改为逐模块人工改写或引入 submodule。
- 主仓库 `plugins/` 已删除实现副本，只保留 `plugins/README.md`。
- 104 项旧回归、宿主 HTTP API、桌宠 WebSocket 服务、通话联调、部署脚本。

## 六、待确认决策

1. **QQ 旧能力下一步**：适配入口已建，但旧源码不能复制。是否改为逐模块人工重写（不复制旧文件），或接受 submodule 引用旧仓库？
2. **三个新插件仓库远程地址**：qqbot / desktop-pet / video-call 的 GitHub 仓库名与归属？
3. **dsh 改造版安装**：继续用 zip（已支持 `dsh install`），还是后续提供 git 仓库？
4. **dsh 同步策略**：默认 `core-wins` 是否可接受？是否需要 `dsh-wins` 或人工合并队列？
5. **是否合并到 `main`**：新架构已在 `refactor/plugin-core`；是否走 PR 合并，或继续保留分支。
6. **桌宠/通话部署**：统一用 `external/` 或系统路径；视频通话是否强制只走宿主 API。
7. **引擎副本策略**：vendored + 同步脚本，还是 subtree/submodule。
8. **QQ 迁移优先级已执行顺序**：`richmsg → linkmeta → reminder → owner_gate → asr → bili_live`；是否继续按此顺序人工重写。

详见 [架构方案](docs/architecture.md)、[插件契约](docs/plugin-contract.md)、[迁移表](docs/migration.md)。
