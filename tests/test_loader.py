# -*- coding: utf-8 -*-
"""加载器：发现、注册、降级、失败隔离与依赖顺序。"""
from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jinyv_memory.runtime import JinyvRuntime  # noqa: E402


def _write_plugin(base: Path, plugin_id: str, body: str, *, requires: tuple[str, ...] = ()):
    directory = base / plugin_id
    directory.mkdir(parents=True)
    deps = ", ".join(f'"{item}"' for item in requires)
    (directory / "plugin.toml").write_text(
        "[plugin]\n"
        f'id = "{plugin_id}"\n'
        f'name = "{plugin_id}"\n'
        'version = "0.1.0"\n'
        "requires = [" + deps + "]\n",
        encoding="utf-8",
    )
    (directory / "__init__.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return directory


class LoaderTest(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.TemporaryDirectory(prefix="jinyv-test-loader-")

    def tearDown(self):
        self.data.cleanup()

    def _boot(self, plugin_dirs=None):
        runtime = JinyvRuntime(ROOT, data_dir=self.data.name, mock_llm=True,
                               plugin_dirs=plugin_dirs)
        runtime.load_plugins()
        runtime.start()
        return runtime

    def test_discovers_all_four_plugins(self):
        runtime = self._boot()
        try:
            ids = set(runtime.loader.records)
            self.assertTrue({"qqbot", "desktop-pet", "video-call",
                             "dsh-video-storyboard"} <= ids)
            self.assertIsNotNone(runtime.loader.tools.get("qqbot.remember"))
            self.assertIsNotNone(runtime.loader.tools.get("qqbot.recall"))
            self.assertIsNotNone(runtime.loader.schedules.get("qqbot.heartbeat"))
            rows = {row["id"]: row for row in runtime.loader.health()}
            self.assertIn(rows["qqbot"]["state"], {"degraded", "running"})
            self.assertEqual(rows["desktop-pet"]["state"], "degraded")
            self.assertEqual(rows["video-call"]["state"], "degraded")
        finally:
            runtime.shutdown()

    def test_failed_plugin_does_not_break_host(self):
        with tempfile.TemporaryDirectory(prefix="jinyv-bad-plugins-") as raw:
            base = Path(raw)
            _write_plugin(base, "bad-plugin", """
                def create_plugin():
                    class Bad:
                        def setup(self, ctx):
                            ctx.tools.register(
                                __import__("jinyv_memory.contract", fromlist=["ToolSpec"]).ToolSpec(
                                    name="bad.tool", description="x",
                                    parameters={}, handler=lambda args, turn: 1,
                                ), ctx.plugin_id)
                            raise RuntimeError("boom")
                    return Bad()
            """)
            runtime = self._boot([base, ROOT / "plugins"])
            try:
                bad = runtime.loader.get("bad-plugin")
                self.assertIsNotNone(bad)
                self.assertEqual(bad.state.value, "failed")
                self.assertIsNone(runtime.loader.tools.get("bad.tool"))
                self.assertIsNotNone(runtime.loader.tools.get("qqbot.status"))
            finally:
                runtime.shutdown()

    def test_dependency_order(self):
        with tempfile.TemporaryDirectory(prefix="jinyv-dep-plugins-") as raw:
            base = Path(raw)
            _write_plugin(base, "dep-base", """
                def create_plugin():
                    class P:
                        def setup(self, ctx): ctx.events.emit("dep.ready", "base")
                    return P()
            """)
            _write_plugin(base, "dep-child", """
                def create_plugin():
                    class P:
                        def setup(self, ctx):
                            ctx.events.subscribe("dep.ready", lambda payload: ctx.events.emit(
                                "dep.child", payload))
                    return P()
            """, requires=("dep-base",))
            runtime = self._boot([base])
            try:
                order = runtime.loader._order
                self.assertLess(order.index("dep-base"), order.index("dep-child"))
            finally:
                runtime.shutdown()


if __name__ == "__main__":
    unittest.main()
