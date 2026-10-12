"""Encoder failures stay isolated; real FFmpeg tests decode generated pictures."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops, ImageStat

from astral_party_auto.modkit import cri_emote, usm_encoder
from test_cri_emote import make_usm


class UsmEncoderTests(unittest.TestCase):
    def setUp(self):
        self.reference = make_usm(frames=2)
        self.frames = [Image.new("RGBA", (160, 160), (200, 80, 40, 0)),
                       Image.new("RGBA", (160, 160), (40, 80, 200, 255))]

    def test_input_dimensions_mode_and_count_are_checked_before_encoding(self):
        invalid = [[], [Image.new("RGBA", (80, 80))], [Image.new("RGB", (160, 160))], [None]]
        with patch.object(usm_encoder, "_encode_stream") as encoder:
            for frames in invalid:
                with self.subTest(frames=len(frames)), self.assertRaises(ValueError):
                    usm_encoder.encode_emote_frames(frames, self.reference, "missing-ffmpeg")
            encoder.assert_not_called()

    def test_bad_reference_and_missing_encoder_do_not_create_work_files(self):
        with tempfile.TemporaryDirectory() as folder:
            for reference in (b"GIF89a", self.reference):
                with self.assertRaises(ValueError):
                    usm_encoder.encode_emote_frames(self.frames, reference, "missing-ffmpeg", work_dir=folder)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_pixel_and_duration_limits_do_not_launch_process(self):
        with patch.object(usm_encoder, "_encode_stream") as encoder:
            for frames in ([self.frames[0]] * 3601, [self.frames[0]] * 2501):
                with self.assertRaises(ValueError):
                    usm_encoder.encode_emote_frames(frames, self.reference, "missing-ffmpeg")
            encoder.assert_not_called()

    def test_encoder_failure_removes_temporary_raw_files(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "ffmpeg.exe"
            executable.write_bytes(b"isolated substitute")
            result = subprocess.CompletedProcess([], 1, stderr=b"deliberate encoding failure")
            with patch.object(usm_encoder.subprocess, "run", return_value=result):
                with self.assertRaisesRegex(ValueError, "deliberate"):
                    usm_encoder.encode_emote_frames(self.frames, self.reference, executable, work_dir=folder)
            self.assertEqual([path.name for path in Path(folder).iterdir()], ["ffmpeg.exe"])

    def test_picture_count_and_inter_frames_are_rejected(self):
        picture = b"\0\0\1\0" + b"\0\x08" + b"picture"
        with self.assertRaises(ValueError):
            usm_encoder._picture_packets(picture, 2)
        with self.assertRaises(ValueError):
            usm_encoder._picture_packets(picture.replace(b"\0\x08", b"\0\x10"), 1)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is optional on this machine")
    def test_real_dual_stream_encoding_decodes_alpha_and_changed_color(self):
        ffmpeg = shutil.which("ffmpeg")
        pattern = self.frames + [Image.new("RGBA", (160, 160), (80, 160, 40, 64)),
                                 Image.new("RGBA", (160, 160), (80, 40, 160, 128))]
        frames = pattern * 9
        with tempfile.TemporaryDirectory() as folder:
            raw = usm_encoder.encode_emote_frames(frames, self.reference, ffmpeg, work_dir=folder)
            info = cri_emote.inspect_usm_bytes(raw)
            self.assertEqual((info["frames"], info["width"], info["height"]), (36, 160, 160))
            self.assertEqual(info["duration_ms"], 1200)
            self.assertNotEqual(info["sha256"], cri_emote.inspect_usm_bytes(self.reference)["sha256"])
            chunks = list(cri_emote._chunks(raw))
            decoded = {}
            for tag in (b"@SFV", b"@ALP"):
                stream = b"".join(bytes(chunk.payload) for chunk in chunks if chunk.tag == tag and chunk.kind == 0)
                result = subprocess.run([ffmpeg, "-v", "error", "-f", "mpegvideo", "-i", "pipe:0",
                                         "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                                        input=stream, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                size = 160 * 160 * 3
                self.assertEqual(len(result.stdout), 36 * size)
                decoded[tag] = [Image.frombytes("RGB", (160, 160), result.stdout[index * size:(index + 1) * size])
                                for index in range(36)]
            self.assertLessEqual(max(decoded[b"@ALP"][0].getextrema()[0]), 2)
            self.assertGreaterEqual(min(decoded[b"@ALP"][1].getextrema()[0]), 253)
            for index in range(36):
                error = ImageStat.Stat(ImageChops.difference(decoded[b"@SFV"][index], frames[index].convert("RGB")))
                self.assertLess(max(error.mean), 5)
                alpha = decoded[b"@ALP"][index].getchannel("R")
                alpha_error = ImageStat.Stat(ImageChops.difference(alpha, frames[index].getchannel("A")))
                self.assertLess(alpha_error.mean[0], 3)
            for tag in (b"@SFV", b"@ALP"):
                data = [chunk for chunk in chunks if chunk.tag == tag and chunk.kind == 0]
                index = next(chunk for chunk in chunks if chunk.tag == tag and chunk.kind == 3)
                _, rows = cri_emote._utf_table(index.payload)
                self.assertEqual([row["ofs_frmid"] for row in rows], [0, 16, 32])
                self.assertEqual([row["ofs_byte"] for row in rows], [data[number].offset for number in (0, 16, 32)])
                self.assertEqual([chunk.time for chunk in data], [number * 100 for number in range(36)])
                first = bytes(data[0].payload)
                sequence = first.index(b"\0\0\1\xb3")
                self.assertEqual(first[sequence + 7] & 15, 5)
            self.assertEqual(list(Path(folder).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
