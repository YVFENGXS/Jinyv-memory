# 任务清单：身体工具接入与端到端验证

> 规格：[spec.md](./spec.md)。按顺序执行，每项完成即勾选。

## A. 代码

- [x] A1 `jinyv_stage/tools.py`
  - 扩展 `_SPECS`：`list_abilities`、`get_expression`、`set_light`、`open_door`、`close_door`、`perform`、`play_gesture`。
  - `sit` 增加 `posture`（`sit|lie`）。
  - 新增 `READONLY`、`summarize()`、`failure_payload()`、`success_hint()`。
  - `call()` 内把 `play_gesture` 组装为单步 `perform`。
- [x] A2 `jinyv_stage/bridge.py`
  - 增加 `RP_BRIDGE_TRACE=1` 时的出站/入站日志（默认静默）。
- [x] A3 `call_server.py`
  - `BRIDGE_MAX_READS`；只读/动作分开计数。
  - `_stage_handler` 接入 `failure_payload` / `success_hint`。
  - `_stage_json` 接入 `summarize`。
  - `register_tool(..., readonly=...)`。
  - 更新 `stage_body_note`。
- [x] A4 `jinyv_memory/__init__.py`
  - `_STAGE_READONLY` 改用 `stage_tools.READONLY`。
  - `_stage_json` / `_stage_not_done` / 成功分支接入新 helper。
  - 更新 `_STAGE_NOTE`。
  - 保持仅 `source=="video"` 连桥。
- [x] A5 `tools/stage_demo.py`
  - 追加 `list_abilities`、`perform` 多步、灯/门开合复原。

## B. 离线验证

- [x] B1 `tests/test_regression.py` 增加 FakeBridge 用例（映射、参数、只读集、压缩、跳过提示）。
- [x] B2 运行 `python tests\test_regression.py`，全部通过。

## C. 部署

- [x] C1 备份服务器 `/opt/qqbot/backups/<ts>/` 与 `.env`。
- [x] C2 scp：`jinyv_stage/tools.py`、`jinyv_stage/bridge.py`、`call_server.py`、`jinyv_memory/__init__.py`、`core/rp_skeleton.py`。
- [x] C3 配置 `RP_BRIDGE_URL` / `RP_BRIDGE_TOKEN`（线上原有，未改）/ `RP_BRIDGE_TRACE=1`（测后已移除）。
- [x] C4 `systemctl restart qqbot rp-call`，确认 active。

## D. 端到端

- [x] D1 本地起桥（token 记录）+ 现有产物起页面 + 打开房间页，`/health.roomConnected=true`。
- [x] D2 建反向隧道，服务器 `curl 127.0.0.1:8765/health` 通。
- [x] D3 `stage_demo.py` 全链通过（走位/坐/基元/表情/perform/灯门复原）。
- [x] D4 真实模型视频宿主：走位、坐/躺、挥手/基元、多步 perform 各 ≥1 次，记录桥入站、回执、位置/姿态变化。
- [x] D5 QQ 文本（非视频）边界：无身体、房间状态不变。
- [x] D6 失败回填实测：座位不支持 lie / 已坐在别处 → “这一步没有发生”+code。

## E. 收尾

- [x] E1 恢复 `.env` 原值（移除 `RP_BRIDGE_TRACE`）并重启 qqbot/rp-call。
- [x] E2 关闭本地桥与反向隧道。
- [x] E3 按链路汇报：端到端通 / 只通到某一层 / 未验证项与原因。
