"""真实动图时序/透明度、最终 USM 回解，以及失败不覆盖原文件的回归。"""
from __future__ import annotations

import base64
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from astral_party_auto.modkit import cri_emote, emote_media, usm_encoder
from test_cri_emote import make_usm


def decoded_frame(uri):
    with Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))) as image:
        return image.convert("RGBA")


def save_animation(path, *, durations=(100, 300), size=(80, 40), kind="GIF"):
    frames = [Image.new("RGBA", size, (220, 20, 20, 255)),
              Image.new("RGBA", size, (20, 20, 220, 255))]
    options = {"save_all": True, "append_images": frames[1:], "duration": durations, "loop": 0}
    if kind == "WEBP":
        options["lossless"] = True
    frames[0].save(path, format=kind, **options)


def save_video(folder, kind):
    options = {
        "mp4": ["-c:v", "mpeg4", "-pix_fmt", "yuv420p", "-f", "mp4"],
        "webm": ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-lossless", "1", "-auto-alt-ref", "0", "-f", "webm"],
        "mov": ["-c:v", "qtrle", "-pix_fmt", "argb", "-f", "mov"],
        "mkv": ["-c:v", "ffv1", "-pix_fmt", "bgra", "-f", "matroska"],
        "avi": ["-c:v", "rawvideo", "-pix_fmt", "bgr24", "-f", "avi"],
    }
    raw, source = Path(folder) / "fixture.rgba", Path(folder) / ("fixture." + kind)
    with raw.open("wb") as output:
        for index in range(6):
            frame = Image.new("RGBA", (96, 64), (220, 20, 20, 0 if kind in ("webm", "mov", "mkv") else 255))
            for y in range(16, 48):
                for x in range(24 + index, 64 + index):
                    frame.putpixel((x, y), (220, 20, 20, 255))
            output.write(frame.tobytes())
    subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-nostdin", "-y", "-threads", "1",
                    "-f", "rawvideo", "-pixel_format", "rgba", "-video_size", "96x64", "-framerate", "10",
                    "-i", str(raw), "-frames:v", "6", *options[kind], str(source)],
                   capture_output=True, check=True, timeout=20,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    raw.unlink()
    return source


