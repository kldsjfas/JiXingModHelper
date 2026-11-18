"""启动卡住时留下现场：记录各阶段耗时，超时后写出所有线程的调用栈。

界面只显示“正在连接本地后端”，用户无法看到后端卡在哪一步；这份文件可以直接发给开发者定位。
"""
from __future__ import annotations

import sys
import threading
import time
import traceback
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Callable

STARTUP_SLOW_SECONDS = 20.0


class StartupWatch:
    def __init__(self, path: Path, *, slow_after: float = STARTUP_SLOW_SECONDS, version: str = "") -> None:
        self.path = Path(path)
        self.slow_after = slow_after
        self.version = version
        self.started = time.perf_counter()
        self.phases: list[tuple[str, float]] = []
        self._phase_start = self.started
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self.reported = False

    def start(self) -> "StartupWatch":
        self._timer = threading.Timer(self.slow_after, self._report_slow)
        self._timer.daemon = True
        self._timer.start()
        return self

    def phase(self, name: str) -> None:
        now = time.perf_counter()
        with self._lock:
            self.phases.append((name, now - self._phase_start))
            self._phase_start = now

    def elapsed(self) -> float:
        return time.perf_counter() - self.started

    def _lines(self) -> list[str]:
        with self._lock:
            phases = list(self.phases)
        lines = [f"吉星派对 Mod 助手 {self.version} 启动诊断 · {datetime.now().isoformat(timespec='seconds')}",
                 f"已等待 {self.elapsed():.1f} 秒。已完成的阶段："]
        lines.extend(f"  {name}: {seconds:.2f} 秒" for name, seconds in phases)
        if not phases:
            lines.append("  （尚未完成任何阶段）")
        return lines

    def _report_slow(self) -> None:
        names = {thread.ident: thread.name for thread in threading.enumerate()}
        lines = self._lines() + ["", "各线程当前位置："]
        for ident, frame in sys._current_frames().items():
            if ident == threading.get_ident():
                continue
            lines.append(f"--- 线程 {names.get(ident, ident)} ---")
            lines.extend(line.rstrip("\n") for line in traceback.format_stack(frame))
        self._write(lines)
        self.reported = True

    def finish(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        if self.reported:
            self._write(self._lines() + ["", f"启动最终完成，共 {self.elapsed():.1f} 秒。"], append=True)

    def _write(self, lines: list[str], *, append: bool = False) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a" if append else "w", encoding="utf-8") as stream:
                stream.write("\n".join(lines) + "\n")
        except OSError:
            pass


def watch_startup(path_factory: Callable[[], Path], version: str = "", slow_after: float = STARTUP_SLOW_SECONDS):
    """包在 API 外层：等待控制器锁的时间也计入，卡在锁上时能看到持锁线程。"""

    def decorate(method):
        @wraps(method)
        def wrapped(self, *args, **kwargs):
            watch = StartupWatch(path_factory(), slow_after=slow_after, version=version).start()
            self._startup_watch = watch
            try:
                return method(self, *args, **kwargs)
            finally:
                watch.finish()
                self._startup_watch = None

        return wrapped

    return decorate
