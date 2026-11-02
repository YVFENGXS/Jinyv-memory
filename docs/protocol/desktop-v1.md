# 金羽桌宠协议 desktop-v1（草案）

> 状态：Draft；服务端适配层已实现（embedded / local / server 三种记忆核心接入）
> 真源：本文件为唯一真源，客户端仓库只钉版本与能力位，不复制正文
> 适用：`jinyu-memory` 服务端 ↔ 用户本地桌宠客户端（Windows 优先）
> 日期：2026-10-31

## 0. 一句话

客户端是薄壳：负责渲染、输入、本地闲时状态机；记忆召回、披露判定、LLM、提醒与租约全部留在服务器。

## 1. 目标与非目标

目标：

- 服务器能把「一段回复 + 情绪 + 动作意图」推送到设备，并按气泡节奏呈现
- 设备能把文字、语音、戳/摸/拎/甩/投喂、在场状态回传给服务器
- 断网可用、重连不重复投递、隐私不越界

非目标：

- **不下发记忆卡、检索结果、召回留痕、披露打分**；下行只有拼好的最终文本与最小元数据
- 不做远程控制（截屏、键鼠注入），与 CUA 无关
- v1 只支持已验证主人的设备；普通用户设备留待 v2
- 不做流式 token 下发；v1 以「整条回复 + 多气泡」为单位

## 2. 角色与信任模型

| 角色 | 说明 |
|---|---|
| Server | 记忆核心、LLM、披露层、提醒、租约所在方；可本机或远程 |
| Device | 用户本地桌宠客户端，持设备令牌 |
| Principal | 真人主体；v1 仅 `owner` 可配对 |

约定：

1. **永远由设备出站连接服务器**，服务器不主动拨入（家用 NAT 场景）。
2. 设备令牌只代表「这台机器」，不代表「已通过主人验证」；设备与主人主体的绑定靠一次性配对码。
3. 配对码复用现有机制：仅在已验证主人的私聊中生成，6 位、单次有效、5 分钟过期；群聊拒绝。
4. 服务器对设备上报的在场状态只作参考，缺省时按保守档位处理（见 §8）。

## 3. 传输与连接生命周期

- 端点：`wss://<host>/desktop/v1/ws`
- 服务端可独立进程运行（`desktop-pet/standalone.py`），通过 host-api 连本地或远程记忆核心
- 认证：`Authorization: Bearer <device_token>`；令牌 32 字节随机数，仅在配对成功时下发一次
- 建立连接后 10 秒内必须发送 `hello`，否则服务端关闭（4001）
- 心跳：服务端每 30 秒发 `ping`，客户端回 `pong`；连续 2 次无响应判定掉线
- 重连：指数退避 1s → 60s，带 ±30% 抖动；v1 不做会话恢复，重连即新建会话
- 同一主体允许多设备在线；**主动推送只发给 primary 设备**（见 §13），用户发起的回复只回给发起设备
- 单连接下行节流上限见 §11

## 4. 消息信封

所有消息统一为 JSON 文本帧：

