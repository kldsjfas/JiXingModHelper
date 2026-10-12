"""本地目录补丁保留官方hash，但索引必须随真实JSON内容失效。"""
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from astral_party_auto.modkit.bundles import bundle_source_key


class CatalogFingerprintTests(unittest.TestCase):
    def test_official_hash_and_timestamp_do_not_hide_changed_catalog_content(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            bundles = folder / "AssetBundles"
            bundles.mkdir()
            catalog = folder / "catalog_3.2.0.json"
            catalog.write_bytes(b'{"same_length":"a"}')
            (folder / "catalog_3.2.0.hash").write_text("official-version", encoding="ascii")
            stamp = catalog.stat()
            first = bundle_source_key((bundles,))
            catalog.write_bytes(b'{"same_length":"b"}')
            os.utime(catalog, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            second = bundle_source_key((bundles,))
            self.assertNotEqual(first, second)
            self.assertEqual((folder / "catalog_3.2.0.hash").read_text(), "official-version")
