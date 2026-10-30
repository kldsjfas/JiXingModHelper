"""Unity 的公司/产品身份与 Windows 已知目录定位回归。"""
from __future__ import annotations

import os
import ctypes
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from astral_party_auto.modkit.bundles import bundle_dirs_for_exe, hot_update_dir_for_exe
from astral_party_auto.modkit.unity_paths import local_low_dir, read_unity_identity, unity_catalog_roots_for_exe


class UnityIdentityPathTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="jixing-unity-paths-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile = self.root / "test-profile"

    def installation(self, executable_name, company, product):
        executable = self.root / executable_name
        executable.write_bytes(b"")
        data = executable.parent / f"{executable.stem}_Data"
        data.mkdir(exist_ok=True)
        (data / "app.info").write_text(f"{company}\n{product}\n", encoding="utf-8")
        return executable, data

    def known_folder_api(self, directory, *, result=0):
        path_buffer = ctypes.create_unicode_buffer(str(directory))

        def query(_folder_id, flags, _token, output):
            if flags != 0x00004000:
                return ctypes.c_long(0x80070003).value
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.cast(path_buffer, ctypes.c_void_p)
            return result

        query_folder = Mock(side_effect=query)
        free_path = Mock()
        libraries = {"shell32": SimpleNamespace(SHGetKnownFolderPath=query_folder),
                     "ole32": SimpleNamespace(CoTaskMemFree=free_path)}
        load_library = patch("astral_party_auto.modkit.unity_paths.ctypes.WinDLL",
                             side_effect=lambda name, **_kwargs: libraries[name])
        return load_library, query_folder, free_path

    def test_renamed_executable_keeps_the_product_cache_identity(self):
        executable, data = self.installation("MyLauncherName.exe", "feimo", "AstralParty_CN")
        expected = self.profile / "AppData/LocalLow/feimo/AstralParty_CN/com.unity.addressables"
        self.assertEqual(read_unity_identity(executable), ("feimo", "AstralParty_CN"))
        self.assertEqual(hot_update_dir_for_exe(executable, user_profile=self.profile), expected / "AssetBundles")
        self.assertEqual(unity_catalog_roots_for_exe(executable, user_profile=self.profile),
                         (data / "com.unity.addressables", expected))

    def test_company_and_product_are_both_read_from_this_installation(self):
        executable, _ = self.installation("AstralParty_CN.exe", "Another Studio", "星趴国际版")
        expected = self.profile / "AppData/LocalLow/Another Studio/星趴国际版/com.unity.addressables/AssetBundles"
        self.assertEqual(hot_update_dir_for_exe(executable, user_profile=self.profile), expected)

    def test_system_known_folder_takes_priority_over_userprofile_environment(self):
        executable, _ = self.installation("client.exe", "feimo", "AstralParty_CN")
        redirected = self.root / "Redirected LocalLow"
        with patch.dict(os.environ, {"USERPROFILE": str(self.root / "wrong-profile")}), \
                patch("astral_party_auto.modkit.unity_paths._local_low_dir_from_windows", return_value=redirected) as known_folder:
            actual = hot_update_dir_for_exe(executable)
        known_folder.assert_called_once_with()
        self.assertEqual(actual, redirected / "feimo/AstralParty_CN/com.unity.addressables/AssetBundles")

    def test_cn_and_international_clients_do_not_share_guessed_identity(self):
        cn, _ = self.installation("CN-launcher.exe", "feimo", "AstralParty_CN")
        international, _ = self.installation("INT-launcher.exe", "feimo", "AstralParty")
        cn_cache = hot_update_dir_for_exe(cn, user_profile=self.profile)
        international_cache = hot_update_dir_for_exe(international, user_profile=self.profile)
        cn_cache.mkdir(parents=True)
        international_cache.mkdir(parents=True)
        self.assertEqual(bundle_dirs_for_exe(cn, user_profile=self.profile), (cn_cache,))
        self.assertEqual(bundle_dirs_for_exe(international, user_profile=self.profile), (international_cache,))

    def test_missing_app_info_does_not_guess_a_user_product_directory(self):
        executable = self.root / "AstralParty_CN.exe"
        data = self.root / "AstralParty_CN_Data"
        client_cache = data / "com.unity.addressables/AssetBundles"
        client_cache.mkdir(parents=True)
        guessed = self.profile / "AppData/LocalLow/feimo/AstralParty_CN/com.unity.addressables/AssetBundles"
        guessed.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "app.info"):
            hot_update_dir_for_exe(executable, user_profile=self.profile)
        with self.assertRaisesRegex(ValueError, "app.info"):
            unity_catalog_roots_for_exe(executable, user_profile=self.profile)
        self.assertEqual(bundle_dirs_for_exe(executable, user_profile=self.profile), (client_cache,))

    def test_invalid_identity_cannot_escape_local_low(self):
        invalid = ("", ".", "..", "../other", r"other\product", "C:other", "trailing.",
                   "trailing ", "CON", "NUL.txt", "name\x00suffix")
        executable, data = self.installation("client.exe", "feimo", "AstralParty_CN")
        for value in invalid:
            for position in (0, 1):
                with self.subTest(value=value, position=position):
                    identity = ["feimo", "AstralParty_CN"]
                    identity[position] = value
                    (data / "app.info").write_text("\n".join(identity), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        hot_update_dir_for_exe(executable, user_profile=self.profile)

    def test_app_info_accepts_utf8_bom_but_not_extra_records_or_oversize(self):
        executable, data = self.installation("client.exe", "feimo", "AstralParty_CN")
        info = data / "app.info"
        info.write_bytes(b"\xef\xbb\xbffeimo\r\nAstralParty_CN\r\n")
        self.assertEqual(read_unity_identity(executable), ("feimo", "AstralParty_CN"))
        for raw in (b"feimo\nAstralParty_CN\nother", b"x" * 8193, b"\xff\xfe\xff"):
            info.write_bytes(raw)
            with self.assertRaises(ValueError):
                read_unity_identity(executable)

    def test_known_folder_failure_does_not_fall_back_to_userprofile(self):
        executable, data = self.installation("client.exe", "feimo", "AstralParty_CN")
        base = data / "StreamingAssets/aa/StandaloneWindows64"
        base.mkdir(parents=True)
        with patch("astral_party_auto.modkit.unity_paths._local_low_dir_from_windows", side_effect=ValueError("known folder unavailable")):
            with self.assertRaisesRegex(ValueError, "known folder unavailable"):
                hot_update_dir_for_exe(executable)
            self.assertEqual(bundle_dirs_for_exe(executable), (base,))

    @unittest.skipUnless(os.name == "nt", "Windows Known Folder API")
    def test_known_folder_query_does_not_require_or_create_the_directory(self):
        missing = self.root / "Not created" / "LocalLow"
        load_library, query_folder, free_path = self.known_folder_api(missing)
        self.assertFalse(missing.exists())
        with load_library:
            self.assertEqual(local_low_dir(), missing)
        self.assertEqual(query_folder.call_args.args[1], 0x00004000)
        self.assertFalse(query_folder.call_args.args[1] & 0x00008000, "Must not request KF_FLAG_CREATE")
        free_path.assert_called_once()
        self.assertFalse(missing.exists())

    @unittest.skipUnless(os.name == "nt", "Windows Known Folder API")
    def test_hresult_failure_frees_returned_buffer_without_guessing_a_path(self):
        missing = self.root / "Unavailable LocalLow"
        load_library, _query_folder, free_path = self.known_folder_api(
            missing, result=ctypes.c_long(0x80070003).value)
        with load_library, patch.dict(os.environ, {"USERPROFILE": str(self.root / "wrong-profile")}):
            with self.assertRaisesRegex(ValueError, "HRESULT 0x80070003"):
                local_low_dir()
        free_path.assert_called_once()
        self.assertFalse(missing.exists())

    @unittest.skipUnless(os.name == "nt", "Windows Known Folder API")
    def test_real_windows_local_low_query_returns_an_absolute_directory(self):
        self.assertTrue(local_low_dir().is_absolute())


if __name__ == "__main__":
    unittest.main()
