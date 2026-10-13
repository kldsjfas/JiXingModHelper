"""维护工具不会把结构校验结果自动变成上架批准。"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from test_workshop import metadata, pack_bytes
from tools.workshop_review import build_entry, _read_metadata


class WorkshopReviewTests(unittest.TestCase):
    def test_candidate_stays_revoked_and_binds_the_actual_package(self):
        with TemporaryDirectory() as folder:
            package = Path(folder) / "example.zip"
            package.write_bytes(pack_bytes())
            entry = build_entry(package, metadata(), "workshop-example-v1")
            self.assertTrue(entry["revoked"])
            self.assertEqual(entry["asset"]["size"], package.stat().st_size)
            self.assertEqual(entry["asset"]["name"], "example.zip")
            self.assertEqual(len(entry["asset"]["sha256"]), 64)

    def test_sidecar_metadata_is_supported_and_duplicate_fields_rejected(self):
        with TemporaryDirectory() as folder:
            report = Path(folder) / "report.json"
            report.write_text(json.dumps({"metadata": metadata()}), encoding="utf-8")
            self.assertEqual(_read_metadata(report), metadata())
            report.write_text('{"metadata":{},"metadata":{}}', encoding="utf-8")
            with self.assertRaises(ValueError):
                _read_metadata(report)


if __name__ == "__main__":
    unittest.main()
