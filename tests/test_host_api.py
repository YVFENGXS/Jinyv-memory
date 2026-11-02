# -*- coding: utf-8 -*-
"""宿主 HTTP API：health / remember / recall / chat 与 Bearer 鉴权。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jinyv_memory.runtime import JinyvRuntime  # noqa: E402

TOKEN = "test-host-token"


class HostAPITest(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.TemporaryDirectory(prefix="jinyv-test-host-")
        self._old_token = os.environ.get("JINYV_HOST_API_TOKEN")
        self._old_port = os.environ.get("JINYV_PLUGIN_HOST_API_PORT")
        os.environ["JINYV_HOST_API_TOKEN"] = TOKEN
        os.environ["JINYV_PLUGIN_HOST_API_PORT"] = "0"
        self.runtime = JinyvRuntime(ROOT, data_dir=self.data.name, mock_llm=True)
        self.runtime.load_plugins()
        self.runtime.start()
        record = self.runtime.loader.get("host-api")
        self.assertIsNotNone(record)
        self.assertEqual(record.state.value, "running")
        self.port = record.plugin.port

    def tearDown(self):
        self.runtime.shutdown()
        for key, value in (("JINYV_HOST_API_TOKEN", self._old_token),
                           ("JINYV_PLUGIN_HOST_API_PORT", self._old_port)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.data.cleanup()

    def _request(self, path: str, payload: dict | None = None, auth: bool = True):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data,
            headers={"Content-Type": "application/json"},
        )
        if auth:
            request.add_header("Authorization", f"Bearer {TOKEN}")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "ignore") or "{}"
            return exc.code, json.loads(body)

    def test_health_remember_recall_chat(self):
        status, data = self._request("/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

        status, data = self._request("/remember", {
            "text": "主人喜欢米饭", "speaker": "主人", "importance": 0.9,
        })
        self.assertEqual(status, 200)
        self.assertTrue(data["id"])

        query = urllib.parse.quote("米饭")
        status, data = self._request(f"/recall?q={query}&limit=3")
        self.assertEqual(status, 200)
        self.assertTrue(data["facts"])

        status, data = self._request("/chat", {"text": "主人今天很累"})
        self.assertEqual(status, 200)
        self.assertTrue(data["reply"])

    def test_chat_store_reply_and_session(self):
        status, data = self._request("/chat", {
            "text": "通话里说的话", "store_reply": "通话页原文",
            "session": "host-test", "speaker": "主人",
        })
        self.assertEqual(status, 200)
        self.assertTrue(data["reply"])
        lane = self.runtime.core.accounts.get("owner_qq").with_session("host-test")
        stored = [turn["content"] for turn in self.runtime.core.memory.turns(lane)
                  if turn["role"] == "assistant"]
        self.assertEqual(stored[-1], "通话页原文")

    def test_auth_required(self):
        status, data = self._request("/remember", {"text": "x"}, auth=False)
        self.assertEqual(status, 401)
        self.assertEqual(data["error"], "unauthorized")


if __name__ == "__main__":
    unittest.main()
