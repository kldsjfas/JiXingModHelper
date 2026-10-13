"""Native MovieClip records are checked without writing a game or bundle."""
from __future__ import annotations

import struct
import unittest
from pathlib import Path

from astral_party_auto.modkit import fairygui_movieclip as movieclip
from astral_party_auto.modkit.fairygui_text import FairyGUIError


def u16(value: int) -> bytes:
    return struct.pack(">H", value)


def u32(value: int) -> bytes:
    return struct.pack(">I", value)


def text(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return u16(len(encoded)) + encoded


def indexed(blocks: list[bytes], short: bool = False) -> bytes:
    offset = 2 + len(blocks) * (2 if short else 4)
    offsets = []
    for block in blocks:
        offsets.append(offset if block else 0)
        offset += len(block)
    pack = u16 if short else u32
    return bytes((len(blocks), int(short))) + b"".join(pack(value) for value in offsets) + b"".join(blocks)


def package_fixture(*, short=False, branches=False) -> bytes:
    strings = ["official", "Official", "atlas0", "atlas0.png", "unused duplicate", "unused duplicate", "mobile"]
    no_tail = u16(65533) + b"\x00\x00"
    image = b"\x00" + u16(0) + u16(1) + u16(65533) * 2 + b"\x01" + u32(8) * 2 + b"\x00\x01" + no_tail
    atlas = b"\x04" + u16(2) + u16(65533) * 2 + u16(3) + b"\x00" + u32(32) * 2 + no_tail
    # A nonempty opaque hit-test block must survive a rewrite byte for byte.
    sprite = u16(0) + u16(2) + u32(0) * 2 + u32(8) * 2 + b"\x00\x00"
    blocks = [u16(0) + (u16(1) + u16(6) if branches else u16(0)),
              u16(2) + u32(len(image)) + image + u32(len(atlas)) + atlas,
              u16(1) + u16(len(sprite)) + sprite, b"\x12\x34\x56\x78",
              u32(len(strings)) + b"".join(text(value) for value in strings), b""]
    return b"FGUI" + u32(6) + b"\x00" + text("test1234") + text("Common") + bytes(20) + indexed(blocks, short)


def append(raw: bytes, **overrides) -> bytes:
    options = {"item_id": "jm0123456789ab", "atlas_id": "atlas0", "width": 8, "height": 8,
               "frames": [{"rect": [0, 0, 8, 8], "atlas_rect": [0, 32, 8, 8], "duration_ms": 30},
                          {"rect": [0, 0, 8, 8], "atlas_rect": [8, 32, 8, 8], "duration_ms": 90}],
               "atlas_width": 32, "atlas_height": 40}
    return movieclip.append_movieclip(raw, **(options | overrides))


class MovieClipWriterTests(unittest.TestCase):
    def test_append_has_exact_associations_and_variable_frame_timing(self):
        info = movieclip.inspect_movieclip_package(append(package_fixture()))
        self.assertEqual(info["package_id"], "test1234")
        self.assertEqual(info["package_name"], "Common")
        self.assertEqual((len(info["items"]), len(info["sprites"])), (3, 3))
        clip = info["movieclips"][0]
        self.assertEqual((clip["id"], clip["width"], clip["height"]), ("jm0123456789ab", 8, 8))
        self.assertEqual(clip["interval_ms"], 30)
        self.assertEqual([frame["add_delay_ms"] for frame in clip["frames"]], [0, 60])
        self.assertEqual([frame["duration_ms"] for frame in clip["frames"]], [30, 90])
        self.assertEqual([frame["rect"] for frame in clip["frames"]], [[0, 0, 8, 8]] * 2)
        self.assertEqual(info["sprites"][1]["rect"], [0, 32, 8, 8])
        self.assertEqual(info["atlases"][0]["file"], "atlas0.png")
        self.assertEqual(info["atlases"][0]["height"], 40)

    def test_original_records_and_duplicate_strings_survive_append(self):
        before = movieclip._parse(package_fixture())
        after = movieclip._parse(append(package_fixture()))
        self.assertEqual(after.header, before.header)
        self.assertEqual(after.blocks[0], before.blocks[0])
        self.assertEqual(after.blocks[3], before.blocks[3])
        self.assertEqual(after.strings[:len(before.strings)], before.strings)
        self.assertTrue(after.string_records.startswith(before.string_records))
        self.assertEqual(after.items[0]["_record"], before.items[0]["_record"])
        self.assertEqual(after.sprites[0]["_record"], before.sprites[0]["_record"])
        self.assertEqual(after.items[1]["_record"][:10], before.items[1]["_record"][:10])
        self.assertEqual(after.items[1]["_record"][18:], before.items[1]["_record"][18:])

    def test_short_offset_package_is_rebuilt_safely(self):
        info = movieclip.inspect_movieclip_package(append(package_fixture(short=True)))
        self.assertEqual(len(info["movieclips"]), 1)

    def test_package_branch_table_is_preserved(self):
        raw = package_fixture(branches=True)
        after = movieclip._parse(append(raw))
        self.assertEqual(after.blocks[0], movieclip._parse(raw).blocks[0])
        self.assertTrue(after.has_branches)
        self.assertEqual(after.items[-1]["branches"], [])

    def test_remove_keeps_enlarged_atlas_and_unused_strings(self):
        raw = package_fixture()
        added = append(raw)
        removed = movieclip.remove_movieclip(added, item_id="jm0123456789ab")
        info = movieclip.inspect_movieclip_package(removed)
        self.assertEqual(info["movieclips"], [])
        self.assertEqual([sprite["id"] for sprite in info["sprites"]], ["official"])
        self.assertEqual(info["atlases"][0]["height"], 40)
        self.assertEqual(movieclip._parse(removed).strings, movieclip._parse(added).strings)
        self.assertEqual(movieclip._parse(removed).items[0]["_record"], movieclip._parse(raw).items[0]["_record"])
        self.assertEqual(movieclip.remove_movieclip(removed, item_id="jm0123456789ab"), removed)

    def test_remove_then_append_does_not_accumulate_strings(self):
        added = append(package_fixture())
        removed = movieclip.remove_movieclip(added, item_id="jm0123456789ab")
        replaced = append(removed)
        self.assertEqual(replaced, added)

    def test_multiple_clips_do_not_remove_each_others_sprites(self):
        first = append(package_fixture())
        second = append(first, item_id="jmsecond")
        removed = movieclip.remove_movieclip(second, item_id="jm0123456789ab")
        info = movieclip.inspect_movieclip_package(removed)
        self.assertEqual([clip["id"] for clip in info["movieclips"]], ["jmsecond"])
        self.assertEqual([sprite["id"] for sprite in info["sprites"]], ["official", "jmsecond_0", "jmsecond_1"])

    def test_can_use_trimmed_frame_without_resizing_its_pixels(self):
        written = append(package_fixture(), frames=[{"rect": [2, 1, 4, 5], "atlas_rect": [0, 32, 4, 5], "duration_ms": 100}])
        self.assertEqual(movieclip.inspect_movieclip_package(written)["movieclips"][0]["frames"][0]["rect"], [2, 1, 4, 5])

    def test_rejects_duplicate_and_official_ids(self):
        for raw, item_id in ((package_fixture(), "official"), (append(package_fixture()), "jm0123456789ab")):
            with self.subTest(item_id=item_id), self.assertRaises(FairyGUIError):
                append(raw, item_id=item_id)
        for item_id in ("official", "jm", "jm../../run.exe", "jm\x00abc", 123, "jm" + "a" * 49):
            with self.subTest(item_id=item_id), self.assertRaises(FairyGUIError):
                append(package_fixture(), item_id=item_id)

    def test_rejects_shrunk_or_missing_atlas(self):
        for changes in ({"atlas_width": 31}, {"atlas_height": 31}, {"atlas_id": "missing"}, {"atlas_width": 4097}):
            with self.subTest(changes=changes), self.assertRaises(FairyGUIError):
                append(package_fixture(), **changes)

    def test_rejects_invalid_dimensions_and_frame_counts(self):
        for changes in ({"width": 0}, {"height": True}, {"width": 8.0}, {"frames": []}, {"frames": "frames"},
                        {"frames": [{"duration_ms": 100}] * 513}):
            with self.subTest(changes=changes), self.assertRaises(FairyGUIError):
                append(package_fixture(), **changes)

    def test_rejects_invalid_timing(self):
        for duration in (None, 0, -1, True, 30.1, 60001):
            with self.subTest(duration=duration), self.assertRaises(FairyGUIError):
                append(package_fixture(), frames=[{"rect": [0, 0, 8, 8], "atlas_rect": [0, 32, 8, 8], "duration_ms": duration}])

    def test_rejects_bad_rects_and_pretended_pixel_scaling(self):
        cases = [{"rect": [0, 0, 9, 8]}, {"rect": [0, 0, 0, 8]}, {"rect": [0, 0, 8]}, {"rect": [-1, 0, 8, 8]},
                 {"rect": [False, 0, 8, 8]}, {"atlas_rect": [31, 32, 8, 8]}, {"atlas_rect": [0, 32, 4, 4]},
                 {"atlas_rect": "0,32,8,8"}]
        for changes in cases:
            frame = {"rect": [0, 0, 8, 8], "atlas_rect": [0, 32, 8, 8], "duration_ms": 100} | changes
            with self.subTest(changes=changes), self.assertRaises(FairyGUIError):
                append(package_fixture(), frames=[frame])

    def test_official_and_unknown_owned_resources_cannot_be_removed(self):
        raw = package_fixture()
        with self.assertRaises(FairyGUIError):
            movieclip.remove_movieclip(raw, item_id="official")
        package = movieclip._parse(append(raw))
        records = [item["_record"] for item in package.items]
        records[-1] = records[-1][:3] + u16(1) + records[-1][5:]  # official display name
        altered = movieclip._write(package, records, [sprite["_record"] for sprite in package.sprites])
        with self.assertRaises(FairyGUIError):
            movieclip.remove_movieclip(altered, item_id="jm0123456789ab")

    def test_shared_sprite_prevents_removal(self):
        raw = append(append(package_fixture()), item_id="jmsecond")
        package = movieclip._parse(raw)
        records = [item["_record"] for item in package.items]
        first_index = package.strings.index("jm0123456789ab_0")
        second_index = package.strings.index("jmsecond_0")
        # The second clip now references one frame of the first.
        records[-1] = records[-1].replace(u16(second_index), u16(first_index), 1)
        altered = movieclip._write(package, records, [sprite["_record"] for sprite in package.sprites])
        with self.assertRaises(FairyGUIError):
            movieclip.remove_movieclip(altered, item_id="jm0123456789ab")

    def test_unsupported_header_extensions_and_truncations_are_rejected(self):
        raw = package_fixture()
        package = movieclip._parse(raw)
        extended_blocks = package.blocks.copy()
        extended_blocks[5] = b"extension"
        malformed = [raw[:7], raw[:7] + b"\x07" + raw[8:], raw[:8] + b"\x01" + raw[9:], raw[:-1],
                     package.header + indexed(extended_blocks)]
        for invalid in malformed:
            with self.subTest(length=len(invalid)), self.assertRaises(FairyGUIError):
                movieclip.inspect_movieclip_package(invalid)

    def test_overlap_and_out_of_bounds_block_offsets_are_rejected(self):
        raw = package_fixture()
        table = len(movieclip._parse(raw).header)
        for index, offset in ((0, 2), (1, 26), (2, len(raw) + 1)):
            altered = bytearray(raw)
            struct.pack_into(">I", altered, table + 2 + index * 4, offset)
            with self.subTest(index=index), self.assertRaises(FairyGUIError):
                movieclip.inspect_movieclip_package(bytes(altered))

    def test_malformed_record_size_and_sprite_reference_are_rejected(self):
        package = movieclip._parse(package_fixture())
        blocks = package.blocks.copy()
        for items, sprites in ((u16(2) + u32(0x7fffffff) + blocks[1][6:], blocks[2]),
                               (blocks[1], blocks[2][:6] + u16(65532) + blocks[2][8:])):
            altered = blocks.copy()
            altered[1], altered[2] = items, sprites
            with self.assertRaises(FairyGUIError):
                movieclip.inspect_movieclip_package(package.header + indexed(altered))


_REAL_FIXTURE = Path(__file__).resolve().parents[1] / "qa-evidence/static-dynamic-20261013/fixtures/Common_fui.bin"


@unittest.skipUnless(_REAL_FIXTURE.is_file(), "local game fixture is not distributed")
class ActualGamePackageTests(unittest.TestCase):
    def test_real_movieclip_layout_matches_verified_game_reader(self):
        info = movieclip.inspect_movieclip_package(_REAL_FIXTURE.read_bytes())
        self.assertEqual((info["version"], info["package_id"], info["package_name"]), (6, "xuaw6o8j", "Common"))
        self.assertEqual((len(info["items"]), len(info["sprites"])), (187, 129))
        original = info["movieclips"][0]
        self.assertEqual((original["id"], original["name"], original["width"], original["height"]), ("q18cj9i", "loading", 120, 120))
        self.assertEqual([frame["duration_ms"] for frame in original["frames"]], [333, 333])
        self.assertEqual(original["frames"][0]["rect"], [3, 2, 115, 115])

    def test_real_package_append_remove_preserves_all_original_records(self):
        raw = _REAL_FIXTURE.read_bytes()
        before = movieclip._parse(raw)
        frames = [{"rect": [0, 0, 64, 64], "atlas_rect": [index * 64, 1920, 64, 64], "duration_ms": duration}
                  for index, duration in enumerate((30, 80, 120))]
        added = append(raw, width=64, height=64, atlas_width=1920, atlas_height=1984, frames=frames)
        after = movieclip._parse(added)
        for old, new in zip(before.items, after.items):
            if old["type"] == 4:
                self.assertEqual(old["_record"][:10], new["_record"][:10])
                self.assertEqual(old["_record"][18:], new["_record"][18:])
            else:
                self.assertEqual(old["_record"], new["_record"])
        self.assertEqual([sprite["_record"] for sprite in before.sprites], [sprite["_record"] for sprite in after.sprites[:len(before.sprites)]])
        self.assertEqual(after.strings[:len(before.strings)], before.strings)
        removed = movieclip.remove_movieclip(added, item_id="jm0123456789ab")
        restored = movieclip._parse(removed)
        self.assertEqual(len(restored.items), len(before.items))
        self.assertEqual([sprite["_record"] for sprite in restored.sprites], [sprite["_record"] for sprite in before.sprites])
        self.assertEqual(restored.items[-2]["_record"], before.items[-2]["_record"])


if __name__ == "__main__":
    unittest.main()
