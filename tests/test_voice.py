"""分段与拼接的纯逻辑测试（不需要 SoVITS、不需要 numpy 也能跑）。"""
from __future__ import annotations

import io
import sys
import unittest
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO.parent) not in sys.path:
    sys.path.insert(0, str(REPO.parent))

from astrbot_plugin_tsukiyo_pet.core import voice  # noqa: E402


def make_wav(seconds: float, sr: int = 32000, amp: int = 8000) -> bytes:
    n = max(1, int(seconds * sr))
    frames = b"".join(int(amp).to_bytes(2, "little", signed=True) for _ in range(n))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(frames)
    return buf.getvalue()


class TestSplit(unittest.TestCase):
    def test_split_by_sentence_end(self) -> None:
        segs = voice.split_text("第一句。第二句！第三句？", max_chars=40)
        self.assertEqual(len(segs), 3)
        self.assertTrue(segs[0].endswith("。"))

    def test_long_sentence_split_by_comma(self) -> None:
        text = "这句话很长，" * 12 + "结束。"
        segs = voice.split_text(text, max_chars=30)
        self.assertGreater(len(segs), 1)
        self.assertTrue(all(voice.visible_chars(s) <= 60 for s in segs))

    def test_max_segments_merges_tail(self) -> None:
        text = "一句。二句。三句。四句。五句。六句。"
        segs = voice.split_text(text, max_chars=10, max_segments=3)
        self.assertEqual(len(segs), 3)
        self.assertIn("六句", segs[-1])

    def test_empty(self) -> None:
        self.assertEqual(voice.split_text("   "), [])


class TestGap(unittest.TestCase):
    def test_by_punctuation(self) -> None:
        self.assertEqual(voice.gap_ms("你好。"), 220)
        self.assertEqual(voice.gap_ms("你好？"), 260)
        self.assertEqual(voice.gap_ms("你好…"), 300)
        self.assertEqual(voice.gap_ms("你好，"), voice.DEFAULT_GAP_MS)


class TestTruncation(unittest.TestCase):
    def test_short_is_truncated(self) -> None:
        self.assertTrue(voice.is_truncated(1.66, 24))   # 实测 speed=1.0 的吞字案例

    def test_normal(self) -> None:
        self.assertFalse(voice.is_truncated(5.2, 24))

    def test_zero_chars(self) -> None:
        self.assertFalse(voice.is_truncated(1.0, 0))


class TestCompose(unittest.TestCase):
    def test_compose_adds_gaps_and_keeps_rate(self) -> None:
        a, b = make_wav(0.20), make_wav(0.20)
        out = voice.compose([(a, 220), (b, 220)])
        seconds = voice.wav_seconds(out)
        # 两段各 0.2s + 两处 220ms 留白 ≈ 0.84s（采样量化±0.05）
        self.assertAlmostEqual(seconds, 0.84, delta=0.08)

    def test_compose_numpy_optional(self) -> None:
        """有没有 numpy 结果都应可用（这里只验证能产出合法 wav）。"""
        out = voice.compose([(make_wav(0.1), 100)])
        with wave.open(io.BytesIO(out), "rb") as w:
            self.assertEqual(w.getnchannels(), 1)
            self.assertEqual(w.getsampwidth(), 2)


if __name__ == "__main__":
    unittest.main()
