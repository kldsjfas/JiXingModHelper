"""Audio API regressions use temporary fake resources, never a live game."""
from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import time
import unittest
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
from wsgiref.util import setup_testing_defaults

from astral_party_auto.modkit.audio_workspace import AudioWorkspace
from astral_party_auto.web_app import DesktopApi, _build_server


def write_wave(path: Path) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\x01\x00" * 800)


class AudioApiTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="jixing-audio-api-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.game = self.root / "game"
        self.game.mkdir()
        self.original = self.game / "shared.bundle"
        self.original.write_bytes(b"original-bank")
        self.controller = SimpleNamespace(
            aa_dirs=(self.game,), log=Mock(),
            original_bundle_path=lambda _name: self.original,
        )
        self.workspace = AudioWorkspace(self.controller, self.root / "data", self.root / "made")
        self.workspace._rows = {
            name: {"id": name, "bundle": "shared.bundle", "object_id": "17", "media_id": number,
                   "name": name, "editable": True}
            for name, number in (("first", 101), ("second", 202))
        }
        self.wav = self.root / "sample.wav"
        write_wave(self.wav)
        self.api = object.__new__(DesktopApi)
        self.api._audio_workspace = self.workspace
        self.api._controller_lock = threading.RLock()
        self.api._append_log = Mock()
        self.app = _build_server(self.api, "test-session-token")

    def test_decoder_installed_next_to_executable_is_available(self):
        installed = self.root / "app" / "tools" / "audio" / "vgmstream" / "vgmstream-cli.exe"
        installed.parent.mkdir(parents=True)
        installed.write_bytes(b"test-decoder")
        with patch("astral_party_auto.modkit.audio_workspace.APP_ROOT", self.root / "app"), \
             patch("astral_party_auto.modkit.audio_workspace.RESOURCE_ROOT", self.root / "internal"):
            self.assertEqual(self.workspace._decoder(), installed)

    def test_missing_decoder_explains_download_package_setup(self):
        with patch("astral_party_auto.modkit.audio_workspace.APP_ROOT", self.root / "app"), \
             patch("astral_party_auto.modkit.audio_workspace.RESOURCE_ROOT", self.root / "internal"):
            with self.assertRaisesRegex(RuntimeError, "获取音频解码组件.cmd"):
                self.workspace._decoder()

    def request(self, path, *, method="GET", token=None, origin=None, body=None, headers=None):
        environment = {}
        setup_testing_defaults(environment)
        environment.update(REQUEST_METHOD=method, PATH_INFO=path, HTTP_HOST="127.0.0.1:12345")
        if token is not None:
            environment["HTTP_X_JIXING_TOKEN"] = token
        if origin is not None:
            environment["HTTP_ORIGIN"] = origin
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            environment.update(CONTENT_TYPE="application/json", CONTENT_LENGTH=str(len(payload)),
                               **{"wsgi.input": io.BytesIO(payload)})
        environment.update(headers or {})
        result = {}

        def start_response(status, response_headers, _exc_info=None):
            result.update(status=int(status.split()[0]), headers=dict(response_headers))

        chunks = self.app(environment, start_response)
        try:
            result["body"] = b"".join(chunks)
        finally:
            if hasattr(chunks, "close"):
                chunks.close()
        return result

    def test_audio_stream_requires_session_and_rejects_cross_origin(self):
        url = self.workspace.media_info(self.wav, "sample")["url"]
        for token, origin in ((None, None), ("wrong", None), ("test-session-token", "https://other.example")):
            with self.subTest(token=token, origin=origin):
                result = self.request(url, token=token, origin=origin)
                self.assertEqual(result["status"], 403)
                self.assertFalse(json.loads(result["body"])["ok"])

    def test_audio_stream_supports_authenticated_range_requests(self):
        url = self.workspace.media_info(self.wav, "sample")["url"]
        result = self.request(url, token="test-session-token", origin="http://127.0.0.1:12345",
                              headers={"HTTP_RANGE": "bytes=0-15"})
        self.assertEqual(result["status"], 206)
        self.assertEqual(result["body"], self.wav.read_bytes()[:16])
        self.assertEqual(result["headers"]["Content-Type"], "audio/wav")

    def test_unknown_audio_handle_cannot_be_used_as_a_file_path(self):
        result = self.request("/audio-media/not-a-ticket", token="test-session-token")
        self.assertEqual(result["status"], 404)
        self.assertNotIn(b"original-bank", result["body"])

    def test_commit_rejects_a_ticket_bound_to_another_resource(self):
        payload = self.workspace.root / "candidate.wem"
        payload.write_bytes(b"candidate")
        self.workspace._tickets["first-ticket"] = {
            "id": "first", "path": payload, "name": "first.wem",
            "original_hash": hashlib.sha256(self.original.read_bytes()).hexdigest(),
        }
        with patch("astral_party_auto.modkit.audio_bank.replace_bank_media") as replace:
            result = self.request("/api/audio_commit", method="POST", token="test-session-token",
                                  body=["second", "first-ticket"])
        self.assertEqual(result["status"], 200)
        self.assertFalse(json.loads(result["body"])["ok"])
        replace.assert_not_called()
        self.assertEqual(self.original.read_bytes(), b"original-bank")
        self.assertTrue(payload.exists())
        self.assertEqual(self.workspace.items, [])

    def test_invalid_candidate_does_not_create_a_ticket_or_draft(self):
        invalid = self.root / "renamed-mp3.wem"
        invalid.write_bytes(b"ID3not-a-Wwise-file")
        with patch("astral_party_auto.modkit.audio_bank.replace_bank_media", side_effect=ValueError("invalid WEM")):
            with self.assertRaisesRegex(ValueError, "invalid WEM"):
                self.workspace.prepare("first", invalid)
        self.assertEqual(self.workspace._tickets, {})
        self.assertEqual(list(self.workspace.draft_dir.iterdir()), [])
        self.assertEqual(self.original.read_bytes(), b"original-bank")

    def test_source_change_during_validation_does_not_issue_a_ticket(self):
        candidate = self.root / "candidate.wem"
        candidate.write_bytes(b"candidate")

        def replace(_source, _object, _media, _raw, checked, **_kwargs):
            checked.write_bytes(b"validated-bank")
            self.original.write_bytes(b"game-updated")

        with patch("astral_party_auto.modkit.audio_bank.replace_bank_media", side_effect=replace), \
                patch.object(self.workspace, "_decode", return_value=self.wav):
            with self.assertRaisesRegex(RuntimeError, "资源发生变化"):
                self.workspace.prepare("first", candidate)
        self.assertEqual(self.workspace._tickets, {})
        self.assertFalse(list(self.workspace.root.glob("candidate-*")))

    def test_exports_cannot_overwrite_game_backup_or_draft(self):
        backup = self.workspace.root.parent / "backups" / "original.wem"
        draft = self.workspace.draft_dir / "saved.wem"
        for output in (self.original, self.game / "sound.wem", backup, draft):
            with self.subTest(output=output), patch.object(self.workspace, "_bytes") as extract:
                with self.assertRaisesRegex(ValueError, "不能覆盖"):
                    self.workspace.export_audio("first", "original", "wem", output)
                extract.assert_not_called()
        self.assertEqual(self.original.read_bytes(), b"original-bank")

    def test_export_outside_resource_directories_is_complete(self):
        output = self.root / "export.wem"
        output.write_bytes(b"older-export")
        with patch.object(self.workspace, "_bytes", return_value=b"complete-audio"):
            self.workspace.export_audio("first", "original", "wem", output)
        self.assertEqual(output.read_bytes(), b"complete-audio")
        self.assertEqual(self.original.read_bytes(), b"original-bank")
        self.assertFalse(list(self.root.glob(".audio-export-*")))

    def test_concurrent_preview_requests_share_a_completed_decode(self):
        def decode(arguments, **_kwargs):
            time.sleep(0.03)
            write_wave(Path(arguments[arguments.index("-o") + 1]))
            return SimpleNamespace(returncode=0)

        with patch.object(self.workspace, "_decoder", return_value=Path("fake-decoder.exe")), \
                patch("astral_party_auto.modkit.audio_workspace.subprocess.run", side_effect=decode) as run:
            with ThreadPoolExecutor(max_workers=4) as pool:
                outputs = list(pool.map(self.workspace._decode, [b"same-audio"] * 4))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(len(set(outputs)), 1)
        self.assertEqual(outputs[0].read_bytes(), self.wav.read_bytes())
        self.assertFalse(list(self.workspace.cache_dir.glob("*.part.wav")))
        self.assertFalse(list(self.workspace.cache_dir.glob("*.wem")))

    def test_clear_recovers_stale_multiple_items_without_touching_game(self):
        self.workspace.items = [{**row, "original_hash": "outdated"}
                                for row in self.workspace._rows.values()]
        draft = self.workspace.draft_dir / "shared.bundle"
        draft.write_bytes(b"old-draft")
        backup = self.workspace.root.parent / "backups" / "shared.bundle"
        backup.parent.mkdir()
        backup.write_bytes(b"backup")
        with self.assertRaises(RuntimeError):
            self.workspace.remove("first")
        result = self.request("/api/audio_clear_draft", method="POST", token="test-session-token", body=[])
        self.assertTrue(json.loads(result["body"])["ok"])
        self.assertEqual(self.workspace.items, [])
        self.assertFalse(draft.exists())
        self.assertEqual(self.original.read_bytes(), b"original-bank")
        self.assertEqual(backup.read_bytes(), b"backup")
        self.assertEqual(json.loads((self.workspace.root / "draft.json").read_text()), [])

    def test_unrecorded_bundle_is_never_exported_or_installed(self):
        self.workspace.items = [{**self.workspace._rows["first"],
                                 "original_hash": hashlib.sha256(self.original.read_bytes()).hexdigest()}]
        (self.workspace.draft_dir / "shared.bundle").write_bytes(b"known-draft")
        (self.workspace.draft_dir / "forgotten.bundle").write_bytes(b"leftover")
        self.controller.install = Mock()
        for operation in (self.workspace.export_pack, self.workspace.install):
            with self.assertRaisesRegex(RuntimeError, "未记录"):
                operation()
        self.controller.install.assert_not_called()
        self.workspace.clear_draft()
        self.assertFalse(list(self.workspace.draft_dir.glob("*.bundle")))

    def test_cache_eviction_expires_old_handles_and_keeps_recent_audio(self):
        oldest = self.workspace.cache_dir / "old.wav"
        recent = self.workspace.cache_dir / "recent.wav"
        current = self.workspace.cache_dir / "current.wav"
        for timestamp, path in enumerate((oldest, recent, current), start=1):
            write_wave(path)
            os.utime(path, (timestamp, timestamp))
        handle = self.workspace.media_info(oldest, "old")["url"].rsplit("/", 1)[-1]
        with patch.object(self.workspace, "CACHE_LIMIT", oldest.stat().st_size * 2):
            self.workspace._prune_cache(current)
        self.assertFalse(oldest.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(current.exists())
        self.assertIsNone(self.workspace.media_path(handle))

    def test_identical_audio_copies_have_one_stable_catalog_entry(self):
        self.controller.has_game = True
        source = {**self.workspace._rows["first"], "bank_name": "VOICES", "language": "Japanese",
                  "category": "voice", "events": [], "sha256": "same-media"}
        copy = {**source, "id": "copy", "bundle": "z-copy.bundle", "object_id": "31"}
        self.workspace._rows = {"copy": copy, "first": source}
        with patch.object(self.workspace, "_start_scan"):
            catalog = self.workspace.catalog()
        self.assertEqual(catalog["total"], 1)
        self.assertEqual(catalog["items"][0]["id"], "first")
        self.assertEqual(catalog["items"][0]["copy_count"], 2)
        self.assertEqual(self.workspace.row("copy")["id"], "first")

    def test_replacement_waits_until_all_copies_are_discovered(self):
        self.workspace._status["scanning"] = True
        with self.assertRaisesRegex(RuntimeError, "扫描完成"):
            self.workspace.prepare("first", self.root / "not-read-yet.wem")

    def test_catalog_refreshes_when_an_audio_bundle_changes_under_the_same_source_key(self):
        self.controller.has_game = True

        def scan(path):
            return [{"object_id": 17, "name": "SFX", "language": "SFX", "event_names": [],
                     "media": [{"media_id": 101, "codec": "Wwise Vorbis", "editable": True,
                                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]}]

        def wait_for_scan():
            deadline = time.monotonic() + 2
            while self.workspace._status["scanning"] and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertFalse(self.workspace._status["scanning"])
            self.assertNotIn("error", self.workspace._status)

        with patch("astral_party_auto.modkit.audio_workspace.bundle_source_key", return_value="same-catalog"), \
                patch("astral_party_auto.modkit.audio_bank.list_audio_banks", side_effect=scan) as scanned:
            self.workspace.catalog()
            wait_for_scan()
            first_hash = self.workspace.catalog()["items"][0]["sha256"]
            self.original.write_bytes(b"updated-audio-bank-with-new-size")
            self.workspace.catalog()
            wait_for_scan()
            new_hash = self.workspace.catalog()["items"][0]["sha256"]
        self.assertNotEqual(first_hash, new_hash)
        self.assertEqual(scanned.call_count, 2)

    def test_prepare_rejects_media_updated_since_the_catalog_was_built(self):
        from test_audio_bank import make_bank, wem
        from astral_party_auto.modkit.audio_bank import replace_bank_bytes

        indexed_audio = wem(size=32)
        self.workspace._rows["first"]["sha256"] = hashlib.sha256(indexed_audio).hexdigest()
        self.original.write_bytes(make_bank(first=wem(size=48)))
        candidate = self.root / "replacement.wem"
        candidate.write_bytes(wem(size=64))

        def replace(source, _object, media_id, raw, checked, **kwargs):
            bank, _entry = replace_bank_bytes(source.read_bytes(), media_id, raw, **kwargs)
            checked.write_bytes(bank)

        with patch("astral_party_auto.modkit.audio_bank.replace_bank_media", side_effect=replace), \
                patch.object(self.workspace, "_decode", return_value=self.wav):
            with self.assertRaisesRegex(ValueError, "音频已发生变化"):
                self.workspace.prepare("first", candidate)
        self.assertEqual(self.workspace._tickets, {})
        self.assertFalse(list(self.workspace.root.glob("candidate-*")))


if __name__ == "__main__":
    unittest.main()