```json
{
  "v": 1,
  "id": "01HQ8Z6K3M9T2R5W7X1Y4C0B8A",
  "type": "pet.say",
  "ts": 1793427694000,
  "seq": 12,
  "device_id": "dev_01HQ8Z...",
  "payload": {}
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `v` | int | 是 | 协议大版本，固定 `1` |
| `id` | string | 是 | ULID，幂等去重键 |
| `type` | string | 是 | 消息类型，见 §5/§6 |
| `ts` | int | 是 | 发送端毫秒时间戳 |
| `seq` | int | 是 | 上行：连接内自增；下行：设备维度自增 |
| `device_id` | string | 否 | 下行必带；上行由 `hello` 绑定 |
| `payload` | object | 是 | 见各类型定义 |

兼容规则：

- 未知 `type`：忽略并计数，不报错、不断连
- 未知字段：忽略
- 单帧上限 64KB；图片、音频、动画包一律走 HTTPS URL 或本地资源 ID，不进 WS

## 5. 上行消息

### 5.1 `hello`

```json
{
  "v": 1,
  "id": "01HQ8Z0...",
  "type": "hello",
  "ts": 1793427694000,
  "seq": 0,
  "payload": {
    "protocol": "desktop-v1",
    "device_id": "dev_01HQ8Z...",
    "device_name": "书房台式机",
    "client_version": "0.1.0",
    "capabilities": ["bubble.v1", "video.webm", "region.hit", "presence.report"],
    "presence": "active",
    "pairing": { "code": "483920" }
  }
}
```

`pairing` 仅在首次绑定时出现；已绑定设备重复提交配对码返回 4002。

### 5.2 `event.text`

```json
{
  "v": 1,
  "id": "01HQ8Z1...",
  "type": "event.text",
  "ts": 1793427700000,
  "seq": 1,
  "payload": { "text": "今天好累", "source": "keyboard" }
}
```

`source`：`keyboard` | `option`（选项气泡）| `quick`（快捷短语）。

### 5.3 `event.voice`

```json
{
  "v": 1,
  "id": "01HQ8Z2...",
  "type": "event.voice",
  "ts": 1793427710000,
  "seq": 2,
  "payload": {
    "audio_url": "https://<host>/desktop/audio/<one-time-token>",
    "duration_ms": 4200,
    "format": "opus",
    "sample_rate": 16000,
    "asr_text": null
  }
}
```

- 优先本地 ASR（`asr.local` 能力位），直接发 `event.text`，不发音频
- 无本地 ASR 时先 HTTPS 上传一次性音频 URL，再用本消息引用；单段 ≤ 60 秒
- 音频 URL 5 分钟失效，服务端取用后即删

### 5.4 `event.interaction`

```json
{
  "v": 1,
  "id": "01HQ8Z3...",
  "type": "event.interaction",
  "ts": 1793427720000,
  "seq": 3,
  "payload": {
    "kind": "poke",
    "position": { "x": 1180, "y": 740 },
    "detail": { "count": 3, "duration_ms": 800 }
  }
}
```

`kind`：`poke`（戳）| `pet`（摸头）| `drag`（拖动结束）| `throw`（甩出）| `feed`（投喂）| `figure`（换形象）。

### 5.5 `event.presence`

```json
{
  "v": 1,
  "id": "01HQ8Z4...",
  "type": "event.presence",
  "ts": 1793427730000,
  "seq": 4,
  "payload": { "state": "fullscreen", "changed_at": 1793427730000 }
}
```

`state`：`active` | `idle` | `locked` | `screen_share` | `fullscreen`。

### 5.6 `ack`

对每条下行消息回执：

```json
{
  "v": 1,
  "id": "01HQ8Z5...",
  "type": "ack",
  "ts": 1793427740000,
  "seq": 5,
  "payload": { "ref_id": "01HQ8Z6K...", "status": "rendered" }
}
```

`status`：`received` | `rendered` | `failed` | `dropped`（离线超时丢弃）。

### 5.7 `pong`

回 `ping`，无 payload。

## 6. 下行消息

### 6.1 `hello.ack`

```json
{
  "v": 1,
  "id": "01HQ8Z7...",
  "type": "hello.ack",
  "ts": 1793427695000,
  "seq": 0,
  "device_id": "dev_01HQ8Z...",
  "payload": {
    "session": "s_01HQ8Z...",
    "primary": true,
    "server_time": 1793427695000,
    "heartbeat_s": 30,
    "protocol": "desktop-v1",
    "limits": { "downlink_per_min": 60, "input_per_sec": 1, "proactive_per_hour": 6 },
    "figures": ["whale"]
  }
}
```

### 6.2 `pet.say`

```json
{
  "v": 1,
  "id": "01HQ8Z8...",
  "type": "pet.say",
  "ts": 1793427750000,
  "seq": 1,
  "device_id": "dev_01HQ8Z...",
  "payload": {
    "msg_id": "m_01HQ8Z9...",
    "reply_to": "01HQ8Z1...",
    "display_no": 812,
    "bubbles": [
      { "text": "你回来啦。", "emotion": "happy", "delay_ms": 0, "tts_url": null },
      { "text": "今天……还好吗？", "emotion": "shy", "delay_ms": 900 }
    ],
    "actions": ["wave"],
    "priority": 1,
    "ttl_ms": 300000
  }
}
```

- `bubbles` 有序，客户端按 `delay_ms` 依次呈现；拆分规则沿用服务端富消息逻辑，单条回复最多 6 个气泡
- `display_no` 供前台 AI 引用「第几条消息」，与撤回指令对齐；可空
- `emotion` 见 §7.1；`actions` 为可选的动作意图，等价于附带的 `pet.act`
- `ttl_ms` 内未呈现则丢弃并回 `dropped`

### 6.3 `pet.act`

```json
{
  "v": 1,
  "id": "01HQ8ZA...",
  "type": "pet.act",
  "ts": 1793427760000,
  "seq": 2,
  "device_id": "dev_01HQ8Z...",
  "payload": {
    "action": "headpat",
    "emotion": "shy",
    "loop": 1,
    "interrupt": true,
    "priority": 2
  }
}
```

- 服务器只发语义动作，**不发帧、不发时长、不发片段文件名**；节奏由客户端状态机负责
- 未知 `action`：客户端回退到 `idle`，不报错
- `priority`：0 闲时背景 / 1 普通 / 2 打断当前 / 3 强打断（如提醒）

### 6.4 `pet.move`

```json
{
  "v": 1,
  "id": "01HQ8ZB...",
  "type": "pet.move",
  "ts": 1793427770000,
  "seq": 3,
  "device_id": "dev_01HQ8Z...",
  "payload": {
    "x": 1200,
    "y": 860,
    "coordinate": "screen_px",
    "duration_ms": 600,
    "interrupt": false
  }
}
```

坐标越界由客户端夹到工作区内；多屏以主屏坐标系为准。

### 6.5 `pet.quiet`

```json
{
  "v": 1,
  "id": "01HQ8ZC...",
  "type": "pet.quiet",
  "ts": 1793427780000,
  "seq": 4,
  "device_id": "dev_01HQ8Z...",
  "payload": { "reason": "user_busy", "until_ms": 1793431380000 }
}
```

### 6.6 `pet.figure`

```json
{
  "v": 1,
  "id": "01HQ8ZD...",
  "type": "pet.figure",
  "ts": 1793427790000,
  "seq": 5,
  "device_id": "dev_01HQ8Z...",
  "payload": { "pack": "whale", "outfit": "default" }
}
```

### 6.7 `recall`

```json
{
  "v": 1,
  "id": "01HQ8ZE...",
  "type": "recall",
  "ts": 1793427800000,
  "seq": 6,
  "device_id": "dev_01HQ8Z...",
  "payload": { "msg_id": "m_01HQ8Z9...", "bubble_index": null }
}
```

- 仅对 120 秒内呈现过的消息有效，与现有 QQ 撤回窗口保持一致
- `bubble_index` 为空表示整条回复

### 6.8 `state.push`

```json
{
  "v": 1,
  "id": "01HQ8ZF...",
  "type": "state.push",
  "ts": 1793427810000,
  "seq": 7,
  "device_id": "dev_01HQ8Z...",
  "payload": {
    "context_ratio": 0.62,
    "token_left": 48200,
    "balance": 12.5,
    "updated_at": 1793427810000
  }
}
```

仅发给 primary 且已完成主人验证的设备；余额未知时字段为 `null`，不报错。

### 6.9 `error`

```json
{
  "v": 1,
  "id": "01HQ8ZG...",
  "type": "error",
  "ts": 1793427820000,
  "seq": 8,
  "device_id": "dev_01HQ8Z...",
  "payload": { "code": 4003, "message": "device revoked", "retryable": false }
+}
```

## 7. 语义词表

### 7.1 emotion

`neutral` | `happy` | `shy` | `sad` | `angry` | `sleepy` | `curious` | `worried`

### 7.2 action

`idle` | `wave` | `nod` | `shake` | `headpat` | `eat` | `sleep` | `think` | `celebrate` | `peek` | `hide`

客户端负责建立「服务端语义 → 本地片段」映射；映射表随客户端发布，服务端不感知片段名。

## 8. 在场状态与披露

所有判定在服务端完成，客户端无法请求披露等级。

| 设备状态 | 主动私密推送 | 记忆披露上限 |
|---|---|---|
| 已验证主人 + `active` + 无共享 | 允许 | `full` |
| 已验证主人 + `idle` | 仅提醒类 | `full`（可延迟） |
| `locked` | 禁止 | `hint`，提醒排队到下次 `active` |
| `screen_share` / `fullscreen` | 禁止 | `hint`（按直播场景处理） |
| 状态未知 | 禁止 | `paraphrase` |
| 未配对设备 | 禁止 | 不注入记忆，公开兜底 |

补充约束：

- 下行**永远不含**记忆卡原文、召回条目、披露打分、trace 记录
- 排队超过 30 分钟的私密推送直接作废，不跨状态补发
- 该表为默认值，具体阈值可在服务端配置；任何放宽都必须经过披露层，不得在适配器里绕开

## 9. 确认、重试与离线

- 下行采用 at-least-once：未收到 `ack` 时按 2s / 10s / 30s 重试，3 次后丢弃并记录
- 去重键为信封 `id`；重复投递客户端必须幂等（同一 `msg_id` 不重复渲染）
- 离线队列：客户端最多缓存 20 条上行事件，重连后按原 `ts` 顺序补发
- 超过 5 分钟的 `event.text` / `event.voice` 服务端直接丢弃并回 `ack: dropped`；`event.presence` 只保留最后一条
- 客户端不落库对话历史，只保留最近 1 条回复文本与本地台词缓存

## 10. 安全

- 全链路 TLS；禁止明文 `ws://`
- 令牌存储：Windows 用 DPAPI，macOS 用 Keychain；不得写入配置文件
- 配对码 6 位、5 分钟过期、单次有效、最多 3 次尝试；仅已验证主人私聊可生成
- 重放防护：`ts` 与服务器时间偏差超过 ±5 分钟拒收；`id` 缓存 10 分钟去重
- 撤销：管理员可吊销设备；被吊销设备连回返回 4003 且不再重连
- 日志脱敏：服务端日志不记录 `event.text` 与 `event.voice` 原文，只记长度与哈希前缀

