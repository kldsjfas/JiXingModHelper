"""Dropped files use the same validators and cannot escape their editor session."""
import base64
import io
import json
from pathlib import Path
import threading
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from wsgiref.util import setup_testing_defaults

from PIL import Image

from astral_party_auto.modkit.file_import import MAX_IMPORT_REQUEST_BYTES
from astral_party_auto.web_app import DesktopApi, _build_server


class DroppedFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="jixing-file-import-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data_patch = patch("astral_party_auto.web_app.DATA_DIR", self.root)
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)
        self.api = object.__new__(DesktopApi)
        self.api._controller_lock = threading.RLock()
        self.api._append_log = Mock()
        self.api._replacement_path = None
        self.api._replacement_temporary = None
        self.api._image_cache = {}
        self.selection = {"asset_type": "texture", "bundle": "card.bundle", "name": "Card", "editable": True}
        self.api.controller = SimpleNamespace(selection=self.selection)
        self.addCleanup(self.api._clear_replacement)
        self.expected = {key: self.selection[key] for key in ("bundle", "name", "asset_type")}

    def image_bytes(self, color="red", animated=False):
        output = io.BytesIO()
        first = Image.new("RGBA", (4, 4), color)
        if animated:
            first.save(output, format="PNG", save_all=True,
                       append_images=[Image.new("RGBA", (4, 4), "blue")], duration=100, loop=0)
        else:
            first.save(output, format="PNG")
        return output.getvalue()

    def drop(self, payload=None, filename="替换图片.png", expected=None):
        encoded = base64.b64encode(self.image_bytes() if payload is None else payload).decode("ascii")
        return self.api.import_replacement_file(filename, encoded, self.expected if expected is None else expected)

    def test_import_stages_actual_bytes_and_preserves_display_filename(self):
        payload = self.image_bytes()
        result = self.drop(payload)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["data"]["name"], "替换图片.png")
        self.assertEqual(self.api._replacement_path.read_bytes(), payload)
        self.assertEqual(self.api._replacement_path.name, "replacement.png")
        self.assertTrue(self.api._replacement_path.is_relative_to(self.root / "dropped-replacements"))
        self.assertTrue(result["data"]["preview_data"].startswith("data:image/png;base64,"))

    def test_successful_second_drop_releases_only_previous_temporary(self):
        self.assertTrue(self.drop()["ok"])
        old = self.api._replacement_path
        self.assertTrue(self.drop(self.image_bytes("green"))["ok"])
        self.assertFalse(old.exists())
        self.assertTrue(self.api._replacement_path.exists())

    def test_bad_image_does_not_replace_or_remove_previous_candidate(self):
        self.assertTrue(self.drop()["ok"])
        old = self.api._replacement_path
        result = self.drop(b"MZ-not-an-image")
        self.assertFalse(result["ok"])
        self.assertIn("图片无法读取", result["error"])
        self.assertNotIn(str(self.root), result["error"])
        self.assertEqual(self.api._replacement_path, old)
        self.assertTrue(old.exists())
        self.assertEqual(len(list((self.root / "dropped-replacements").iterdir())), 1)

    def test_animated_image_is_not_silently_flattened(self):
        result = self.drop(self.image_bytes(animated=True))
        self.assertFalse(result["ok"])
        self.assertIn("多帧", result["error"])
        self.assertIsNone(self.api._replacement_path)

    def test_renamed_tiff_cannot_claim_to_be_a_browser_preview(self):
        output = io.BytesIO()
        Image.new("RGBA", (4, 4), "red").save(output, format="TIFF")
        result = self.drop(output.getvalue())
        self.assertFalse(result["ok"])
        self.assertIn("后缀不符", result["error"])
        self.assertIsNone(self.api._replacement_path)

    def test_occupied_temporary_is_reported_without_blocking_selection_cleanup(self):
        temporary = Mock(name="occupied", cleanup=Mock(side_effect=PermissionError("in use")))
        self.api._replacement_temporary = temporary
        self.api._replacement_path = self.root / "occupied.png"
        self.api._clear_replacement()
        self.assertIsNone(self.api._replacement_path)
        self.assertIsNone(self.api._replacement_temporary)
        temporary.cleanup.assert_called_once()
        self.api._append_log.assert_called_once()

    def test_stale_selection_is_rejected_before_staging(self):
        self.api.controller.selection = {**self.selection, "name": "Different"}
        result = self.drop()
        self.assertFalse(result["ok"])
        self.assertIn("已切换", result["error"])
        self.assertFalse((self.root / "dropped-replacements").exists())

    def test_missing_identity_or_noneditable_resource_is_rejected(self):
        self.assertFalse(self.drop(expected={})["ok"])
        self.selection["editable"] = False
        self.assertFalse(self.drop()["ok"])
        self.assertFalse((self.root / "dropped-replacements").exists())

    def test_paths_scripts_empty_and_malformed_payloads_are_rejected(self):
        for filename in ("../escape.png", "C:\\escape.png", "folder/file.png", "file.png:stream", "tool.exe", "x\0.png"):
            with self.subTest(filename=filename):
                self.assertFalse(self.drop(filename=filename)["ok"])
        for content in ("", "not base64!", "汉字", 12, None):
            with self.subTest(content=content):
                result = self.api.import_replacement_file("image.png", content, self.expected)
                self.assertFalse(result["ok"])
        self.assertFalse((self.root / "dropped-replacements").exists())

    def test_file_and_pixel_limits_are_checked_before_loading(self):
        with patch("astral_party_auto.modkit.file_import.MAX_DROPPED_FILE_BYTES", 4):
            self.assertFalse(self.drop()["ok"])
        with patch("astral_party_auto.modkit.file_import.MAX_DROPPED_IMAGE_PIXELS", 15):
            result = self.drop()
            self.assertFalse(result["ok"])
            self.assertIn("尺寸过大", result["error"])
        self.assertIsNone(self.api._replacement_path)

    def test_text_uses_existing_encoding_validator(self):
        self.selection["asset_type"] = self.expected["asset_type"] = "text"
        result = self.drop("中文文本".encode("utf-8"), "说明.txt")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["data"]["text_content"], "中文文本")

    def test_clip_uses_existing_validation_and_other_entry_is_untouched(self):
        self.selection["asset_type"] = self.expected["asset_type"] = "anim"
        self.api._clip_candidate_preview = Mock(side_effect=ValueError("动画格式不匹配"))
        result = self.drop(b"not-a-clip", "animation.animbin")
        self.assertFalse(result["ok"])
        self.assertIn("动画格式不匹配", result["error"])
        self.api._clip_candidate_preview.assert_called_once()
        self.assertIsNone(self.api._replacement_path)

    def test_clearing_import_never_removes_original_picked_file(self):
        original = self.root / "original.png"
        original.write_bytes(self.image_bytes())
        self.api._replacement_path = original
        self.assertTrue(self.drop()["ok"])
        staged = self.api._replacement_path
        self.api._clear_replacement()
        self.assertFalse(staged.exists())
        self.assertTrue(original.exists())

    def request(self, args, *, headers=None):
        payload = json.dumps(args, ensure_ascii=False).encode("utf-8")
        environment = {}
        setup_testing_defaults(environment)
        environment.update(REQUEST_METHOD="POST", PATH_INFO="/api/import_replacement_file",
                           HTTP_HOST="127.0.0.1:12345", HTTP_X_JIXING_TOKEN="file-test-token",
                           CONTENT_TYPE="application/json", CONTENT_LENGTH=str(len(payload)),
                           **{"wsgi.input": io.BytesIO(payload)})
        environment.update(headers or {})
        response = {}

        def start_response(status, _headers, _exc_info=None):
            response["status"] = int(status.split()[0])

        chunks = _build_server(self.api, "file-test-token")(environment, start_response)
        try:
            response["data"] = json.loads(b"".join(chunks).decode("utf-8"))
        finally:
            if hasattr(chunks, "close"):
                chunks.close()
        return response

    def test_real_route_accepts_files_above_bottle_default_json_limit(self):
        self.selection["asset_type"] = self.expected["asset_type"] = "text"
        text = "large text\n" * 20000
        result = self.request(["large.txt", base64.b64encode(text.encode()).decode(), self.expected])
        self.assertEqual(result["status"], 200)
        self.assertTrue(result["data"]["ok"], result["data"].get("error"))
        self.assertEqual(result["data"]["data"]["text_content"], text)

    def test_http_limit_authorization_origin_and_content_type(self):
        args = ["sample.png", base64.b64encode(self.image_bytes()).decode(), self.expected]
        for headers, status in (
            ({"HTTP_X_JIXING_TOKEN": "wrong"}, 403),
            ({"HTTP_ORIGIN": "https://example.org"}, 403),
            ({"CONTENT_TYPE": "text/plain"}, 415),
            ({"CONTENT_LENGTH": str(MAX_IMPORT_REQUEST_BYTES + 1)}, 413),
            ({"CONTENT_LENGTH": "-1"}, 411),
            ({"HTTP_TRANSFER_ENCODING": "chunked"}, 411),
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(args, headers=headers)["status"], status)
        self.assertIsNone(self.api._replacement_path)


if __name__ == "__main__":
    unittest.main()
