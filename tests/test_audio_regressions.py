"""Audio preview recovery uses generated WAV files in an isolated workspace."""
from __future__ import annotations

import hashlib
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from astral_party_auto.modkit.audio_workspace import AudioWorkspace


def write_sample(path: Path) -> bytes:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\x01\x00" * 800)
    return path.read_bytes()


class AudioPreviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="jixing-audio-recovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        controller = SimpleNamespace(aa_dirs=(), log=Mock())
        self.workspace = AudioWorkspace(controller, self.root / "data", self.root / "made")
        self.workspace._rows = {"sound": {
            "id": "sound", "bundle": "sound.bundle", "object_id": "1", "media_id": 101,
            "name": "sound", "editable": True,
        }}
        self.raw = b"generated-audio-source"
        self.cached = self.workspace.cache_dir / (hashlib.sha256(self.raw).hexdigest() + ".wav")
        self.sample = write_sample(self.root / "sample.wav")

    def decode_sample(self, arguments, **_kwargs):
        Path(arguments[arguments.index("-o") + 1]).write_bytes(self.sample)
        return SimpleNamespace(returncode=0)

    def test_retry_rebuilds_broken_and_truncated_preview_cache(self):
        zero_rate = self.sample[:24] + b"\0" * 4 + self.sample[28:]
        for damaged in (b"not-a-wave", self.sample[:-20], zero_rate):
            with self.subTest(damaged_size=len(damaged)):
                self.cached.write_bytes(damaged)
                with patch.object(self.workspace, "_bytes", return_value=self.raw), \
                        patch.object(self.workspace, "_decoder", return_value=Path("fake-decoder.exe")), \
                        patch("astral_party_auto.modkit.audio_workspace.subprocess.run", side_effect=self.decode_sample) as decoder:
                    preview = self.workspace.preview("sound")
                self.assertEqual(decoder.call_count, 1)
                self.assertEqual(self.cached.read_bytes(), self.sample)
                self.assertAlmostEqual(preview["duration"], .1)
                self.assertEqual(self.workspace.media_path(preview["url"].rsplit("/", 1)[-1]), self.cached)
                self.assertFalse(list(self.workspace.cache_dir.glob("*.part.wav")))
                self.assertFalse(list(self.workspace.cache_dir.glob("*.wem")))

    def test_decoder_success_with_truncated_samples_is_not_cached(self):
        def truncated_decode(arguments, **_kwargs):
            Path(arguments[arguments.index("-o") + 1]).write_bytes(self.sample[:-20])
            return SimpleNamespace(returncode=0)

        with patch.object(self.workspace, "_decoder", return_value=Path("fake-decoder.exe")), \
                patch("astral_party_auto.modkit.audio_workspace.subprocess.run", side_effect=truncated_decode):
            with self.assertRaisesRegex(RuntimeError, "音频.*不完整"):
                self.workspace._decode(self.raw)
        self.assertFalse(self.cached.exists())
        self.assertEqual(list(self.workspace.cache_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
