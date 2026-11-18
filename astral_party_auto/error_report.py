"""汇总一份可以直接发给开发者的错误报告。

只读取已有状态，不获取控制器锁：启动或某个操作卡住时也能导出，并带上此刻各线程所在位置。
"""
from __future__ import annotations

import platform
import sys
import threading
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path

MAX_STARTUP_DIAGNOSTICS_CHARS = 200_000


def _safe(label: str, read) -> str:
    try:
        value = read()
    except Exception as exc:  # noqa: BLE001 - 报告必须尽量完整，单项失败只记录原因
        return f"{label}：读取失败（{exc.__class__.__name__}: {exc}）"
    return f"{label}：{value}"


def _snapshot(sequence) -> list:
    # 日志由其它线程追加，复制时可能恰好被修改，重试几次即可。
    for _ in range(5):
        try:
            return list(sequence)
        except RuntimeError:
            continue
    return []


def _thread_stacks() -> list[str]:
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    lines = []
    for ident, frame in sys._current_frames().items():
        if ident == threading.get_ident():
            continue
        lines.append(f"--- 线程 {names.get(ident, ident)} ---")
        lines.extend(line.rstrip("\n") for line in traceback.format_stack(frame))
    return lines or ["（没有其它线程）"]


def build_error_report(api, *, version: str, data_dir: Path, startup_name: str) -> str:
    controller = getattr(api, "controller", None)
    logs = [str(line) for line in _snapshot(getattr(api, "_logs", ()))]
    errors = [line for line in logs if "失败" in line or "错误" in line or "异常" in line]
    lines = [
        f"吉星派对 Mod 助手错误报告 · v{version}",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        _safe("系统", platform.platform),
        _safe("Python", lambda: sys.version.split()[0] + ("（发布包）" if getattr(sys, "frozen", False) else "（源码运行）")),
        _safe("助手目录", lambda: Path(data_dir).parent),
        "",
        "【游戏连接】",
        _safe("已连接", lambda: bool(getattr(controller, "has_game", False))),
        _safe("游戏程序", lambda: getattr(controller, "game_executable", None) or "未检测到"),
        _safe("检测方式", lambda: getattr(controller, "game_location_mode", "auto")),
        _safe("检测说明", lambda: getattr(controller, "game_detection_note", "") or "无"),
        _safe("资源目录", lambda: "；".join(str(path) for path in getattr(controller, "aa_dirs", ()) or ()) or "无"),
        "",
        "【作品集】",
        _safe("名称", lambda: getattr(controller, "draft_name", "")),
        _safe("作品数量", lambda: ", ".join(f"{kind} × {count}" for kind, count in sorted(Counter(
            str(item.get("kind")) for item in _snapshot(getattr(controller, "draft_items", ())) if isinstance(item, dict)
        ).items())) or "空"),
        "",
    ]
    watch = getattr(api, "_startup_watch", None)
    if watch is not None:
        lines.append("【启动仍在进行】")
        lines.append(_safe("已等待", lambda: f"{watch.elapsed():.1f} 秒"))
        lines.extend(f"  {name}: {seconds:.2f} 秒" for name, seconds in _snapshot(watch.phases))
        lines.append("")
    startup_file = Path(data_dir) / startup_name
    if startup_file.is_file():
        try:
            text = startup_file.read_text(encoding="utf-8", errors="replace")[-MAX_STARTUP_DIAGNOSTICS_CHARS:]
        except OSError as exc:
            text = f"读取失败：{exc}"
        lines += ["【上次启动诊断】", text.rstrip(), ""]
    lines.append(f"【最近的错误】（{len(errors)} 条）")
    lines.extend(errors[-50:] or ["（无）"])
    lines += ["", f"【运行日志】（{len(logs)} 条）"]
    lines.extend(logs or ["（暂无日志）"])
    lines += ["", "【各线程当前位置】"]
    lines.extend(_thread_stacks())
    return "\n".join(lines) + "\n"