class EmoteMediaTests(unittest.TestCase):
    def setUp(self):
        self.reference = {"width": 160, "height": 160, "fps": 10}

    def test_variable_source_timing_is_preserved_with_transparent_letterbox(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.gif"
            save_animation(path)
            frames, info = emote_media.adapt_animation_source(path, self.reference)
            self.assertEqual(len(frames), 4)
            self.assertEqual(info["source_duration_ms"], 400)
            self.assertEqual(info["duration_ms"], 400)
            self.assertEqual([frame.getpixel((80, 80))[:3] for frame in frames],
                             [(220, 20, 20)] + [(20, 20, 220)] * 3)
            self.assertTrue(all(frame.mode == "RGBA" and frame.size == (160, 160) for frame in frames))
            self.assertTrue(all(frame.getpixel((0, 0))[3] == 0 for frame in frames))

    def test_apng_and_webp_use_their_actual_frame_duration(self):
        with tempfile.TemporaryDirectory() as folder:
            for kind, suffix in (("PNG", ".png"), ("WEBP", ".webp")):
                path = Path(folder) / ("source" + suffix)
                save_animation(path, kind=kind)
                with self.subTest(kind=kind):
                    frames, info = emote_media.adapt_animation_source(path, self.reference)
                    self.assertEqual((len(frames), info["source_duration_ms"]), (4, 400))
                    self.assertGreater(frames[0].getpixel((80, 80))[0], 200)
                    self.assertGreater(frames[-1].getpixel((80, 80))[2], 200)

    def test_apng_default_cover_is_not_encoded_as_an_animation_frame(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cover.png"
            cover = Image.new("RGBA", (20, 20), "green")
            first = Image.new("RGBA", (20, 20), "red")
            second = Image.new("RGBA", (20, 20), "blue")
            cover.save(path, save_all=True, append_images=[first, second], default_image=True,
                       duration=[100, 300], loop=0)
            frames, info = emote_media.adapt_animation_source(path, self.reference)
            self.assertEqual(info["source_frames"], 2)
            self.assertEqual(frames[0].getpixel((80, 80))[:3], (255, 0, 0))
            self.assertEqual(frames[-1].getpixel((80, 80))[:3], (0, 0, 255))

    def test_static_broken_and_unsupported_inputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            static, broken, unsupported = [Path(folder) / name for name in ("static.png", "broken.gif", "image.jpg")]
            Image.new("RGBA", (10, 10)).save(static)
            broken.write_bytes(b"GIF89a damaged")
            Image.new("RGB", (10, 10)).save(unsupported)
            for source in (static, broken, unsupported):
                with self.subTest(source=source.name), self.assertRaises(ValueError):
                    emote_media.adapt_animation_source(source, self.reference)

    def test_content_detection_ignores_the_filename_extension(self):
        with tempfile.TemporaryDirectory() as folder:
            gif, usm = Path(folder) / "animation.mp4", Path(folder) / "native.gif"
            save_animation(gif)
            usm.write_bytes(make_usm())
            self.assertEqual(emote_media.detect_emote_media(gif), "gif")
            self.assertEqual(emote_media.detect_emote_media(usm), "usm")

    def test_source_and_output_budgets_reject_before_encoding(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.gif"
            save_animation(path)
            for constant, limit in (("MAX_SOURCE_BYTES", 8), ("MAX_SOURCE_FRAMES", 1),
                                    ("MAX_SOURCE_FRAME_PIXELS", 1), ("MAX_SOURCE_PIXELS", 1),
                                    ("MAX_SOURCE_DURATION_MS", 1), ("MAX_OUTPUT_PIXELS", 1)):
                with self.subTest(budget=constant), patch.object(emote_media, constant, limit), self.assertRaises(ValueError):
                    emote_media.adapt_animation_source(path, self.reference)

    def test_preview_sampling_keeps_real_duration_and_last_frame(self):
        indices, durations = emote_media._sample_preview(601, 30, 120)
        self.assertEqual((len(indices), indices[0], indices[-1]), (120, 0, 600))
        self.assertAlmostEqual(sum(durations), 601 * 1000 / 30)
        self.assertTrue(all(duration > 0 for duration in durations))
        one, duration = emote_media._sample_preview(601, 30, 1)
        self.assertEqual(one, [0])
        self.assertAlmostEqual(duration[0], 601 * 1000 / 30)
        for limit in (0, 241, True):
            with self.assertRaises(ValueError):
                emote_media._sample_preview(3, 30, limit)

    def test_native_preview_combines_both_streams_and_cleans_scratch(self):
        raw = make_usm(frames=3)

        def decode(_ffmpeg, source, _output, indices, width, height, pixel_format):
            self.assertGreater(source.stat().st_size, 0)
            self.assertEqual(indices, [0, 2])
            self.assertEqual((width, height), (160, 160))
            return (bytes((40, 80, 120)) * width * height * len(indices) if pixel_format == "rgb24"
                    else b"\x80" * width * height * len(indices))

        with tempfile.TemporaryDirectory() as folder, \
                patch.object(emote_media, "find_ffmpeg", return_value=Path("ffmpeg")), \
                patch.object(emote_media, "_decode_stream", side_effect=decode):
            result = emote_media.read_usm_preview(raw, work_dir=folder, max_preview_frames=2)
            self.assertEqual(result["total"], 3)
            self.assertEqual(len(result["frames"]), 2)
            self.assertAlmostEqual(sum(result["durations"]), 100)
            self.assertEqual(decoded_frame(result["frames"][0]).getpixel((80, 80)), (40, 80, 120, 128))
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_corrupted_usm_is_rejected_before_looking_for_decoder(self):
        with patch.object(emote_media, "find_ffmpeg") as resolver, self.assertRaises(ValueError):
            emote_media.read_usm_preview(b"not a USM")
        resolver.assert_not_called()

    def test_failed_decode_removes_partial_streams(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(emote_media, "find_ffmpeg", return_value=Path("ffmpeg")), \
                patch.object(emote_media, "_decode_stream", side_effect=ValueError("bad stream")):
            with self.assertRaisesRegex(ValueError, "bad stream"):
                emote_media.read_usm_preview(make_usm(), work_dir=folder)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_decoder_truncation_and_timeout_have_clear_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "input.m1v", Path(folder) / "out.raw"
            source.write_bytes(b"stream")
            output.write_bytes(b"short")
            with patch.object(emote_media.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
                with self.assertRaisesRegex(ValueError, "帧数"):
                    emote_media._decode_stream(Path("ffmpeg"), source, output, [0], 2, 2, "gray")
            with patch.object(emote_media.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffmpeg", 60)):
                with self.assertRaisesRegex(ValueError, "超时"):
                    emote_media._decode_stream(Path("ffmpeg"), source, output, [0], 2, 2, "gray")

    def test_explicit_missing_ffmpeg_does_not_silently_use_another_program(self):
        with self.assertRaisesRegex(ValueError, "FFmpeg"):
            emote_media.find_ffmpeg("nonexistent-emote-ffmpeg.exe")

    def test_prepare_previews_final_video_and_keeps_previous_output_on_failure(self):
        original, final = make_usm(frames=2), make_usm(frames=3, body=b"converted")
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "source.gif", Path(folder) / "result.usm"
            save_animation(source)
            source_before = source.read_bytes()
            output.write_bytes(b"previous")
            with patch.object(emote_media, "_reference_data", return_value=({"movieInfo": {"width": 0}}, original)), \
                    patch.object(emote_media, "find_ffmpeg", return_value=Path("ffmpeg")), \
                    patch.object(usm_encoder, "encode_emote_frames", return_value=final), \
                    patch.object(emote_media, "read_usm_preview", side_effect=ValueError("final decode failed")) as preview:
                with self.assertRaisesRegex(ValueError, "final decode"):
                    emote_media.prepare_emote_media(source, "reference.bundle", "VEmoji_sample", output, work_dir=folder)
                self.assertEqual(preview.call_args.args[0], final)
            self.assertEqual(output.read_bytes(), b"previous")
            self.assertEqual(source.read_bytes(), source_before)

    def test_direct_usm_input_is_copied_only_after_validation_and_actual_preview(self):
        original = make_usm(frames=2)
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "source.usm", Path(folder) / "result.usm"
            source.write_bytes(original)
            with patch.object(emote_media, "_reference_data", return_value=({"movieInfo": {"width": 0}}, original)), \
                    patch.object(emote_media, "find_ffmpeg", return_value=Path("ffmpeg")), \
                    patch.object(usm_encoder, "encode_emote_frames") as encoder, \
                    patch.object(emote_media, "read_usm_preview", return_value={"notice": "decoded", "frames": ["actual"]}) as preview:
                result = emote_media.prepare_emote_media(source, "reference.bundle", "VEmoji_sample", output, work_dir=folder)
            encoder.assert_not_called()
            self.assertEqual(preview.call_args.args[0], original)
            self.assertEqual(result["animation"]["frames"], ["actual"])
            self.assertEqual(output.read_bytes(), source.read_bytes())
            self.assertEqual(result["path"], str(output))

    def test_prepare_never_overwrites_import_source_or_reference_bundle(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.usm"
            source.write_bytes(b"original")
            with patch.object(emote_media, "_reference_data") as reference:
                for output in (source, Path(folder) / "reference.usm"):
                    with self.assertRaises(ValueError):
                        emote_media.prepare_emote_media(source, Path(folder) / "reference.usm", "video", output)
            reference.assert_not_called()
            self.assertEqual(source.read_bytes(), b"original")

    def test_cached_movie_metadata_is_rejected_before_preview_and_output(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "source.usm", Path(folder) / "result.usm"
            source.write_bytes(make_usm(frames=3))
            output.write_bytes(b"old")
            with patch.object(emote_media, "_reference_data", return_value=({"movieInfo": {"width": 160}}, make_usm())), \
                    patch.object(emote_media, "find_ffmpeg", return_value=Path("ffmpeg")), \
                    patch.object(emote_media, "read_usm_preview") as preview:
                with self.assertRaisesRegex(ValueError, "缓存"):
                    emote_media.prepare_emote_media(source, "reference.bundle", "video", output, work_dir=folder)
            preview.assert_not_called()
            self.assertEqual(output.read_bytes(), b"old")

    def test_video_duration_prefers_video_track_over_longer_audio_container(self):
        self.assertEqual(emote_media._video_duration({"duration": "1.25"}, {"duration": "6"}), 1250)
        self.assertEqual(emote_media._video_duration({"tags": {"DURATION": "00:00:01.250000"}}, {"duration": "6"}), 1250)
        with self.assertRaises(ValueError):
            emote_media._video_duration({}, {"duration": "nan"})

    def test_video_pixel_aspect_metadata_is_bounded_and_respects_rotation(self):
        self.assertEqual(emote_media._video_display_aspect({"sample_aspect_ratio": "2:1"}, 96, 64), 3)
        self.assertEqual(emote_media._video_display_aspect({"display_aspect_ratio": "3:1"}, 96, 64), 3)
        self.assertAlmostEqual(emote_media._video_display_aspect({"sample_aspect_ratio": "2:1", "side_data_list": [{"rotation": 90}]}, 96, 64), 1 / 3)
        for aspect in ("-1:2", "0:1", "65536:1", "1:0", "1000:1", "nan:1", "2"):
            with self.subTest(aspect=aspect), self.assertRaises(ValueError):
                emote_media._video_display_aspect({"sample_aspect_ratio": aspect}, 96, 64)

    def test_video_limits_reject_before_full_frame_count_and_decode(self):
        base = {"streams": [{"width": 96, "height": 64, "duration": "0.6", "codec_name": "h264", "pix_fmt": "yuv420p"}],
                "format": {"format_name": "mov,mp4", "duration": "0.6"}}
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "input.video"
            source.write_bytes(b"video")
            with patch.object(emote_media, "find_ffmpeg", return_value=Path("ffmpeg")), \
                    patch.object(emote_media, "find_ffprobe", return_value=Path("ffprobe")), \
                    patch.object(emote_media, "_probe_result", return_value=base) as probe, \
                    patch.object(emote_media, "MAX_SOURCE_DURATION_MS", 100), \
                    patch.object(emote_media, "_logged_process") as process:
                with self.assertRaisesRegex(ValueError, "30 秒"):
                    emote_media.adapt_video_source(source, self.reference, work_dir=folder)
                self.assertEqual(probe.call_count, 1)
                process.assert_not_called()
            self.assertEqual([path.name for path in Path(folder).iterdir()], ["input.video"])

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is not installed")
    def test_real_gif_conversion_and_final_dual_stream_preview(self):
        original = make_usm(frames=2)
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "source.gif", Path(folder) / "result.usm"
            save_animation(source, durations=(100, 300))
            with patch.object(emote_media, "_reference_data", return_value=({"movieInfo": {"width": 0}}, original)):
                result = emote_media.prepare_emote_media(source, "reference.bundle", "VEmoji_sample", output, work_dir=folder)
            preview = result["animation"]
            self.assertEqual(preview["total"], 12)
            self.assertAlmostEqual(sum(preview["durations"]), 400)
            self.assertEqual(preview["sha256"], cri_emote.inspect_usm_bytes(output.read_bytes())["sha256"])
            first, last = decoded_frame(preview["frames"][0]), decoded_frame(preview["frames"][-1])
            self.assertEqual(first.getpixel((0, 0))[3], 0)
            self.assertGreater(first.getpixel((80, 80))[3], 250)
            self.assertGreater(first.getpixel((80, 80))[0], 200)
            self.assertGreater(last.getpixel((80, 80))[2], 200)
            self.assertEqual(sorted(path.name for path in Path(folder).iterdir()), ["result.usm", "source.gif"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg/FFprobe is not installed")
    def test_real_common_video_content_conversion_and_alpha(self):
        original = make_usm(frames=2)
        with tempfile.TemporaryDirectory() as folder:
            for kind in ("mp4", "webm", "mov", "mkv", "avi"):
                with self.subTest(kind=kind):
                    source = save_video(folder, kind)
                    # The file picker may deliver a mislabeled filename.
                    renamed = source.with_suffix(".gif")
                    source.rename(renamed)
                    source_before = renamed.read_bytes()
                    output = Path(folder) / "result.usm"
                    with patch.object(emote_media, "_reference_data", return_value=({"movieInfo": {"width": 0}}, original)):
                        result = emote_media.prepare_emote_media(renamed, "reference.bundle", "VEmoji_sample", output, work_dir=folder)
                    self.assertEqual(result["source_info"]["source_frames"], 6)
                    self.assertEqual(result["animation"]["total"], 18)
                    self.assertAlmostEqual(sum(result["animation"]["durations"]), 600)
                    first = decoded_frame(result["animation"]["frames"][0])
                    self.assertEqual(first.getpixel((0, 0))[3], 0)
                    self.assertGreater(first.getpixel((80, 80))[3], 250)
                    if kind in ("webm", "mov", "mkv"):
                        self.assertTrue(result["source_info"]["has_alpha"])
                        self.assertLess(first.getpixel((15, 45))[3], 3)
                    else:
                        self.assertFalse(result["source_info"]["has_alpha"])
                        self.assertIn("没有透明通道", result["notice"])
                        self.assertGreater(first.getpixel((15, 45))[3], 250)
                    self.assertEqual(renamed.read_bytes(), source_before)
                    self.assertEqual(sorted(path.name for path in Path(folder).iterdir()), ["fixture.gif", "result.usm"])
                    renamed.unlink()
                    output.unlink()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg/FFprobe is not installed")
    def test_real_video_packet_pts_fallback_ignores_long_container_duration(self):
        original_probe = emote_media._probe_result

        def missing_video_duration(*args, **kwargs):
            probe = original_probe(*args, **kwargs)
            if not kwargs.get("count_frames"):
                probe["streams"][0].pop("duration", None)
                probe["streams"][0].get("tags", {}).pop("DURATION", None)
                probe["format"]["duration"] = "9.0"
            return probe

        with tempfile.TemporaryDirectory() as folder:
            source = save_video(folder, "mp4")
            with patch.object(emote_media, "_probe_result", side_effect=missing_video_duration):
                frames, info = emote_media.adapt_video_source(source, self.reference, work_dir=folder)
            self.assertEqual(info["timing_source"], "video_packet_pts")
            self.assertAlmostEqual(info["source_duration_ms"], 600)
            self.assertEqual(len(frames), 6)
            self.assertEqual([path.name for path in Path(folder).iterdir()], ["fixture.mp4"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg/FFprobe is not installed")
    def test_real_anamorphic_video_is_fit_using_display_aspect(self):
        with tempfile.TemporaryDirectory() as folder:
            source = save_video(folder, "mp4")
            anamorphic = Path(folder) / "wide.mp4"
            subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-nostdin", "-y", "-i", str(source),
                            "-vf", "setsar=2", "-c:v", "mpeg4", "-pix_fmt", "yuv420p", str(anamorphic)],
                           capture_output=True, check=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            frames, info = emote_media.adapt_video_source(anamorphic, self.reference, work_dir=folder)
            self.assertAlmostEqual(info["source_display_aspect"], 3)
            self.assertEqual((info["fitted_width"], info["fitted_height"]), (160, 53))
            self.assertEqual(frames[0].getchannel("A").getbbox()[2:], (160, 106))
            output = Path(folder) / "result.usm"
            with patch.object(emote_media, "_reference_data", return_value=({"movieInfo": {"width": 0}}, make_usm())):
                result = emote_media.prepare_emote_media(anamorphic, "reference.bundle", "video", output, work_dir=folder)
            first = decoded_frame(result["animation"]["frames"][0])
            solid = first.getchannel("A").point(lambda alpha: 255 if alpha >= 250 else 0).getbbox()
            self.assertEqual((solid[2] - solid[0], solid[3] - solid[1]), (160, 53))
            self.assertEqual(sorted(path.name for path in Path(folder).iterdir()), ["fixture.mp4", "result.usm", "wide.mp4"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg/FFprobe is not installed")
    def test_real_single_frame_video_shorter_than_target_frame_is_not_dropped(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "single-frame.mp4"
            subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-nostdin", "-y", "-f", "lavfi",
                            "-i", "color=c=red:s=96x64:r=30", "-frames:v", "1", "-c:v", "mpeg4",
                            "-pix_fmt", "yuv420p", str(source)], capture_output=True, check=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            before = source.read_bytes()
            frames, info = emote_media.adapt_video_source(source, self.reference, work_dir=folder)
            self.assertEqual((info["source_frames"], len(frames)), (1, 1))
            self.assertAlmostEqual(info["source_duration_ms"], 1000 / 30, places=2)
            self.assertEqual(info["duration_ms"], 100)
            self.assertLess(abs(info["duration_error_ms"]), 1000 / self.reference["fps"])
            self.assertGreater(frames[0].getpixel((80, 80))[0], 240)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual([path.name for path in Path(folder).iterdir()], ["single-frame.mp4"])

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is not installed")
    def test_real_long_video_preview_does_not_exceed_ffmpeg_expression_depth(self):
        with tempfile.TemporaryDirectory() as folder:
            frames = [Image.new("RGBA", (160, 160), (index, 80, 180, 255)) for index in range(125)]
            raw = usm_encoder.encode_emote_frames(frames, make_usm(), emote_media.find_ffmpeg(), work_dir=folder)
            preview = emote_media.read_usm_preview(raw, work_dir=folder)
            self.assertEqual((len(preview["frames"]), preview["total"]), (120, 125))
            self.assertAlmostEqual(sum(preview["durations"]), 125 * 1000 / 30)
            self.assertGreater(decoded_frame(preview["frames"][-1]).getpixel((80, 80))[0], 120)
            self.assertEqual(list(Path(folder).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
