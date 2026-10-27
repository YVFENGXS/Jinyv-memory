# -*- coding: utf-8 -*-
"""MCP 客户端（P2）：把只读 MCP 服务器接成前台可调的工具。

设计约束（和 rp_skeleton 的同步工具循环对齐）：
- 前台 `chat` 跑在线程池里，工具 handler 也是同步的；MCP 是 asyncio，
  所以这里用一个常驻后台线程 + 独立事件循环托管所有 stdio 会话，
  同步侧通过 run_coroutine_threadsafe 调过去。
- 只读、白名单：每个服务器必须显式列出允许的工具；不给 shell/写文件类能力。
- 失败静默降级：MCP 没装、启动失败、调用超时，都只记一条日志，回复照常。
- 结果进 prompt 前由 rp_skeleton 统一截断（TOOL_RESULT_CHARS），这里不再放宽。

配置（默认读插件目录下的 mcp.json，可用 RP_MCP_CONFIG 指定）：
{
  "servers": {
    "fetch": {
      "command": "/opt/qqbot/bot/.venv/bin/python",
      "args": ["-m", "mcp_server_fetch"],
      "tools": ["fetch"],
      "timeout": 10
    }
  }
}
环境变量：
  RP_MCP          1/0，总开关，默认 1（配置文件不存在时自然不生效）
  RP_MCP_CONFIG   配置文件路径
  RP_MCP_TIMEOUT  单次调用默认超时秒数，默认 6
"""
import asyncio
import concurrent.futures
import json
import logging
import os
import re
import threading

logger = logging.getLogger("jinyu_memory.mcp")

DEFAULT_TIMEOUT = 6.0
MAX_TOOLS = 8                 # 工具总数上限，防止一次塞爆 prompt
MAX_DESC = 300                # 工具描述截断，控制 token
_START_TIMEOUT = 12.0

# 只把必要变量传给 MCP 子进程，避免把密钥透给第三方工具
_ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE",
             "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
             "http_proxy", "https_proxy", "no_proxy")
_OFF = {"0", "off", "false", "no", "none", "disabled"}


def _off(value) -> bool:
    return str(value or "").strip().lower() in _OFF


