# -*- coding: utf-8 -*-
"""瑾瑜宿主 HTTP 服务（零依赖）：给 dsh 等非 NoneBot 框架一个写入通道。

启动（工作目录为仓库根）：
  set RP_HOST_TOKEN=<随机串>
  python tools/serve.py --port 8710 --db data/rp_memory.db

接口（除 /health 外都要 Authorization: Bearer <token>）：
  GET  /health                     存活检查
  POST /chat     {"text","session"}          → {"reply","session"}   写入一轮对话
  POST /remember {"text","speaker","session"} → {"ok","id"}          直接写长期词条
  GET  /recall?q=&session=&limit=   → {"facts":[...],"cards":[...]} 只读召回

session 缺省为 host-main；同一个 session 串行执行，不同 session 互不阻塞。
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "jinyu_memory" / "core", ROOT / "core"):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from host import JinyuHost                                   # noqa: E402

MAX_BODY = 1 << 20                  # 1 MB
MAX_TEXT = 4000
SESSION_RE = re.compile(r"[^0-9A-Za-z_@.\-]")
POOL = None
TOKEN = ""


def authed(header: str, token: str) -> bool:
    """校验 Bearer token；常量时间比较，空 token 一律拒绝。"""
    token = str(token or "")
    header = str(header or "")
    if not token or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[7:].strip(), token)


def session_name(raw) -> str:
    name = SESSION_RE.sub("", str(raw or ""))[:64]
    return name or "host-main"


def _pool():
    global POOL
    if POOL is None:
        raise RuntimeError("pool 尚未初始化")
    return POOL


class Pool:
    """session → JinyuHost；懒加载，退出时统一冲刷。"""

    def __init__(self, db_path=""):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._hosts = {}

    def get(self, session: str) -> JinyuHost:
        with self._lock:
            if session not in self._hosts:
                self._hosts[session] = JinyuHost(
                    db_path=self.db_path or None, sid=session)
            return self._hosts[session]

    def close_all(self) -> None:
        with self._lock:
            hosts = list(self._hosts.values())
            self._hosts.clear()
        for host in hosts:
            try:
                host.close()
            except Exception as exc:              # noqa: BLE001 退出时不能互相拖死
                logging.getLogger("jinyu-host").warning("关闭会话失败：%s", exc)


class Handler(BaseHTTPRequestHandler):
    server_version = "jinyu-host/1"

    def _json(self, code: int, data: dict) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _auth(self) -> bool:
        if authed(self.headers.get("Authorization", ""), TOKEN):
            return True
        self._json(401, {"error": "unauthorized"})
        return False

    def _body(self) -> dict:
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = 0
        if size <= 0 or size > MAX_BODY:
            return {}
        try:
            data = json.loads(self.rfile.read(size).decode("utf-8", "ignore") or "{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:                 # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._json(200, {"ok": True, "name": "jinyu-host"})
            return
        if parsed.path == "/recall":
            if not self._auth():
                return
            qs = parse_qs(parsed.query)
            session = session_name((qs.get("session") or ["host-main"])[0])
            query = str((qs.get("q") or [""])[0])[:MAX_TEXT]
            try:
                limit = max(1, min(50, int((qs.get("limit") or ["6"])[0])))
            except ValueError:
                limit = 6
            try:
                data = _pool().get(session).recall(query, limit)
            except Exception as exc:          # noqa: BLE001
                logging.getLogger("jinyu-host").exception("recall 失败：%s", exc)
                self._json(500, {"error": "internal"})
                return
            self._json(200, data)
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:                # noqa: N802
        path = urlparse(self.path).path
        if path not in ("/chat", "/remember"):
            self._json(404, {"error": "not found"})
            return
        if not self._auth():
            return
        if path == "/chat" and not (os.environ.get("RP_FRONT_KEY")
                                    and os.environ.get("RP_BACK_KEY")):
            self._json(503, {"error": "model key missing"})
            return
        body = self._body()
        text = str(body.get("text") or "").strip()
        if not text:
            self._json(400, {"error": "text required"})
            return
        if len(text) > MAX_TEXT:
            self._json(413, {"error": "text too long"})
            return
        session = session_name(body.get("session"))
        try:
            host = _pool().get(session)
            if path == "/chat":
                self._json(200, {"reply": host.chat(text), "session": session})
                return
            speaker = str(body.get("speaker") or "")[:80]
            try:
                importance = max(0.0, min(1.0, float(body.get("importance") or 0.7)))
            except (TypeError, ValueError):
                importance = 0.7
            new_id = host.remember(text, speaker, importance)
            self._json(200, {"ok": True, "id": new_id})
        except Exception as exc:              # noqa: BLE001 不把堆栈吐给调用方
            logging.getLogger("jinyu-host").exception("%s 失败：%s", path, exc)
            self._json(500, {"error": "internal"})

    def log_message(self, fmt: str, *args) -> None:
        logging.getLogger("jinyu-host").info("%s %s", self.address_string(), fmt % args)


def main() -> None:
    parser = argparse.ArgumentParser(description="瑾瑜宿主 HTTP 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8710)
    parser.add_argument("--db", default=os.environ.get("RP_DB") or "")
    args = parser.parse_args()
    global TOKEN, POOL
    TOKEN = str(os.environ.get("RP_HOST_TOKEN") or "").strip()
    if not TOKEN:
        sys.exit("缺少 RP_HOST_TOKEN：先设置一个随机串再启动（除 /health 外都要 Bearer 校验）")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    POOL = Pool(args.db)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    logging.info("瑾瑜宿主服务 %s:%s（db=%s）", args.host, args.port, args.db or "RP_DB")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        POOL.close_all()


if __name__ == "__main__":
    main()
