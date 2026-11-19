"""房间身体桥：把服务器侧的动作指令送进 3D 房间页面。

通道是 Jinyv-room 自带的本地 WebSocket 桥（`tools/ai-bridge.mjs`）：
  服务器(Python) --ws--> 本地桥(127.0.0.1:8765) --ws--> 页面 `window.__APP__.ai`

- 只连回环地址；鉴权沿用桥自己的 `?token=` 握手，不引入新的一套凭证。
- 本模块零第三方依赖（RFC 6455 客户端帧用 asyncio 手写），避免给服务器加包。
- 回执形状与页面一致：`{"ok": bool, "code": str, "detail": {...}}`。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import ssl
from collections import deque
from typing import Any
from urllib.parse import parse_qsl, urlsplit

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME_BYTES = 8 << 20
OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG, OP_CONT = 0x1, 0x2, 0x8, 0x9, 0xA, 0x0


class StageBridgeError(RuntimeError):
    """连接层失败（握手被拒、对端断开、等不到回执）。业务失败仍走 result.code。"""


def _split_url(url: str) -> tuple[str, str, int, str, str | None]:
    parts = urlsplit(url)
    if parts.scheme not in ("ws", "wss"):
        raise ValueError(f"不是 WebSocket 地址：{url}")
    host = parts.hostname or "127.0.0.1"
    port = parts.port or (443 if parts.scheme == "wss" else 80)
    path = parts.path or "/"
    query = parts.query
    token = dict(parse_qsl(query)).get("token")
    target = f"{path}?{query}" if query else path
    return parts.scheme, host, port, target, token


def _mask(payload: bytes) -> bytes:
    key = os.urandom(4)
    return key + bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))


class StageBridge:
    """一条到本地桥的控制器连接。一个实例只跑一个房间页。"""

    def __init__(self, url: str, *, token: str | None = None, timeout: float = 25.0) -> None:
        scheme, host, port, target, query_token = _split_url(url)
        self.url = url
        self._scheme = scheme
        self._host = host
        self._port = port
        self.token = (token or query_token or os.environ.get("JINYV_AI_TOKEN") or "").strip().lstrip("\ufeff")
        if self.token and not query_token:
            target = f"{target}{'&' if '?' in target else '?'}token={self.token}"
        self._target = target
        self.timeout = timeout
        self.hello: dict[str, Any] = {}
        self.events: deque[dict[str, Any]] = deque(maxlen=200)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._counter = 0
        # 桥端 pending 按 id 全局去重：同进程多实例（视频回合 + 按需连接）必须带
        # 各自 nonce，否则第一条命令都叫 stage-{pid}-1，后到的会被判「请求 id 重复」。
        self._nonce = os.urandom(4).hex()
        self._closed = False
        # 端到端取证用：RP_BRIDGE_TRACE=1 时逐条打印入站/出站，默认静默
        self.trace = (os.environ.get("RP_BRIDGE_TRACE") or "").strip() == "1"

    # ---------- 连接 ----------

    async def connect(self, *, wait_hello: float = 5.0) -> dict[str, Any]:
        ssl_ctx = ssl.create_default_context() if self._scheme == "wss" else None
        self._reader, self._writer = await asyncio.open_connection(
            self._host, self._port, ssl=ssl_ctx, server_hostname=self._host if ssl_ctx else None
        )
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {self._target} HTTP/1.1\r\n"
            f"Host: {self._host}:{self._port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._writer.write(request.encode("ascii"))
        await self._writer.drain()
        head = await asyncio.wait_for(self._reader.readuntil(b"\r\n\r\n"), timeout=wait_hello)
        first = head.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in first:
            raise StageBridgeError(f"桥拒绝握手：{first}")
        self._closed = False
        self._task = asyncio.create_task(self._read_loop())
        deadline = asyncio.get_running_loop().time() + wait_hello
        while not self.hello and not self._closed:
            if asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(0.02)
        return self.hello

    @property
    def room_connected(self) -> bool:
        return bool(self.hello.get("roomConnected"))

    @property
    def methods(self) -> list[str]:
        return list(self.hello.get("methods") or [])

    # ---------- 收发 ----------

    async def _read_loop(self) -> None:
        assert self._reader is not None
        fragments: list[bytes] = []
        try:
            while not self._closed:
                opcode, payload, fin = await self._read_frame(self._reader)
                if opcode == OP_CLOSE:
                    break
                if opcode == OP_PING:
                    await self._send_frame(payload, opcode=OP_PONG)
                    continue
                if opcode == OP_CONT:
                    fragments.append(payload)
                elif opcode in (OP_TEXT, OP_BINARY):
                    fragments = [payload]
                else:
                    continue
                if not fin:
                    continue
                data = b"".join(fragments)
                fragments = []
                if data:
                    self._dispatch(data.decode("utf-8", "replace"))
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._closed = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(StageBridgeError("房间页在回执前断开"))
            self._pending.clear()

    async def _read_frame(self, reader: asyncio.StreamReader) -> tuple[int, bytes, bool]:
        head = await reader.readexactly(2)
        fin = bool(head[0] & 0x80)
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            length = int.from_bytes(await reader.readexactly(2), "big")
        elif length == 127:
            length = int.from_bytes(await reader.readexactly(8), "big")
        if length > MAX_FRAME_BYTES:
            raise StageBridgeError(f"帧过大：{length} 字节")
        key = await reader.readexactly(4) if masked else b""
        payload = await reader.readexactly(length) if length else b""
        if key:
            payload = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
        return opcode, payload, fin

    async def _send_frame(self, payload: bytes, *, opcode: int = OP_TEXT) -> None:
        if self._writer is None or self._closed:
            raise StageBridgeError("桥连接已关闭")
        length = len(payload)
        if length < 126:
            header = bytes([0x80 | opcode, 0x80 | length])
        elif length < 65536:
            header = bytes([0x80 | opcode, 0x80 | 126]) + length.to_bytes(2, "big")
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + length.to_bytes(8, "big")
        self._writer.write(header + _mask(payload))
        await self._writer.drain()

    def _dispatch(self, text: str) -> None:
        try:
            message = json.loads(text)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        kind = message.get("type")
        if kind == "hello":
            self.hello = message
            return
        if kind == "event":
            self.events.append(message.get("event") or {})
            return
        if kind == "result" and isinstance(message.get("id"), str):
            future = self._pending.pop(message["id"], None)
            if future and not future.done():
                future.set_result(message)

    # ---------- 对外 ----------

    def _next_id(self) -> str:
        """每实例带 nonce：桥端按 id 全局去重，同进程多实例不能共用计数空间。"""
        self._counter += 1
        return f"stage-{os.getpid()}-{self._nonce}-{self._counter}"

    async def invoke(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """发一条指令并等回执。业务失败原样返回 `{ok:false, code, detail}`。"""
        if self._closed:
            raise StageBridgeError("桥连接已关闭")
        command_id = self._next_id()
        message: dict[str, Any] = {"id": command_id, "method": method, "params": params or {}}
        if options:
            message["options"] = options
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[command_id] = future
        if self.trace:
            print(f"[stage] -> {method} {json.dumps(params or {}, ensure_ascii=False)}", flush=True)
        await self._send_frame(json.dumps(message, ensure_ascii=False).encode("utf-8"))
        try:
            result = await asyncio.wait_for(future, timeout=timeout or self.timeout)
        except asyncio.TimeoutError:
            self._pending.pop(command_id, None)
            if self.trace:
                print(f"[stage] <- {method} client_timeout", flush=True)
            return {"ok": False, "code": "client_timeout", "detail": {"method": method, "id": command_id}}
        finally:
            self._pending.pop(command_id, None)
        outcome = {
            "ok": bool(result.get("ok")),
            "code": result.get("code") or ("ok" if result.get("ok") else "internal"),
            "detail": result.get("detail") or {},
        }
        if self.trace:
            print(f"[stage] <- {method} {outcome['code']} "
                  f"{json.dumps(outcome['detail'], ensure_ascii=False)[:300]}", flush=True)
        return outcome

    async def close(self) -> None:
        self._closed = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self._writer is not None:
            try:
                await self._send_frame(b"", opcode=OP_CLOSE)
            except Exception:  # noqa: BLE001
                pass
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._writer = None
        self._reader = None

    async def __aenter__(self) -> "StageBridge":
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()


def health(url: str = "ws://127.0.0.1:8765", *, timeout: float = 2.0) -> dict[str, Any]:
    """读桥的 /health（纯 HTTP，同步，给脚本用）。不传 token，桥的 health 本就不需要。"""
    import urllib.request

    scheme, host, port, _target, _token = _split_url(url)
    http_scheme = "https" if scheme == "wss" else "http"
    with urllib.request.urlopen(f"{http_scheme}://{host}:{port}/health", timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))
