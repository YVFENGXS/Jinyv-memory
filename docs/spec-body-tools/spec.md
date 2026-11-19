# 规格：服务器侧身体工具接入（走位 / 坐躺 / 动作 / 灯光门 / perform / 基元 / listAbilities）

日期：2026-11-13
范围：`E:\rp_memory_project\jinyv-memory`（服务器侧）
只读参考：`E:\Jinyv-room`（本轮禁止修改、禁止跑 build）

## 1. 目标

把房间页面已经支持的新能力挂给前台模型，并保证：

1. 模型能调到：走位、坐/躺（座位 + 姿态）、站起、播放 clip、表情、灯、门、`perform` 多步编排、手势基元、`listAbilities` 能力枚举。
2. 成功有明确回执；失败明确回填「这一步没有发生」+ 原因码，模型不能把失败当成功继续叙述。
3. 只读工具（状态/目标/能力/动作名/图层/表情）不占动作步数，但有独立只读上限。
4. QQ 宿主 `/jinyu/chat` 的 `source=="video"` 分支可用新能力；`qq`/`desk` 等非视频来源维持无身体（只读边界不变）。

## 2. 渲染端契约（已核对，作为唯一口径）

### 2.1 桥白名单 `E:\Jinyv-room\tools\ai-bridge.mjs`

允许方法：`getState getPlayerState listTargets moveTo lookAt sit stand setLight openDoor closeDoor setTime setTimeFlow showScreen clearScreen playClip getClipState setEyesClosed setLayer getLayers setExpression getExpression startSpeaking stopSpeaking cancelQueued cancel perform listAbilities`。

**结论：白名单没有 `aiPrimitive` 方法。** 手势基元必须走 `perform` 的 `{kind:"primitive"}` 步骤，不能调用 `bridge.invoke("aiPrimitive")`。

### 2.2 页面 `packages/renderer/src/ai.ts`

- `sit(seatId, posture?)`：`posture` 为 `"sit"|"lie"`；省略时用座位默认姿态。
- `perform(steps)`：步骤 `kind` = `move|look|sit|stand|clip|primitive|expression|light|door|wait`。
  - `primitive` 字段：`name / amplitude / durationS / repeat / side("L"|"R"|"both")`。
  - 失败回执：`{ok:false, code, detail:{done:[{index,kind,ok,code,detail}], failedAt, lastCode, failedStep}}`。
  - 成功回执：`{ok:true, detail:{done, count}}`。
  - 基元被通道占用/冷却/说话打断时是 `ok:true` 且 `started:false`，带 `reason`（`channel_busy|cooldown|speaking`）——**这类也必须告诉模型“这一步没发生”**。
- `listAbilities()` 返回：`{clips, primitives, primitiveCatalog, seats:[{id,label,postures}], lights:[{id,roomId,on}], doors:[{id,open}], expressions}`。
- 基元名（8）：`tail_wag, body_twist, shrug, head_tilt, arm_raise, stretch, lean_in_out, idle_breath`。
- 表情名（8）：`neutral, happy, smile, sad, angry, surprised, shy, thinking`。
- 座位（8）：`seat.dining.chair, seat.desk.chair, seat.her.window, seat.her.armchair, seat.living.sofa, seat.living.daybed, seat.her.bed.left, seat.her.bed.right`。

## 3. 代码改动

### 3.1 `jinyv_stage/tools.py`

**新增工具规格（`_SPECS`）**，公开名 → 页面方法：

| 工具名 | method | 参数（server → 页面 rename） |
| --- | --- | --- |
| `list_abilities` | `listAbilities` | 无（只读） |
| `get_expression` | `getExpression` | 无（只读） |
| `set_light` | `setLight` | `objectId:string`, `on:boolean` |
| `open_door` | `openDoor` | `objectId:string` |
| `close_door` | `closeDoor` | `objectId:string` |
| `perform` | `perform` | `steps:array`（步骤对象，见 §3.1.1） |
| `play_gesture` | `perform` | `name:string`, `amplitude?:number`, `durationS?:number`, `repeat?:number`, `side?:"L"\|"R"\|"both"` |