def load_config(path=None):
    """读 mcp.json，返回 servers 字典；没配置/关闭/坏文件都返回空字典。"""
    if _off(os.environ.get("RP_MCP")):
        return {}
    raw = str(path or os.environ.get("RP_MCP_CONFIG") or "").strip()
    if raw:
        file = os.path.expanduser(raw)
    else:
        file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp.json")
    if not os.path.isfile(file):
        return {}
    try:
        with open(file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning("MCP 配置读取失败 path=%s：%s", file, exc)
        return {}
    servers = (data or {}).get("servers") if isinstance(data, dict) else None
    return servers if isinstance(servers, dict) else {}


def _tool_name(server: str, tool: str) -> str:
    """OpenAI 函数名只允许字母数字下划线横线，最长 64。"""
    name = re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp_{server}_{tool}")
    return name[:64]


def _flatten(result) -> str:
    """把 MCP 的 content 列表压成纯文本；图片只留占位符。"""
    parts = []
    for block in getattr(result, "content", None) or []:
        kind = str(getattr(block, "type", "") or "")
        if kind == "text":
            parts.append(str(getattr(block, "text", "") or ""))
        elif kind == "image":
            parts.append("[图片内容已省略]")
        elif kind == "resource":
            resource = getattr(block, "resource", None)
            parts.append(str(getattr(resource, "text", "") or "[嵌入资源]"))
        elif kind:
            parts.append(f"[{kind}]")
    text = "\n".join(p for p in parts if p).strip()
    return text or "（工具没有返回文本内容）"


class MCPManager:
    """常驻后台事件循环，托管若干 stdio MCP 会话。"""

    def __init__(self, servers: dict, timeout: float = DEFAULT_TIMEOUT):
        self.servers = servers or {}
        self.timeout = max(0.5, min(float(timeout or DEFAULT_TIMEOUT), 30.0))
        self._sessions = {}
        self._tools = []                  # 可直接给 middleware 的规格
        self._by_name = {}
        self._loop = None
        self._stop_event = None
        self._thread = None
        self._ready = threading.Event()
        self._started = False

    # ------------------------------------------------------------ 生命周期
    def start(self) -> int:
        """起线程并等首批工具就绪；返回注册到的工具数（0 表示不可用）。"""
        if not self.servers:
            return 0
        self._thread = threading.Thread(target=self._run, name="jinyu-mcp",
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(_START_TIMEOUT):
            logger.warning("MCP 启动超时（%.0fs）", _START_TIMEOUT)
        return len(self._tools) if self._started else 0

    def stop(self, timeout: float = 3.0) -> None:
        if self._loop is not None and self._stop_event is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop_event.set)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout)
        self._started = False

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._serve())
        except Exception as exc:                       # noqa: BLE001
            logger.warning("MCP 线程异常退出：%s", exc)
        finally:
            self._ready.set()
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:                          # noqa: BLE001
                pass
            loop.close()

    async def _serve(self) -> None:
        from contextlib import AsyncExitStack

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._stop_event = asyncio.Event()
        env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
        async with AsyncExitStack() as stack:
            for server, cfg in self.servers.items():
                if not isinstance(cfg, dict) or not cfg.get("command"):
                    logger.warning("MCP 服务器 %s 配置不完整，跳过", server)
                    continue
                if len(self._tools) >= MAX_TOOLS:
                    logger.warning("MCP 工具已达上限 %d，忽略 %s 其余工具", MAX_TOOLS, server)
                    break
                try:
                    params = StdioServerParameters(
                        command=str(cfg["command"]),
                        args=[str(a) for a in (cfg.get("args") or [])],
                        env={**env, **(cfg.get("env") or {})})
                    read, write = await stack.enter_async_context(stdio_client(params))
                    session = await stack.enter_async_context(ClientSession(read, write))
                    await asyncio.wait_for(session.initialize(), timeout=10)
                    listed = await asyncio.wait_for(session.list_tools(), timeout=10)
                    self._sessions[server] = session
                    allow = {str(t) for t in (cfg.get("tools") or [])}
                    timeout = max(0.5, min(float(cfg.get("timeout") or self.timeout), 30.0))
                    loaded = 0
                    for tool in listed.tools:
                        if allow and "*" not in allow and tool.name not in allow:
                            continue
                        if len(self._tools) >= MAX_TOOLS:
                            break
                        # MCP 1.x 用 inputSchema，2.x 改成 input_schema，两个都认
                        schema = (getattr(tool, "inputSchema", None)
                                  or getattr(tool, "input_schema", None)
                                  or {"type": "object", "properties": {}})
                        spec = {
                            "name": _tool_name(server, tool.name),
                            "description": f"[MCP:{server}] "
                                           + str(tool.description or "")[:MAX_DESC],
                            "parameters": schema,
                            "server": server,
                            "tool": tool.name,
                            "timeout": timeout,
                        }
                        self._tools.append(spec)
                        self._by_name[spec["name"]] = spec
                        loaded += 1
                    logger.info("MCP 服务器 %s 就绪 工具=%d", server, loaded)
                except Exception as exc:               # noqa: BLE001  单个服务器坏了不影响别的
                    logger.warning("MCP 服务器 %s 启动失败：%s", server, exc)
            self._started = bool(self._by_name)
            self._ready.set()
            await self._stop_event.wait()

    # ------------------------------------------------------------ 调用
    def tool_specs(self):
        """给插件注册用：name / description / parameters 三件套。"""
        return [{"name": s["name"], "description": s["description"],
                 "parameters": s["parameters"]} for s in self._tools]

    def call(self, name: str, args: dict, timeout=None) -> str:
        """同步侧调用：超时/会话缺失都抛异常，由 _run_tool 统一降级。"""
        spec = self._by_name.get(name)
        if not spec:
            raise KeyError(f"未知 MCP 工具 {name}")
        if not self._started or self._loop is None:
            raise RuntimeError("MCP 未就绪")
        session = self._sessions.get(spec["server"])
        if session is None:
            raise RuntimeError(f"MCP 会话 {spec['server']} 不可用")
        limit = float(timeout or spec["timeout"] or self.timeout)
        future = asyncio.run_coroutine_threadsafe(
            session.call_tool(spec["tool"], args or {}), self._loop)
        try:
            result = future.result(timeout=limit)
        except concurrent.futures.TimeoutError:
            self._loop.call_soon_threadsafe(future.cancel)
            raise TimeoutError(f"MCP 调用超时 {name} >{limit:.1f}s")
        return _flatten(result)
