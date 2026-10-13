"""Archive attacks, pinned transport, freshness and immutable preparation."""
import hashlib
import io
import json
import os
import stat
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.request import Request

from astral_party_auto.modkit import workshop
from tests.workshop_fixtures import resource_bundle


def bundle(payload=b"resource-data"):
    return resource_bundle(payload)


def metadata():
    return {"id": "fenny-test", "name": "芬妮测试", "author": "作者",
            "description": "测试贴图", "category": "texture", "version": "1.0", "game_version": "2026-10"}


def pack_bytes(entries=None, *, info=None, compression=zipfile.ZIP_DEFLATED, include_info=True):
    entries = [("texture.bundle", bundle())] if entries is None else entries
    info = {"name": "作品", "items": [], "bundle_count": sum(name.endswith(".bundle") for name, _ in entries)} if info is None else info
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        if include_info:
            archive.writestr("mod_info.json", json.dumps(info).encode())
        for name, data in entries:
            archive.writestr(name, data)
    return output.getvalue()


def entry(payload, **changes):
    value = {**metadata(), "revoked": False, "asset": {"tag": "workshop-1", "name": "fenny.zip",
             "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}}
    value.update(changes)
    return value


def catalog(entries):
    return {"format": "jixing-workshop", "version": 1, "mods": entries}


class Response(io.BytesIO):
    def __init__(self, payload, url, headers=None):
        super().__init__(payload)
        self.url = url
        self.headers = headers or {}

    def geturl(self):
        return self.url


class WorkshopTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "source.zip"

    def tearDown(self):
        self.temporary.cleanup()

    def inspect(self, payload):
        self.path.write_bytes(payload)
        return workshop.inspect_package(self.path)

    def fetch(self, entries, payload):
        def download(url, output, limit, *, expected_size=None):
            content = json.dumps(catalog(entries)).encode() if url == workshop.CATALOG_URL else payload
            output.write(content)
            return len(content)
        return download

    def test_valid_pack_has_content_hashes_and_no_unitypy_parser(self):
        result = self.inspect(pack_bytes())
        self.assertEqual(result["bundle_count"], 1)
        self.assertEqual(result["file_count"], 2)
        self.assertEqual(result["sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(result["bundles"][0]["sha256"], hashlib.sha256(bundle()).hexdigest())
        self.assertIn("不代表无毒", result["risk_note"])

    def test_catalog_rejects_malformed_and_duplicate_fields(self):
        payload = pack_bytes()
        valid = catalog([entry(payload)])
        self.assertEqual(workshop.validate_catalog(valid)["mods"][0]["id"], "fenny-test")
        mutations = [lambda v: v.update(url="https://evil.invalid"), lambda v: v.update(version=True),
                     lambda v: v["mods"][0].update(url="https://evil.invalid"),
                     lambda v: v["mods"][0]["asset"].update(name="../bad.zip"),
                     lambda v: v["mods"][0]["asset"].update(tag="../tag"),
                     lambda v: v["mods"][0]["asset"].update(size=True),
                     lambda v: v["mods"][0]["asset"].update(sha256="F" * 64),
                     lambda v: v["mods"].append(v["mods"][0])]
        for mutate in mutations:
            value = json.loads(json.dumps(valid))
            mutate(value)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                workshop.validate_catalog(value)
        for malformed in (b'{"format":"jixing-workshop","format":"jixing-workshop","version":1,"mods":[]}',
                          b'{"format":"jixing-workshop","version":1,"mods":NaN}', b"[]"):
            with self.assertRaises(ValueError):
                workshop.validate_catalog(malformed)

    def test_pinned_download_url(self):
        value = entry(pack_bytes())
        self.assertEqual(workshop.asset_url(value), workshop.REPOSITORY_URL + "/releases/download/workshop-1/fenny.zip")
        for url in ("https://github.com/evil/repo/releases/download/tag/x.zip", "http://github.com/kldsjfas/JiXingModHelper/releases/download/tag/x.zip",
                    "https://user@github.com/kldsjfas/JiXingModHelper/releases/download/tag/x.zip",
                    "https://github.com:443/kldsjfas/JiXingModHelper/releases/download/tag/x.zip",
                    workshop.REPOSITORY_URL + "/releases/download/../x.zip", workshop.CATALOG_URL + "?x=1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                workshop._download(url, io.BytesIO(), 100)

    def test_redirects_only_go_to_release_assets_over_https(self):
        initial = workshop.asset_url(entry(pack_bytes()))
        redirects = workshop._TrustedRedirects(initial)
        request = Request(initial)
        allowed = "https://release-assets.githubusercontent.com/github-production-release-asset/1/file?sig=value"
        self.assertEqual(redirects.redirect_request(request, None, 302, "", {}, allowed).full_url, allowed)
        for url in ("https://evil.invalid/x", "https://github.com/evil/x", "http://release-assets.githubusercontent.com/file",
                    "https://u:p@release-assets.githubusercontent.com/file", "https://release-assets.githubusercontent.com:443/file",
                    "https://release-assets.githubusercontent.com/file#fragment", "https://release-assets.githubusercontent.com/fi\\le"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                redirects.redirect_request(request, None, 302, "", {}, url)
        with self.assertRaises(ValueError):
            workshop._TrustedRedirects(workshop.CATALOG_URL).redirect_request(Request(workshop.CATALOG_URL), None, 302, "", {}, allowed)

    def test_download_checks_length_stream_limit_and_response_encoding(self):
        url = workshop.asset_url(entry(pack_bytes()))
        opener = Mock()
        opener.open.return_value = Response(b"abc", url, {"Content-Length": "3"})
        with patch.object(workshop, "build_opener", return_value=opener):
            output = io.BytesIO()
            self.assertEqual(workshop._download(url, output, 10, expected_size=3), 3)
            self.assertEqual(output.getvalue(), b"abc")
            self.assertEqual(opener.open.call_args.kwargs["timeout"], 12)
        cases = [(b"abc", {"Content-Length": "4"}, 3), (b"abcd", {}, 3),
                 (b"ab", {}, 3), (b"abc", {"Content-Encoding": "gzip"}, 3),
                 (b"abc", {"Content-Length": "bad"}, 3)]
        for payload, headers, expected in cases:
            opener.open.return_value = Response(payload, url, headers)
            with patch.object(workshop, "build_opener", return_value=opener), self.assertRaises(ValueError):
                workshop._download(url, io.BytesIO(), 3, expected_size=expected)

    def test_forbidden_paths_extra_programs_and_duplicate_names(self):
        for name in ("../escape.bundle", "/absolute.bundle", "C:/absolute.bundle", "con.bundle",
                     "sub/NUL.bundle", "file.bundle:ads", "sub./file.bundle", "file.bundle ", "run.exe", "run.py",
                     "run.ps1", "run.js", "run.dll", "shortcut.lnk", "nested/mod_info.json"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.inspect(pack_bytes([(name, bundle())]))
        # On Windows ZipInfo normalizes backslashes when writing. Patch both
        # ZIP records to exercise an archive received from another writer.
        backslash = pack_bytes([("sub/file.bundle", bundle())]).replace(b"sub/file.bundle", b"sub\\file.bundle")
        with self.assertRaises(ValueError):
            self.inspect(backslash)
        for entries in ([('a.bundle', bundle()), ('A.bundle', bundle())],
                        [('one/a.bundle', bundle()), ('two/a.bundle', bundle())],
                        [('a.bundle', bundle()), ('a.bundle/child.bundle', bundle())]):
            with self.assertRaises(ValueError):
                self.inspect(pack_bytes(entries))

    def test_nul_filename_is_rejected_before_zipinfo_truncation(self):
        info = zipfile.ZipInfo("safe.bundle\0.exe")
        with self.assertRaises(ValueError):
            workshop._archive_path(info)

    def test_central_directory_is_bounded_before_zipfile_allocates_entries(self):
        payload = bytearray(pack_bytes())
        offset = payload.rfind(b"PK\x05\x06")
        struct.pack_into("<H", payload, offset + 8, 65535)
        struct.pack_into("<H", payload, offset + 10, 65535)
        with patch.object(workshop.zipfile, "ZipFile") as parser, self.assertRaises(ValueError):
            self.inspect(bytes(payload))
        parser.assert_not_called()
        for prefix in (b"MZ", b"external-content"):
            with self.assertRaises(ValueError):
                self.inspect(prefix + pack_bytes())

    def test_zip_links_encryption_unsupported_compression_and_bombs(self):
        for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR):
            info = zipfile.ZipInfo("a.bundle")
            info.external_attr = (mode | 0o777) << 16
            with self.assertRaises(ValueError):
                workshop._archive_path(info)
        info = zipfile.ZipInfo("a.bundle")
        info.flag_bits = 1
        with self.assertRaises(ValueError):
            workshop._archive_path(info)
        with self.assertRaises(ValueError):
            self.inspect(pack_bytes(compression=zipfile.ZIP_BZIP2))
        with self.assertRaises(ValueError):
            self.inspect(pack_bytes([("a.bundle", bundle(b"\0" * (2 * 1024 * 1024)))]))
        with patch.object(workshop, "MAX_FILES", 1), self.assertRaises(ValueError):
            self.inspect(pack_bytes())
        with patch.object(workshop, "MAX_EXPANDED_BYTES", 20), self.assertRaises(ValueError):
            self.inspect(pack_bytes())
        with patch.object(workshop, "MAX_BUNDLE_BYTES", 20), self.assertRaises(ValueError):
            self.inspect(pack_bytes())

    def test_metadata_duplicate_keys_bad_shape_and_header_fail_closed(self):
        for info in ({"name": "x", "items": "bad"}, {"name": "x", "items": [], "bundle_count": 9},
                     {"name": "x", "items": [], "program": "evil"}):
            with self.assertRaises(ValueError):
                self.inspect(pack_bytes(info=info))
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("mod_info.json", b'{"name":"x","items":[],"name":"y"}')
            archive.writestr("a.bundle", bundle())
        with self.assertRaises(ValueError):
            self.inspect(output.getvalue())
        for data in (b"not-a-bundle", b"UnityFS\0" + b"\0" * 80, bundle()[:-1]):
            with self.assertRaises(ValueError):
                self.inspect(pack_bytes([("a.bundle", data)]))

    def test_crc_corruption_is_rejected(self):
        payload = bytearray(pack_bytes(compression=zipfile.ZIP_STORED))
        index = payload.index(b"resource-data")
        payload[index] ^= 1
        with self.assertRaises(ValueError):
            self.inspect(bytes(payload))

    def test_emote_manifest_requires_root_declared_bundle_and_exact_size(self):
        name = "jixing_emote_120101008.bundle"
        data = bundle()
        manifest = {"format": "jixing-emote-addresses", "version": 1, "patches": [
            {"emote_id": 120101008, "video_name": "VEmoji_101_08_999", "bundle_name": name,
             "template_video_key": "VEmoji_101_09", "bundle_size": len(data)}]}
        for include_info in (True, False):
            with self.subTest(include_info=include_info):
                result = self.inspect(pack_bytes([(name, data), ("emote_addresses.json", json.dumps(manifest).encode())], include_info=include_info))
                self.assertEqual(len(result["emote_patches"]), 1)
                with self.assertRaises(ValueError):
                    self.inspect(pack_bytes([(name, data)], include_info=include_info))
                changed = json.loads(json.dumps(manifest))
                changed["patches"][0]["bundle_size"] += 1
                with self.assertRaises(ValueError):
                    self.inspect(pack_bytes([(name, data), ("emote_addresses.json", json.dumps(changed).encode())], include_info=include_info))

    def test_catalog_is_copied_cached_and_failed_refresh_removes_stale_items(self):
        payload = pack_bytes()
        client = workshop.WorkshopClient(self.root)
        with patch.object(workshop, "_download", side_effect=self.fetch([entry(payload)], payload)) as fetch:
            result = client.catalog()
            result["mods"].clear()
            self.assertEqual(len(client.catalog()["mods"]), 1)
            self.assertEqual(fetch.call_count, 1)
        with patch.object(workshop, "_download", side_effect=TimeoutError()):
            result = client.catalog(force=True)
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["mods"], [])

    def test_raw_network_failure_uses_only_fixed_github_api_and_discards_partial_bytes(self):
        client = workshop.WorkshopClient(self.root)
        calls = []
        def fetch(url, output, limit, **kwargs):
            calls.append(url)
            if url == workshop.CATALOG_URL:
                output.write(b"partial network response")
                raise TimeoutError()
            self.assertEqual(url, workshop.CATALOG_API_URL)
            output.write(b'{"format":"jixing-workshop","version":1,"mods":[]}')
        with patch.object(workshop, "_download", side_effect=fetch):
            result = client.catalog(force=True)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["source"], workshop.CATALOG_API_URL)
        self.assertEqual(calls, [workshop.CATALOG_URL, workshop.CATALOG_API_URL])

    def test_catalog_validation_or_redirect_failure_does_not_trigger_fallback(self):
        for response in (b'{"untrusted":true}', None):
            def fetch(url, output, limit, **kwargs):
                if response is None:
                    raise ValueError("Untrusted redirect")
                output.write(response)
            with patch.object(workshop, "_download", side_effect=fetch) as download:
                result = workshop.WorkshopClient(self.root).catalog(force=True)
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(download.call_count, 1)

    def test_prepare_always_refreshes_and_refuses_revocation_or_changed_hash(self):
        payload = pack_bytes()
        value = entry(payload)
        client = workshop.WorkshopClient(self.root)
        with patch.object(workshop, "_download", side_effect=self.fetch([value], payload)):
            client.catalog()
        for entries in ([entry(payload, revoked=True)], [], [entry(pack_bytes([("x.bundle", bundle())]))]):
            with patch.object(workshop, "_download", side_effect=self.fetch(entries, payload)), self.assertRaises(ValueError):
                client.prepare(value["id"], value["asset"]["sha256"])
        with self.assertRaises(ValueError):
            client.prepare(value["id"])

    def test_prepare_download_digest_mismatch_never_leaves_installable_content(self):
        payload = pack_bytes()
        value = entry(payload)
        value["asset"]["sha256"] = "a" * 64
        client = workshop.WorkshopClient(self.root)
        with patch.object(workshop, "_download", side_effect=self.fetch([value], payload)), self.assertRaises(ValueError):
            client.prepare(value["id"], value["asset"]["sha256"])
        self.assertEqual(list((self.root / "workshop").iterdir()), [])

    def test_prepared_snapshot_integrity_unknown_files_and_precise_cleanup(self):
        payload = pack_bytes([("nested/texture.bundle", bundle())])
        value = entry(payload)
        client = workshop.WorkshopClient(self.root)
        with patch.object(workshop, "_download", side_effect=self.fetch([value], payload)):
            prepared = client.prepare(value["id"], value["asset"]["sha256"])
        content = Path(prepared["mod_dir"])
        self.assertTrue((content / "texture.bundle").is_file())
        client.verify_prepared(prepared)
        path = content / "texture.bundle"
        original = path.read_bytes()
        path.write_bytes(original[:-1] + b"X")
        with self.assertRaises(ValueError):
            client.verify_prepared(prepared)
        self.assertFalse(client.discard(prepared)["removed"])
        path.write_bytes(original)
        extra = content / "unknown.txt"
        extra.write_text("unknown")
        with self.assertRaises(ValueError):
            client.verify_prepared(prepared)
        self.assertFalse(client.discard(prepared)["removed"])
        extra.unlink()
        package = Path(prepared["path"])
        original_zip = package.read_bytes()
        package.write_bytes(original_zip[:-1] + b"X")
        with self.assertRaises(ValueError):
            client.verify_prepared(prepared)
        self.assertFalse(client.discard(prepared)["removed"])
        package.write_bytes(original_zip)
        self.assertTrue(client.discard(prepared)["removed"])
        self.assertFalse(Path(prepared["path"]).exists())
        self.assertFalse(client.discard(prepared)["removed"])

    def test_submission_keeps_animation_reload_guard_without_private_history(self):
        from astral_party_auto.modkit.manager import ModManager

        self.path.write_bytes(pack_bytes(info={"name": "animation", "items": [
            {"kind": "visual_animation", "source": "C:/private/user/clip.gif", "bundle": "texture.bundle"},
            {"source": "D:/private/photo.png"}], "bundle_count": 1}))
        destination = self.root / "animation-submission.zip"
        result = workshop.prepare_submission(self.path, metadata(), destination)
        self.assertEqual(result["inspection"]["mod_info"]["items"], [{"kind": "visual_animation"}])
        installed = self.root / "installed-animation"
        installed.mkdir()
        with zipfile.ZipFile(destination) as archive:
            info = archive.read("mod_info.json")
            self.assertNotIn(b"private", info)
            (installed / "mod_info.json").write_bytes(info)
        self.assertTrue(ModManager.has_native_ui_animation(installed))

    def test_submission_rewrites_local_history_and_keeps_sidecar_outside_zip(self):
        payload = pack_bytes(info={"name": "original", "items": [{"source": "C:/private/user/photo.png", "original_path": "D:/secret"}], "bundle_count": 1})
        self.path.write_bytes(payload)
        destination = self.root / "submission.zip"
        result = workshop.prepare_submission(self.path, metadata(), destination)
        self.assertTrue(Path(result["path"]).samefile(destination))
        with zipfile.ZipFile(destination) as archive:
            self.assertEqual(set(archive.namelist()), {"texture.bundle", "mod_info.json"})
            info = json.loads(archive.read("mod_info.json"))
            self.assertEqual(info["items"], [])
            self.assertEqual(info["name"], metadata()["name"])
        report = Path(result["report_path"]).read_text(encoding="utf-8")
        self.assertNotIn("private", report)
        self.assertNotIn("D:/secret", report)
        self.assertNotIn(str(self.root), report)
        self.assertEqual(self.path.read_bytes(), payload)
        self.assertEqual(workshop.inspect_package(destination)["sha256"], result["report"]["asset"]["sha256"])
        with self.assertRaises(ValueError):
            workshop.prepare_submission(self.path, metadata(), destination)
        self.assertEqual(destination.read_bytes(), Path(result["path"]).read_bytes())

    def test_external_resource_pack_without_metadata_exports_normalized_submission(self):
        payload = pack_bytes([("OtherEditor/texture.bundle", bundle())], include_info=False)
        original = self.inspect(payload)
        self.assertIsNone(original["mod_info"])
        self.assertEqual(original["file_count"], 1)
        self.assertEqual(original["bundles"][0]["path"], "OtherEditor/texture.bundle")
        destination = self.root / "external-submission.zip"
        result = workshop.prepare_submission(self.path, metadata(), destination)
        with zipfile.ZipFile(destination) as archive:
            self.assertEqual(set(archive.namelist()), {"texture.bundle", "mod_info.json"})
            self.assertEqual(archive.read("texture.bundle"), bundle())
            info = json.loads(archive.read("mod_info.json"))
            self.assertEqual(info["name"], metadata()["name"])
            self.assertEqual(info["bundle_count"], 1)
            self.assertEqual(info["items"], [])
        checked = workshop.inspect_package(destination)
        self.assertEqual(checked["mod_info"]["name"], metadata()["name"])
        self.assertEqual(checked["sha256"], result["report"]["asset"]["sha256"])
        self.assertEqual(checked["bundles"][0]["sha256"], original["bundles"][0]["sha256"])
        self.assertEqual(result["report"]["review_status"], "pending")
        self.assertEqual(self.path.read_bytes(), payload)
        self.assertNotIn(str(self.root), Path(result["report_path"]).read_text(encoding="utf-8"))

    def test_metadata_from_another_editor_is_checked_without_tool_name_requirement(self):
        result = self.inspect(pack_bytes(info={"name": "外部作品", "tool": "Another Resource Editor", "items": [], "bundle_count": 1}))
        self.assertEqual(result["mod_info"]["name"], "外部作品")
        for info in ({"name": "外部作品", "tool": "Another Resource Editor", "items": "invalid"},
                     {"name": "外部作品", "items": [], "bundle_count": 2},
                     {"name": "外部作品", "items": [], "execute": "payload.dll"}):
            with self.subTest(info=info), self.assertRaises(ValueError):
                self.inspect(pack_bytes(info=info))

    def test_external_pack_still_rejects_plugins_scripts_paths_and_broken_resources(self):
        for name, data in (("plugin.dll", b"MZ"), ("run.ps1", b"Write-Output x"), ("run.js", b"alert(1)"),
                           ("README.md", b"external instructions"), ("../escape.bundle", bundle()),
                           ("bad.bundle", b"MZ-not-a-unity-resource")):
            payload = pack_bytes([("texture.bundle", bundle()), (name, data)], include_info=False)
            self.path.write_bytes(payload)
            destination = self.root / "rejected-submission.zip"
            with self.subTest(name=name), self.assertRaises(ValueError):
                workshop.prepare_submission(self.path, metadata(), destination)
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_suffix(".submission.json").exists())
            self.assertEqual(self.path.read_bytes(), payload)

    def test_reviewed_external_resource_pack_keeps_download_hash_and_snapshot_checks(self):
        payload = pack_bytes(include_info=False)
        value = entry(payload)
        client = workshop.WorkshopClient(self.root)
        with patch.object(workshop, "_download", side_effect=self.fetch([value], payload)):
            prepared = client.prepare(value["id"], value["asset"]["sha256"])
        self.assertIsNone(prepared["inspection"]["mod_info"])
        self.assertEqual({path.name for path in Path(prepared["mod_dir"]).iterdir()}, {"texture.bundle"})
        client.verify_prepared(prepared)
        self.assertTrue(client.discard(prepared)["removed"])

    def test_submission_publish_race_preserves_other_writers_file(self):
        self.path.write_bytes(pack_bytes())
        destination = self.root / "race.zip"
        def publish(source, target):
            Path(target).write_bytes(b"other-writer")
            raise FileExistsError("created by another writer")
        with patch.object(workshop.os, "link", side_effect=publish), self.assertRaises(FileExistsError):
            workshop.prepare_submission(self.path, metadata(), destination)
        self.assertEqual(destination.read_bytes(), b"other-writer")
        self.assertEqual(list(self.root.glob(".workshop-*.tmp")), [])

    def test_discard_missing_scan_quarantined_directory_does_not_mask_error(self):
        payload = pack_bytes()
        value = entry(payload)
        client = workshop.WorkshopClient(self.root)
        with patch.object(workshop, "_download", side_effect=self.fetch([value], payload)):
            prepared = client.prepare(value["id"], value["asset"]["sha256"])
        content = Path(prepared["mod_dir"])
        for file in content.iterdir():
            file.unlink()
        content.rmdir()
        self.assertFalse(client.discard(prepared)["removed"])
        self.assertTrue(Path(prepared["path"]).exists())

    def test_windows_reparse_points_and_symlinks_are_not_regular_inputs(self):
        for mode, attributes in ((stat.S_IFREG | 0o600, 0x400), (stat.S_IFLNK | 0o777, 0)):
            path = Mock()
            path.lstat.return_value = Mock(st_mode=mode, st_file_attributes=attributes)
            self.assertTrue(workshop._is_link(path))
        self.path.write_bytes(pack_bytes())
        with patch.object(workshop, "_is_link", return_value=True), self.assertRaises(ValueError):
            workshop.inspect_package(self.path)
        with patch.object(workshop, "_is_link", return_value=True), self.assertRaises(ValueError):
            workshop._ensure_directory(self.root / "cache")


if __name__ == "__main__":
    unittest.main()