**修改 `sit`**：保留 `seat_id`（rename `seatId`），新增可选 `posture`（enum `sit`/`lie`）。描述写明“不传 posture 用座位默认姿态；`lie` 只适用于 daybed / 双人床”。

**新增常量与纯函数（供两个进程复用）**：

- `READONLY: frozenset[str]` = `{get_state, list_targets, list_abilities, get_clip_state, get_layers, get_expression}`。
- `summarize(name, result) -> dict`：不修改入参，返回压缩后的回执：
  - `list_targets` → `{ok, count, ids}`（seat/door 排前）。
  - `list_abilities` → `{ok, primitives, expressions, seats:[id], lights:[{id,on}], doors:[{id,open}], clips:<count>}`。
  - `get_clip_state` → `{ok, clip, names}`。
  - `perform`（以及 `play_gesture` 的内部 perform 失败）→ 保留 `ok/code/message`、`failedAt/lastCode/failedStep`、`done` 仅压成 `[{i,kind,ok,code,started?}]`、`skipped`。
- `failure_payload(name, code, result=None) -> dict`：复制 result 并加 `hint`：
  「`{name}` 这一步没有发生（`{code}`）：这个动作没有执行成功；不要说你已经做了、也不要说自己现在已经在那里，只说打算做或还没做到；想重试就再调一次工具；照常把话说下去，不要向主人报告工具错误。」
- `success_hint(name, result) -> str | None`：`perform/play_gesture` 成功但存在 `started:false` 的基元步骤时，返回「第 N 步手势没有发生（reason），不要说你做了」；其余返回 `None`。

**`_SPECS` 中 `perform` 步骤 schema**（仅描述，页面仍做最终校验）：每个 step 是 object，`kind` enum 十种，字段按 §2.2 列全。`play_gesture` 在 `call()` 内组装单步 `perform`：

```python
{"steps": [{"kind": "primitive", "name": ..., "amplitude": ..., "durationS": ...,
            "repeat": ..., "side": ...}]}
```

**`tools/stage_demo.py`**：追加 `list_abilities`、`perform`（move→sit→primitive→expression→wait→stand）、`set_light` 开关复原、`open_door/close_door` 复原各一次，便于无模型端到端先跑通。

### 3.2 `call_server.py`

- 删除本地 `_STAGE_READONLY` 的隐式口径，改为 `stage_tools.READONLY`。
- 新增 `BRIDGE_MAX_READS`（`RP_BRIDGE_MAX_READS`，默认 4）。
- `_stage_handler`：
  - 只读：`reads` 计数与上限；不增加 `calls`。
  - 动作：`calls` 计数与 `BRIDGE_MAX_CALLS`；不增加 `reads`。
  - 失败（含超时/预算/页面业务码）：走 `stage_tools.failure_payload`，不得原样只回 `{ok:false}`。
  - 成功但 `success_hint` 非空（基元被跳过）：把 hint 加进回执。
- `_stage_json`：先 `stage_tools.summarize(name, result)` 再截断（`BRIDGE_RESULT_CHARS`）。
- `stage_tools_on`：`MW.register_tool(..., readonly=fn["name"] in stage_tools.READONLY)`。
- `stage_body_note` 补充：`perform` 可一次编排多步；`list_abilities` 查可用动作/基元/座位/灯门；**没有成功回执就不能叙述已完成**。
- 预算语义与 QQ 分支一致：只读不占动作步数。

### 3.3 `jinyv_memory/__init__.py`（QQ 宿主视频分支）

- `_STAGE_READONLY` 改为 `stage_tools.READONLY`（保留元组兜底）。
- `_stage_json` 接入 `stage_tools.summarize`；`_stage_not_done` 接入 `failure_payload`。
- `_stage_handler` 成功分支接入 `success_hint`（基元跳过要明说没发生）。
- `_STAGE_NOTE` 补充新能力与“失败/跳过不得叙述为完成”。
- 保持：只有 `source=="video"` 才 `_stage_open()`；`qq/desk` 不挂身体工具，行为不变。

### 3.4 `jinyv_stage/bridge.py`（可选观测，默认静默）

