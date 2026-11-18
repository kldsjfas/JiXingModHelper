from __future__ import annotations

import threading
import unittest
from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from astral_party_auto import web_app
from astral_party_auto.error_report import build_error_report
from astral_party_auto.startup_diagnostics import StartupWatch


def make_api(data_dir: Path):
    api = web_app.DesktopApi.__new__(web_app.DesktopApi)
    api._controller_lock = threading.RLock()
    api._logs = deque(["[10:00:00] 已连接游戏：game.exe", "[10:00:05] 操作失败：Card 的卡面/立绘字段中找不到此精确资源名。"], maxlen=50)
    api._events, api._event_seq, api._events_lock = [], 0, threading.Lock()
    api._startup_watch = None
    api.controller = SimpleNamespace(has_game=True, game_executable=Path("D:/Game/AstralParty_CN.exe"),
                                     game_location_mode="auto", game_detection_note="", aa_dirs=(Path("D:/Game/aa"),),
                                     draft_name="我的Mod套装",
                                     draft_items=[{"kind": "visual_animation"}, {"kind": "texture"}, {"kind": "texture"}])
    return api


class ErrorReportContentTests(unittest.TestCase):
    def test_report_collects_logs_errors_startup_diagnostics_and_threads(self):
        with TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            (data_dir / "startup-diagnostics.txt").write_text("已等待 25.0 秒\n检测游戏位置", encoding="utf-8")
            api = make_api(data_dir)
            api._startup_watch = StartupWatch(data_dir / "unused.txt")
            api._startup_watch.phase("等待其它操作完成")
            release = threading.Event()
            waiting = threading.Thread(target=release.wait, args=(5,), name="后台检测")
            waiting.start()
            try:
                text = build_error_report(api, version="9.9.9", data_dir=data_dir, startup_name="startup-diagnostics.txt")
            finally:
                release.set()
                waiting.join(2)
        self.assertIn("--- 线程 后台检测 ---", text)
        for expected in ("v9.9.9", "AstralParty_CN.exe", "texture × 2", "visual_animation × 1", "【启动仍在进行】",
                         "等待其它操作完成", "【上次启动诊断】", "已等待 25.0 秒", "【最近的错误】（1 条）",
                         "找不到此精确资源名", "【运行日志】（2 条）", "【各线程当前位置】", "--- 线程"):
            self.assertIn(expected, text)

    def test_unreadable_values_are_reported_instead_of_failing(self):
        class Broken:
            @property
            def has_game(self):
                raise OSError("磁盘无法访问")

        api = make_api(Path("."))
        api.controller = Broken()
        text = build_error_report(api, version="1", data_dir=Path("missing"), startup_name="none.txt")
        self.assertIn("已连接：读取失败（OSError: 磁盘无法访问）", text)


class ErrorReportExportTests(unittest.TestCase):
    def test_export_works_while_another_call_holds_the_controller_lock(self):
        with TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            api = make_api(data_dir)
            target = data_dir / "report"
            api._pick_path = Mock(return_value=target)
            held, release = threading.Event(), threading.Event()

            def hold():
                with api._controller_lock:
                    held.set()
                    release.wait(5)

            holder = threading.Thread(target=hold)
            holder.start()
            held.wait(2)
            result = {}
            worker = threading.Thread(target=lambda: result.update(value=api.export_error_report()))
            with patch.object(web_app, "DATA_DIR", data_dir):
                worker.start()
                worker.join(3)
            stuck = worker.is_alive()
            release.set()
            holder.join(2)
            worker.join(2)
            self.assertFalse(stuck, "导出被控制器锁挡住了")
            self.assertTrue(result["value"]["ok"], result)
            saved = Path(result["value"]["data"]["path"])
            self.assertEqual(saved.name, "report.txt")
            text = saved.read_text(encoding="utf-8-sig")
            self.assertIn("hold", text)
            self.assertTrue(any("已导出错误报告" in line for line in api._logs))

    def test_cancelled_save_dialog_writes_nothing(self):
        api = make_api(Path("."))
        api._pick_path = Mock(return_value=None)
        self.assertEqual(api.export_error_report(), {"ok": True, "data": None})


if __name__ == "__main__":
    unittest.main()
