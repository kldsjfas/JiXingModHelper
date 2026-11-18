from __future__ import annotations

import threading
import time
import unittest
from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from astral_party_auto import web_app
from astral_party_auto.startup_diagnostics import watch_startup


def holder_keeps_lock(lock, ready, release):
    with lock:
        ready.set()
        release.wait(5)


class StartupWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "startup-diagnostics.txt"

    def tearDown(self):
        self.temp.cleanup()

    def api_class(self, slow_after):
        path = self.path

        class Api:
            def __init__(self):
                self._controller_lock = threading.RLock()
                self._logs = deque()

            def _append_log(self, message):
                self._logs.append(message)

            @watch_startup(lambda: path, "test", slow_after=slow_after)
            @web_app.exposed
            def bootstrap(self):
                self._startup_watch.phase("检测游戏位置")
                return "ready"

        return Api

    def test_waiting_for_the_controller_lock_records_the_thread_holding_it(self):
        api = self.api_class(0.2)()
        ready, release = threading.Event(), threading.Event()
        holder = threading.Thread(target=holder_keeps_lock, args=(api._controller_lock, ready, release), name="持锁线程")
        holder.start()
        ready.wait(2)
        threading.Timer(0.6, release.set).start()
        self.assertEqual(api.bootstrap(), {"ok": True, "data": "ready"})
        holder.join(2)
        report = self.path.read_text(encoding="utf-8")
        self.assertIn("已等待", report)
        self.assertIn("持锁线程", report)
        self.assertIn("holder_keeps_lock", report)
        self.assertIn("启动最终完成", report)
        self.assertIn("检测游戏位置", report)

    def test_fast_startup_writes_nothing_and_stays_exposed(self):
        api = self.api_class(5)()
        self.assertTrue(getattr(type(api).bootstrap, "_exposed", False))
        self.assertEqual(api.bootstrap(), {"ok": True, "data": "ready"})
        time.sleep(0.05)
        self.assertFalse(self.path.exists())


class BootstrapPhaseTests(unittest.TestCase):
    def test_bootstrap_reports_slow_phases_in_the_log(self):
        api = web_app.DesktopApi.__new__(web_app.DesktopApi)
        api._controller_lock = threading.RLock()
        api._logs = deque(maxlen=50)
        api._events, api._event_seq, api._events_lock = [], 0, threading.Lock()
        api.controller = Mock(has_game=True, draft_name="草稿", draft_items=[])
        api.controller.installed_mods.return_value = []
        api._dashboard_state = Mock(return_value={"has_game": True, "game_exe": "game.exe"})
        clock = iter([0.0, 0.0, 6.0, 6.0, 6.0, 6.0, 6.0, 6.0, 6.0, 6.0, 6.0, 6.0])
        with TemporaryDirectory() as temporary, patch.object(web_app, "DATA_DIR", Path(temporary)), \
                patch("astral_party_auto.startup_diagnostics.time.perf_counter", side_effect=lambda: next(clock, 6.0)):
            result = api.bootstrap()
        self.assertTrue(result["ok"], result)
        self.assertTrue(any("本次启动用时" in line and "检测游戏位置" in line for line in result["data"]["logs"]))


if __name__ == "__main__":
    unittest.main()