## 11. 限流与配额

| 项 | 默认值 | 说明 |
|---|---|---|
| 上行文本 | 1 条/秒，突发 5 | 超出丢弃并回 4004 |
| 上行语音 | 1 段/30 秒 | 单段 ≤ 60 秒 |
| 下行消息 | 60 条/分钟 | 超出排队 |
| 主动私密推送 | 6 条/小时 | 与现有主动话题配额合并计算 |
| 单帧大小 | 64KB | 超出直接断开 |
| 设备数 | 每主体 3 台 | 超出需先吊销 |

## 12. 能力协商与版本

- `hello.capabilities` 声明客户端能力；服务端缺少对应能力时自动降级（例如无 `bubble.v1` 就只发单气泡）
- 能力位：`bubble.v1`、`video.webm`、`region.hit`、`presence.report`、`asr.local`、`tts.play`、`figure.pack`、`move.screen`
- v1 内只做加法：新增 `type`、新增可选字段；客户端必须忽略未知项
- 破坏性变更走 `wss://<host>/desktop/v2/ws`，v1 保留至少一个发布周期

## 13. 与服务端内部的映射（实现建议）

- 新增适配器：`desktop` 通道，与 QQ / B站 / 直播适配器同级，不进入记忆核心
- 会话 lane：已验证主人设备 → `peer:<owner_principal>`；设备维度再记 `device:<device_id>` 便于限流与吊销
- 受众字段：桌面设备默认 `owner`；未配对设备不写记忆
- 主动任务：复用现有租约，保证全局唯一执行；推送前查 primary 与在场状态
- 提醒 / 归档 / 离线巩固：保持原链路，只在投递层多一个出口
- 召回留痕：`recall_trace` 增加 `device_id` 字段，便于定位「哪台设备看到了什么」
- 前台工具：新增 `pet_say` / `pet_act` / `pet_move` / `pet_quiet`，与现有搜图、撤回工具同一管道；工具失败静默降级

## 14. 错误码

| 码 | 含义 | 可重试 |
|---|---|---|
| 4001 | 认证失败 / 未按时 hello | 否 |
| 4002 | 需要配对或配对码无效 | 否 |
| 4003 | 设备已吊销 | 否 |
| 4004 | 触发限流 | 是（退避后） |
| 4005 | 协议版本不支持 | 否 |
| 4006 | 帧过大 / 解析失败 | 是（修正后） |
| 5001 | 服务端内部错误 | 是 |

## 15. 样例

见 `tests/protocol_fixtures/`：

- `hello.json`
- `hello_ack.json`
- `pet_say.json`
- `pet_act.json`
- `event_interaction.json`
- `ack.json`

## 16. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-10-31 | 初稿：连接、信封、双向消息、披露映射、限流与错误码 |
