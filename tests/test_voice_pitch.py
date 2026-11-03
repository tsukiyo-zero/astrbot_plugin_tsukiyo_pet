"""句末音调体检的测试（用合成波形，不依赖 SoVITS）。"""
from __future__ import annotations

import io
import math
import sys
import unittest
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO.parent) not in sys.path:
    sys.path.insert(0, str(REPO.parent))

from astrbot_plugin_tsukiyo_pet.core import voice  # noqa: E402


def tone(freqs: list[float], seconds_each: float = 0.5, sr: int = 32000, amp: float = 0.35) -> bytes:
    """按给定的 F0 序列拼一段"唱音"，用来模拟句末音调上跳。"""
    frames = bytearray()
    for f in freqs:
        n = int(seconds_each * sr)
        for i in range(n):
            v = int(amp * 32767 * math.sin(2 * math.pi * f * i / sr))
            frames += v.to_bytes(2, "little", signed=True)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return buf.getvalue()


class TestFinalPitch(unittest.TestCase):
    def test_flat_has_small_delta(self) -> None:
        delta = voice.final_pitch_delta(tone([250, 250, 250, 250]))
        self.assertLess(abs(delta), 30)

    def test_rising_tail_is_positive(self) -> None:
        delta = voice.final_pitch_delta(tone([250, 250, 250, 380]))
        self.assertGreater(delta, 60)

    def test_falling_tail_is_negative(self) -> None:
        delta = voice.final_pitch_delta(tone([300, 300, 300, 220]))
        self.assertLess(delta, -30)

    def test_silence_returns_zero(self) -> None:
        self.assertEqual(voice.final_pitch_delta(tone([0, 0, 0], amp=0.0)), 0.0)

    def test_too_short_returns_zero(self) -> None:
        self.assertEqual(voice.final_pitch_delta(tone([250], seconds_each=0.2)), 0.0)


class TestNeedsRetry(unittest.TestCase):
    def test_truncated_triggers(self) -> None:
        self.assertTrue(voice.needs_retry(tone([250] * 4), chars=80))   # 2s/80字 = 太短

    def test_spike_triggers(self) -> None:
        data = tone([250, 250, 250, 400])
        self.assertTrue(voice.needs_retry(data, chars=8))               # 时长够，但句末甩了

    def test_clean_passes(self) -> None:
        data = tone([250, 250, 250, 255])
        self.assertFalse(voice.needs_retry(data, chars=8))


if __name__ == "__main__":
    unittest.main()
