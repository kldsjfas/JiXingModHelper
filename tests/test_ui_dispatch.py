"""文件选择调度区分取消和超时，过期请求不再打开或复用窗口结果。"""
from __future__ import annotations

import threading
import unittest
from collections import deque
from unittest.mock import Mock, patch

from astral_party_auto import web_app


class FakeTkRoot:
    def __init__(self):
        self.callbacks = deque()

    def after(self, delay, callback):
        if delay != 0:
            raise AssertionError("UI callbacks should be scheduled immediately")
        self.callbacks.append(callback)

    def run_next(self):
        self.callbacks.popleft()()

    def run_all(self):
        while self.callbacks:
            self.run_next()


class FakeUiEvent:
    def __init__(self, on_wait):
        self.on_wait = on_wait
        self.completed = False
        self.wait_timeouts = []

    def set(self):
        self.completed = True

    def wait(self, timeout):
        self.wait_timeouts.append(timeout)
        return self.on_wait(self)


class UiDispatchTests(unittest.TestCase):
    def setUp(self):
        self.api = object.__new__(web_app.DesktopApi)
        self.root = FakeTkRoot()
        self.api.set_tk_root(self.root)

    def complete_queued(self, event):
        self.root.run_all()
        return event.completed

    def call_with_event(self, func, on_wait=None):
        event = FakeUiEvent(on_wait or self.complete_queued)
        with patch.object(web_app.threading, "Event", return_value=event):
            result = self.api._run_on_ui(func)
        self.assertEqual(event.wait_timeouts, [180])
        return result

    def test_without_tk_root_runs_directly(self):
        self.api.set_tk_root(None)
        callback = Mock(return_value="direct selection")
        with patch.object(web_app.threading, "Event") as event_factory:
            self.assertEqual(self.api._run_on_ui(callback), "direct selection")
        callback.assert_called_once_with()
        event_factory.assert_not_called()

    def test_completed_selection_returns_this_request_value(self):
        callback = Mock(return_value="selected.gif")
        self.assertEqual(self.call_with_event(callback), "selected.gif")
        callback.assert_called_once_with()

    def test_completed_none_is_normal_cancel(self):
        self.assertIsNone(self.call_with_event(lambda: None))

    def test_callback_error_is_propagated(self):
        error = ValueError("dialog failed")
        with self.assertRaises(ValueError) as raised:
            self.call_with_event(Mock(side_effect=error))
        self.assertIs(raised.exception, error)

    def test_expired_queued_callback_does_not_run_and_next_request_is_fresh(self):
        stale_callback = Mock(return_value="stale.gif")
        timed_out = FakeUiEvent(lambda _event: False)
        fresh = FakeUiEvent(self.complete_queued)
        with patch.object(web_app.threading, "Event", side_effect=[timed_out, fresh]):
            with self.assertRaisesRegex(TimeoutError, "等待超时"):
                self.api._run_on_ui(stale_callback)
            self.assertEqual(self.api._run_on_ui(lambda: "fresh.gif"), "fresh.gif")
        stale_callback.assert_not_called()
        self.assertTrue(timed_out.completed)
        self.assertEqual(timed_out.wait_timeouts, [180])
        self.assertEqual(fresh.wait_timeouts, [180])

    def test_deadline_passed_before_dispatch_does_not_start_dialog(self):
        callback = Mock(return_value="too late.gif")
        with patch("time.monotonic", side_effect=[100, 280]):
            with self.assertRaisesRegex(TimeoutError, "等待超时"):
                self.call_with_event(callback)
        callback.assert_not_called()

    def test_false_wait_is_timeout_even_if_callback_just_completed(self):
        def complete_after_wait_expired(_event):
            self.root.run_all()
            return False

        for result in (None, "late.gif"):
            with self.subTest(result=result), self.assertRaises(TimeoutError):
                self.call_with_event(lambda: result, complete_after_wait_expired)

    def test_late_running_dialog_result_or_error_cannot_pollute_next_request(self):
        for fail in (False, True):
            with self.subTest(late_error=fail):
                dialog_started = threading.Event()
                release_dialog = threading.Event()
                worker_errors = []

                def old_dialog():
                    dialog_started.set()
                    if not release_dialog.wait(2):
                        raise AssertionError("test did not release the old dialog")
                    if fail:
                        raise ValueError("late dialog error")
                    return "late old selection.gif"

                def run_old_wrapper():
                    try:
                        self.root.run_next()
                    except BaseException as error:
                        worker_errors.append(error)

                # Build real thread barriers before patching the dispatch Event factory.
                worker = threading.Thread(target=run_old_wrapper)

                def timeout_running_dialog(_event):
                    worker.start()
                    self.assertTrue(dialog_started.wait(2), "old dialog did not start")
                    return False

                expired_event = FakeUiEvent(timeout_running_dialog)
                fresh_event = FakeUiEvent(self.complete_queued)
                cancel_event = FakeUiEvent(self.complete_queued)
                try:
                    with patch.object(web_app.threading, "Event", side_effect=[expired_event, fresh_event, cancel_event]):
                        with self.assertRaisesRegex(TimeoutError, "等待超时"):
                            self.api._run_on_ui(old_dialog)
                        self.assertEqual(self.api._run_on_ui(lambda: "fresh selection.gif"), "fresh selection.gif")
                        release_dialog.set()
                        worker.join(2)
                        self.assertFalse(worker.is_alive(), "old dialog callback did not finish")
                        self.assertIsNone(self.api._run_on_ui(lambda: None))
                finally:
                    release_dialog.set()
                    if worker.ident is not None:
                        worker.join(2)
                self.assertEqual(worker_errors, [])
                self.assertTrue(expired_event.completed)
                self.assertEqual(expired_event.wait_timeouts, [180])
                self.assertEqual(fresh_event.wait_timeouts, [180])
                self.assertEqual(cancel_event.wait_timeouts, [180])


if __name__ == "__main__":
    unittest.main()