新增 `RP_BRIDGE_TRACE=1` 时，`invoke` 打印一行出站 `method + params` 和一行入站 `code + detail`，用于端到端取证；默认不输出，避免生产噪声。

### 3.5 离线测试 `tests/test_regression.py`

用 FakeBridge 断言（不连网）：

1. 每个新工具映射到正确 method 与参数：`sit` 带 `posture→posture`、`set_light→setLight`、`open_door→openDoor`、`close_door→closeDoor`、`list_abilities→listAbilities`、`perform→perform`。
2. `play_gesture` 发出的 method 是 `perform`，且 `steps[0].kind=="primitive"`、字段改名正确（`amplitude` 等）。
3. `READONLY` 恰为 6 个只读名，且不含任何动作工具。
4. `summarize` 对 `list_abilities`、`perform` 失败/基元跳过的压缩结果包含 `failedAt/lastCode/skipped` 等关键字段。
5. 既有回归全过。

## 4. 端到端验证（真实模型）

### 4.1 环境（测试前拉起，测试后收好）

- 本地：`node E:\Jinyv-room\tools\ai-bridge.mjs --token <token> --port 8765`；用现有构建产物起静态页（**不 build**），浏览器打开 `http://localhost:4173/?aiToken=<token>&aiBridge=ws://127.0.0.1:8765`，确认 `/health` 的 `roomConnected=true`。
- 反向隧道：本地 `ssh -N -R 8765:127.0.0.1:8765 -i e:\rp_memory_project\.deploy_key ubuntu@122.51.245.140`，服务器上 `curl 127.0.0.1:8765/health` 应为 `roomConnected=true`。
- 服务器 `.env` 临时补 `RP_BRIDGE_URL=ws://127.0.0.1:8765`、`RP_BRIDGE_TOKEN=<token>`、`RP_BRIDGE_TRACE=1`，备份原值；部署代码后 `systemctl restart qqbot`。
- 测完：恢复 `.env` 原值、重启、关闭本地隧道与桥（保留记录）。

### 4.2 测例（逐条记录：桥入站方法/参数 → 回执 code/detail → get_state 位置或姿态变化）

1. 无模型直连：`python tools/stage_demo.py --token <token>`（走位、坐下、挥手、表情、灯/门复原、perform 全链）。
2. 真实模型（QQ 宿主视频分支）：向服务器 `/jinyu/chat` POST `source="video"`，指令包含：
   - 「走到窗边坐下」→ 期望 `move_to` + `sit` 或 `perform`；
   - 「躺到床左边」→ 期望 `sit(seat.her.bed.left, lie)`；
   - 「向我挥挥手/抬手」→ 期望 `play_gesture(arm_raise)` 或 `perform.primitive`；
   - 「先去窗边坐下、挥手、开客厅灯」→ 期望一次 `perform` 多步。
   从 journalctl 抓 `[jinyu] stage tool ...`，前后各调一次 `get_state` 记录 `position/posture/seated/lights`。
3. 非视频边界：同一 QQ 会话发普通消息，确认工具未挂载/无桥连接、房间状态不变。
4. 失败回填：故意 `sit` 到用户占用或未知座位，确认回执含 `hint:"这一步没有发生"` 与 `code`。

### 4.3 记录与交付

- 每条测例记录：时间、通道（直连/视频宿主/QQ 文本）、工具名、参数、回执、位置/姿态差。
- 无法验证的项单列：卡在哪一层（桥/页面/隧道/模型/配置），附带证据。

## 5. 验收标准

- [ ] `python tests\test_regression.py` 全过。
- [ ] `TOOL_NAMES` 覆盖 §3.1 全部动作与只读工具；`READONLY` 不含动作工具。
- [ ] 任何动作失败/超时/基元跳过，回填文本都含“这一步没有发生”及原因码。
- [ ] 视频宿主分支四种能力各至少一次真实模型回执；非视频来源无身体。
- [ ] 未修改 `E:\Jinyv-room` 任何文件，未执行前端 build。
- [ ] 测试环境已还原（.env、qqbot、隧道、桥）。
