# Jinyv-memory

以「记忆核心」为主体、外围能力插件化的瑾瑜记忆运行时。本轮已把插件实现移出主仓库：

- 主仓库只保留 **core、插件契约、加载器、配置、测试**。
- 插件统一放在 **单一外部 git 仓库** `E:\Jinyv-plugins\`（每个插件一个子目录），加载器通过多路径发现。
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
├── plugins/                          # 仅 README；实现副本已删除
├── tests/                            # 104 项旧回归 + unittest
└── docs/                             # 架构 / 契约 / 迁移 / desktop-v1 协议

E:\Jinyv-plugins/                     # 单一插件 git 仓库（main）
├── qqbot/                            # QQ 适配层 + features 能力入口
├── desktop-pet/                      # 桌宠 desktop-v1 服务端适配层
├── video-call/                       # 视频通话适配层 + HTTP 桥
├── host-api/                         # 宿主 HTTP API（/chat /remember /recall）
└── .repo-history/                    # 原子仓库历史备份（gitignore）
# dsh 源码不在 Jinyv-plugins：保留在用户 zip / $DSH_HOME/plugins/jinyu-memory-dsh
```

### 仓库地址与本地路径

| 仓库 | 远程地址 | 本地路径 | 状态 |
|---|---|---|---|
| 主仓库 | `https://github.com/YVFENGXS/Jinyv-memory` | `E:\Jinyv-memory` | 已推送 `refactor/plugin-core` |
| 插件仓库 | `https://github.com/YVFENGXS/jinyv-plugins` | `E:\Jinyv-plugins` | 已推送 `main`（`e55ec94`） |
| 桌宠客户端 | `https://github.com/YVFENGXS/Jinyu-pet` | `E:\rp_memory_project\rp-desktop-pet` | 原仓库不动，插件仓库只放适配层 |
| 视频通话服务 | `https://github.com/YVFENGXS/jinyu-video-call` | `E:\rp_memory_project\jinyu-video-call` | 原仓库不动，插件仓库只放适配层 |
| dsh 改造版 | 用户 zip，无已知远程 | `$DSH_HOME/plugins/jinyu-memory-dsh` | 源码不复制、不改动 |
| 第三方分镜 | `https://github.com/SAOS-3624/dsh-video-storyboard` | `var/vendor/dsh-video-storyboard` | 独立安装，固定 ref |

插件仓库远程已创建并推送 `main`。后续改动：

```powershell
git -C E:\Jinyv-plugins push origin main
```

## 二、本轮完成情况

| 项 | 状态 |
|---|---|
| 插件契约 / 加载器 | 已完成；支持多路径扫描、依赖排序、工具/钩子/事件/定时任务、失败隔离与降级 |
| qqbot 插件目录 | 已在单一插件仓库；外部加载后跑通 10 轮闭环 |
| desktop-pet / video-call | 已在单一插件仓库；可被发现、注册、健康检查、正常降级 |
| dsh 特殊接入 | 已完成检测、可选启停、导入/导出、journal 幂等、dsh-wins 冲突记录 |
| QQ 旧能力迁移 | ✅ 8 个能力模块已复制到 `qqbot/features/` 并改写内部导入；提醒/主人绑定有测试覆盖 |
| 单一插件仓库 | ✅ `E:\Jinyv-plugins` 已合并为一个 git 仓库并推送 `main` |
| 主仓库 plugins 清理 | ✅ 已删除四个实现副本，仅保留说明文件 |
| dsh 安装 | ✅ `jinyv-memory dsh install --source <zip|目录>` 可安装到 DSH_HOME |
| 推送 | ✅ 已推送 `refactor/plugin-core`（分支最新提交）；`main` 未动 |
| 宿主 HTTP API | ✅ `host-api` 插件：health / chat / remember / recall，Bearer 鉴权 |
| 桌宠 WebSocket | ✅ desktop-v1 协议与服务代码（FastAPI 可选依赖，未装时降级） |
| 通话联调 | ✅ `video-call` HTTP 桥 + mock 服务测试 |
| 部署脚本 | ✅ `deploy/` systemd、安装、插件、桌宠/通话适配脚本；适配脚本支持 `embedded / local / server` |
| 桌宠 / 通话双模 | ✅ `core_mode=embedded` 同进程；`local` 本机 host-api；`server` 远程 host-api；桌宠可独立运行 `standalone.py`；部署脚本同名参数 |
| 104 项旧回归 | ✅ C 方案：`tests/test_regression.py` 复制旧测试逻辑并改写导入，104 项全部通过 |
| 自检 / 测试 | selfcheck 22 项、unittest 24 项、104 项旧回归通过；dsh zip 自带 Node 测试 15/15 通过 |

