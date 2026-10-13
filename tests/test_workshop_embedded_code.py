"""Reject executable content inside resources before submission or installation."""
import io
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from astral_party_auto.modkit import workshop, workshop_security as security
from tests.test_workshop import catalog, entry, metadata, pack_bytes
from tests.workshop_fixtures import assembly_catalog, pe_payload, resource_bundle, serialized_file, text_asset, unityfs


def inspect_bundle(data, **kwargs):
    return security.inspect_resource_bundle(io.BytesIO(data), len(data), **kwargs)


def directory_offset(data):
    cursor = data.index(b"\0") + 5
    cursor = data.index(b"\0", cursor) + 1
    cursor = data.index(b"\0", cursor) + 1
    return (cursor + 20 + 15) // 16 * 16


class EmbeddedCodeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.zip"

    def tearDown(self):
        self.temporary.cleanup()

    def package(self, data, *, name="texture.bundle"):
        self.source.write_bytes(pack_bytes([(name, data)], include_info=False))
        return self.source

    def test_normal_text_and_texture_tables_pass_supported_serialized_versions(self):
        for version in range(17, 23):
            data = serialized_file([(49, text_asset(b"fairygui-package", "Common_fui")), (28, b"texture pixels")],
                                   version=version)
            with self.subTest(version=version):
                result = inspect_bundle(unityfs([("CAB-test", data, 4)]))
                self.assertEqual(result["object_count"], 2)
                self.assertEqual(result["text_asset_count"], 1)

    def test_supported_block_and_directory_compression_is_inspected(self):
        for compression in (0, 1, 2, 3):
            for at_end in (False, True):
                with self.subTest(compression=compression, at_end=at_end):
                    data = unityfs(compression=compression, metadata_compression=compression, at_end=at_end)
                    self.assertEqual(inspect_bundle(data)["object_count"], 1)

    def test_named_assembly_textassets_are_rejected_without_pe_signature(self):
        for name in ("AstralParty.Runtime.dll", "plugin.exe", "plugin.DLL.bytes", "symbols.pdb", "plugin.mdb",
                     "AstralParty.Runtime", "plugin.dll ", "plugin.dll."):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "程序集"):
                inspect_bundle(resource_bundle(b"harmless test bytes", name=name))

    def test_renamed_embedded_pe_is_rejected_in_every_compression(self):
        for compression in (0, 1, 2, 3):
            with self.subTest(compression=compression), self.assertRaisesRegex(ValueError, "PE"):
                inspect_bundle(resource_bundle(pe_payload(), name="background", compression=compression))

    def test_pe_in_non_text_object_and_raw_resource_stream_is_rejected(self):
        variants = [unityfs([("CAB-test", serialized_file([(28, pe_payload())]), 4)]),
                    unityfs([("CAB-test", serialized_file(), 4), ("CAB-test.resS", pe_payload(), 0)]),
                    unityfs([("CAB-test.resource", pe_payload(), 0)])]
        for data in variants:
            with self.subTest(size=len(data)), self.assertRaisesRegex(ValueError, "PE"):
                inspect_bundle(data)

    def test_pe_across_scan_chunk_boundary_and_unreferenced_bytes_is_rejected(self):
        # PE starts at the final byte of one read; the stream node is not an object.
        data = unityfs([("CAB-test.resS", b"\0" * 65535 + pe_payload(), 0)])
        with self.assertRaisesRegex(ValueError, "PE"):
            inspect_bundle(data)

    def test_non_executable_mz_pixels_are_not_treated_as_an_assembly(self):
        data = resource_bundle(b"MZ" + b"\0" * 80)
        self.assertEqual(inspect_bundle(data)["text_asset_count"], 1)

    def test_overlapping_dos_and_pe_headers_and_obfuscated_names_are_rejected(self):
        tiny = bytearray(b"MZ" + b"\0" * 100)
        tiny[4:8] = b"PE\0\0"
        struct.pack_into("<I", tiny, 0x3C, 4)
        with self.assertRaisesRegex(ValueError, "PE"):
            inspect_bundle(resource_bundle(tiny))
        with self.assertRaisesRegex(ValueError, "控制字符"):
            inspect_bundle(resource_bundle(name="plugin.dll\0"))

    def test_malformed_inner_headers_and_unknown_versions_fail_closed(self):
        variants = [b"UnityFS\0", resource_bundle()[:-1]]
        unknown_version = bytearray(serialized_file())
        struct.pack_into(">I", unknown_version, 8, 23)
        variants.append(unityfs([("CAB-test", unknown_version, 4)]))
        unknown_bundle = bytearray(resource_bundle())
        struct.pack_into(">I", unknown_bundle, 8, 9)
        variants.append(bytes(unknown_bundle))
        bad_endian = bytearray(serialized_file())
        bad_endian[16] = 2
        variants.append(unityfs([("CAB-test", bad_endian, 4)]))
        for data in variants:
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                inspect_bundle(data)

    def test_encryption_unknown_block_flags_and_compression_fail_closed(self):
        original = resource_bundle()
        directory = directory_offset(original)
        field_offset = original.index(b"\0", original.index(b"\0", 12) + 1) + 1
        encrypted = bytearray(original)
        struct.pack_into(">I", encrypted, field_offset + 16, 0x440)
        unknown_flags = bytearray(original)
        struct.pack_into(">H", unknown_flags, directory + 28, 0x80)
        unknown_compression = bytearray(original)
        struct.pack_into(">H", unknown_compression, directory + 28, 4)
        for data in (encrypted, unknown_flags, unknown_compression):
            with self.assertRaises(ValueError):
                inspect_bundle(data)

    def test_inner_size_count_and_object_limits_are_applied_before_expansion(self):
        for constant, maximum in (("MAX_INNER_BYTES", 20), ("MAX_INNER_METADATA", 20),
                                  ("MAX_INNER_OBJECTS", 0), ("MAX_INNER_OBJECT_BYTES", 4),
                                  ("MAX_TEXT_ASSET_BYTES", 4), ("MAX_INNER_NODES", 0), ("MAX_INNER_BLOCKS", 0)):
            with self.subTest(constant=constant), patch.object(security, constant, maximum), self.assertRaises(ValueError):
                inspect_bundle(resource_bundle())
        with self.assertRaises(ValueError):
            inspect_bundle(resource_bundle(), max_expanded_bytes=20)
        with patch.object(workshop, "MAX_INNER_PACKAGE_BYTES", len(serialized_file())), self.assertRaises(ValueError):
            self.source.write_bytes(pack_bytes([("one.bundle", resource_bundle()), ("two.bundle", resource_bundle())]))
            workshop.inspect_package(self.source)
        with patch.object(workshop, "MAX_INNER_PACKAGE_OBJECTS", 1), self.assertRaises(ValueError):
            workshop.inspect_package(self.source)

    def test_object_count_out_of_bounds_overlap_and_truncation_are_rejected(self):
        original = serialized_file([(49, text_asset()), (49, text_asset())])
        first_record = 96
        changes = [(91, "<i", 100_001), (first_record + 16, "<I", 128 * 1024 * 1024 + 1),
                   (first_record + 8, "<q", -1), (first_record + 24 + 8, "<q", 0),
                   (first_record + 24, "<q", 1)]
        for offset, kind, value in changes:
            mutated = bytearray(original)
            struct.pack_into(kind, mutated, offset, value)
            with self.subTest(offset=offset, value=value), self.assertRaises(ValueError):
                inspect_bundle(unityfs([("CAB-test", mutated, 4)]))

    def test_textasset_extra_layout_and_declared_payload_size_fail_closed(self):
        variants = [text_asset() + b"extra", struct.pack("<i", 4097),
                    struct.pack("<i", 1) + b"x\0\0\0" + struct.pack("<i", 90)]
        for data in variants:
            with self.assertRaises(ValueError):
                inspect_bundle(unityfs([("CAB-test", serialized_file([(49, data)]), 4)]))

    def test_unknown_inner_files_names_node_flags_and_overlaps_fail_closed(self):
        variants = [unityfs([("unknown", b"not serialized", 0)]),
                    unityfs([("plugin.dll", b"not executable", 0)]),
                    unityfs([("stream.resS", b"pixels", 1)]),
                    unityfs([("CAB-test", serialized_file(), 4), ("cab-TEST", serialized_file(), 4)])]
        overlapped = bytearray(unityfs([("a.resS", b"pixels", 0), ("b.resS", b"pixels", 0)]))
        # The second directory node begins after the first record and its name.
        second_node = directory_offset(overlapped) + 34 + 20 + len(b"a.resS\0")
        struct.pack_into(">q", overlapped, second_node, 1)
        variants.append(overlapped)
        for data in variants:
            with self.assertRaises(ValueError):
                inspect_bundle(data)

    def test_lzma_dictionary_and_declared_expansion_bomb_are_rejected(self):
        compressed = bytearray(resource_bundle(compression=1))
        directory = directory_offset(compressed)
        node_end = directory + 34 + 20 + len(b"CAB-test\0")
        struct.pack_into("<I", compressed, node_end + 1, 64 * 1024 * 1024)
        with self.assertRaisesRegex(ValueError, "内存"):
            inspect_bundle(compressed)
        bomb = bytearray(resource_bundle())
        struct.pack_into(">I", bomb, directory_offset(bomb) + 20, 256 * 1024 * 1024 + 1)
        with self.assertRaises(ValueError):
            inspect_bundle(bomb)

    def test_timeout_and_excessive_pe_candidates_fail_closed(self):
        with patch.object(security.time, "monotonic", side_effect=[0, 31]), self.assertRaisesRegex(ValueError, "超时"):
            inspect_bundle(resource_bundle())
        with self.assertRaisesRegex(ValueError, "候选"):
            inspect_bundle(unityfs([("stream.resS", b"MZ" * 8193, 0)]))

    def test_catalog_follows_assembly_dependency_and_protects_target_name(self):
        directory = self.root / "aa"
        directory.mkdir()
        (directory / "catalog.json").write_text(json.dumps(assembly_catalog()), encoding="utf-8")
        blocked = security.forbidden_workshop_bundle_names([directory])
        self.assertEqual(blocked, {"runtime.bundle"})
        for address in ("runtime.bundle", "nested/RUNTIME.bundle", "nested\\runtime.bundle"):
            with self.subTest(address=address), self.assertRaisesRegex(ValueError, "覆盖"):
                security.reject_assembly_bundle_targets([address], blocked)
        self.package(resource_bundle(), name="nested/runtime.bundle")
        with self.assertRaisesRegex(ValueError, "覆盖"):
            workshop.inspect_package(self.source, forbidden_bundle_names=blocked)
        with self.assertRaisesRegex(ValueError, "覆盖"):
            workshop.prepare_submission(self.source, metadata(), self.root / "submission.zip", forbidden_bundle_names=blocked)

    def test_missing_malformed_catalog_and_dependency_budget_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "catalog"):
            security.forbidden_workshop_bundle_names([self.root])
        for value in ([], {"m_InternalIds": []}, {**assembly_catalog(), "m_KeyDataString": "invalid-base64!"}):
            with self.subTest(value=type(value)), self.assertRaises(ValueError):
                security._assembly_catalog_targets(value)
        with patch.object(security, "MAX_CATALOG_REFERENCES", 1), self.assertRaises(ValueError):
            security._assembly_catalog_targets(assembly_catalog())

    def test_submission_review_and_download_share_embedded_pe_rejection(self):
        data = pack_bytes([("safe-name.bundle", resource_bundle(pe_payload()))], include_info=False)
        self.source.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "PE"):
            workshop.prepare_submission(self.source, metadata(), self.root / "submission.zip")
        from tools import workshop_review
        with self.assertRaisesRegex(ValueError, "PE"):
            workshop_review.inspect_package(self.source)
        self.assertFalse((self.root / "submission.zip").exists())
        client = workshop.WorkshopClient(self.root)
        value = entry(data)
        def download(url, output, limit, *, expected_size=None):
            payload = json.dumps(catalog([value])).encode() if url == workshop.CATALOG_URL else data
            output.write(payload)
            return len(payload)
        with patch.object(workshop, "_download", side_effect=download), self.assertRaisesRegex(ValueError, "PE"):
            client.prepare(value["id"], value["asset"]["sha256"])
        self.assertEqual(list(client.work_dir.iterdir()), [])

    def test_prepared_install_rechecks_current_catalog_target_names(self):
        data = pack_bytes([("runtime.bundle", resource_bundle())], include_info=False)
        value = entry(data)
        client = workshop.WorkshopClient(self.root)
        def download(url, output, limit, *, expected_size=None):
            payload = json.dumps(catalog([value])).encode() if url == workshop.CATALOG_URL else data
            output.write(payload)
            return len(payload)
        with patch.object(workshop, "_download", side_effect=download):
            prepared = client.prepare(value["id"], value["asset"]["sha256"])
        client.verify_prepared(prepared)
        with self.assertRaisesRegex(ValueError, "覆盖"):
            client.verify_prepared(prepared, forbidden_bundle_names={"runtime.bundle"})
        self.assertTrue(client.discard(prepared)["removed"])


if __name__ == "__main__":
    unittest.main()