## 三、快速开始

```powershell
$env:PYTHONPATH = "E:\Jinyv-memory\src"

# 离线自检：外部插件发现 + qqbot 闭环 + dsh 桥接
python -m jinyv_memory.cli selfcheck

# unittest 回归
python -m unittest discover -s tests -t . -v

# 104 项旧回归（C 方案迁移版）
python tests/test_regression.py

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

# 回滚 dsh-wins 插件侧补丁
python scripts/install_dsh_plugin.py --unpatch --target <插件安装目录>
# Node 侧适配测试
node --test tests/dsh_wins.test.mjs
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
5. **冲突**：同一逻辑条目双端同时变化时默认 `dsh-wins`，以 DSH 文本为准；核心旧结论进入 `.jinyv-bridge.json` 与 `var/integrations/dsh/conflicts.jsonl` 修订归档，不物理删除核心记录。
6. **插件侧生效**：安装脚本把 `scripts/dsh_wins_adapter.js` 放入插件 `lib/`，并在 `lib/index.js` 注入 `installDshWins(ctx, createStore)`；备份 `index.js.jinyv-orig`，`--unpatch` 可回滚。
7. **不复制源码**：DSH 源码只存在于用户 zip / `$DSH_HOME`，主仓库与 `Jinyv-plugins` 都不持有副本。

## 五、迁移状态

**已迁移**
- 旧 `core/` 13 个模块 → `src/jinyv_memory/core/engine/`（仅改内部导入，行为等价）。
- 旧 `docs/protocol/desktop-v1.md`。
- 旧 `deploy/install_storyboard.sh` → `scripts/install_storyboard.sh`。
- 本轮 qqbot 适配层 → `E:\Jinyv-plugins\qqbot`。

**单一插件仓库 / 适配层**
- qqbot：消息闭环、工具、钩子、定时任务、NoneBot 可选降级。
- desktop-pet：`handle_hello/handle_text` + 健康检查；客户端在原仓库。
- video-call：`memory_context/record_turn` + 健康检查；服务在原仓库。
- dsh：核心桥接；第三方分镜只检测与安装。

**未完成 / 受限**
- 旧 `__init__.py` 中的主动话题/命令/轮换（`legacy_entry`）尚未复制；如需启用，把 `legacy_source_dir` 指向旧包目录作为回退。
- 8 个能力的 NoneBot/富消息实际接线仍按 P2 逐模块对照，当前先保证可导入、可测试、最小闭环不回归。
- Linux 实机部署与真实桌宠/通话端联调尚未执行。

## 六、待确认决策

1. **QQ 旧入口下一步**：8 个能力模块已迁入；主动话题/命令/轮换仍在旧 `__init__.py`，是否继续拆分为 `legacy/` 子模块？
2. **插件仓库远程**：`https://github.com/YVFENGXS/jinyv-plugins` 已创建并推送 `main`；后续改动继续推同一分支。
3. **dsh 改造版安装**：继续用 zip（已支持 `dsh install`），还是后续提供 git 仓库？
4. **dsh 同步策略**：已按确认改为 `dsh-wins`；`core-wins` 仅作为配置回退保留。是否需要人工合并队列待定。
5. **是否合并到 `main`**：新架构已在 `refactor/plugin-core`；是否走 PR 合并，或继续保留分支。
6. **桌宠/通话部署**：统一用 `external/` 或系统路径；视频通话是否强制只走宿主 API。
7. **引擎副本策略**：vendored + 同步脚本，还是 subtree/submodule。
8. **QQ 迁移优先级已执行顺序**：`richmsg → linkmeta → reminder → owner_gate → asr → bili_live`；是否继续按此顺序人工重写。

详见 [架构方案](docs/architecture.md)、[插件契约](docs/plugin-contract.md)、[迁移表](docs/migration.md)。
