"""长文本语音的分段与拼接（纯标准库，numpy 可选加速）。

为什么需要这层：
- 整段一次性合成时，长文本会被"赶"着读完（实测 0.153 s/字，原声 0.262）
- 而且同一段里的随机轨迹会带着整篇语调漂，分句独立合成能逐句定调
- 分段合成有代价：每段音量不同、段间停顿不能一刀切，所以要在这里归一 + 按标点留白
"""
from __future__ import annotations

import io
import math
import re
import wave
from array import array

try:  # numpy 有就快一点，没有也能跑
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None

#: 句末标点 → 段间留白（毫秒）
GAP_MS = {"。": 220, "！": 260, "？": 260, "…": 300, "!": 260, "?": 260, ".": 220, "；": 180, ";": 180}
DEFAULT_GAP_MS = 180
#: 每字少于这个秒数就认为吞字
MIN_SEC_PER_CHAR = 0.16


def visible_chars(text: str) -> int:
    return len([c for c in str(text) if c.strip()])


def split_text(text: str, max_chars: int = 40, max_segments: int = 12) -> list[str]:
    """按句末标点切段；单段过长再按逗号切；段数超上限就把尾巴并成一段。"""
    raw = str(text or "").strip()
    if not raw:
        return []
    max_chars = max(8, int(max_chars))
    max_segments = max(1, int(max_segments))

    chunks: list[str] = []
    buf = ""
    for ch in raw:
        buf += ch
        if ch in "。！？…\n":
            if buf.strip():
                chunks.append(buf.strip())
            buf = ""
    if buf.strip():
        chunks.append(buf.strip())

    # 单段 jeszcze 太长 → 按逗号再切；仍太长就硬切
    refined: list[str] = []
    for chunk in chunks:
        if visible_chars(chunk) <= max_chars * 1.6:
            refined.append(chunk)
            continue
        pieces = re.split(r"(?<=[，,、])", chunk)
        cur = ""
        for piece in pieces:
            if cur and visible_chars(cur + piece) > max_chars:
                refined.append(cur.strip())
                cur = piece
            else:
                cur += piece
        if cur.strip():
            refined.append(cur.strip())
    chunks = [c for c in refined if c.strip()]

    if len(chunks) > max_segments:  # 保上限，尾巴合并，避免把 TTS 打满
        head = chunks[: max_segments - 1]
        tail = "".join(chunks[max_segments - 1:])
        chunks = head + [tail]
    return chunks


def gap_ms(segment: str) -> int:
    """按段末标点决定留白。"""
    for ch in reversed(str(segment or "").strip()):
        if ch in GAP_MS:
            return GAP_MS[ch]
        if ch.strip():
            break
    return DEFAULT_GAP_MS


def is_truncated(seconds: float, chars: int) -> bool:
    """时长/字数太低 = 模型提前收尾（吞字）。"""
    if chars <= 0:
        return False
    return (float(seconds) / chars) < MIN_SEC_PER_CHAR


# ---------------------------------------------------------------- 音频处理
def read_pcm(data: bytes) -> tuple[array, int]:
    """wav 字节 → (int16 单声道采样, 采样率)。多声道取平均。"""
    with wave.open(io.BytesIO(data), "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        frames = w.readframes(w.getnframes())
    if sw != 2:
        raise ValueError(f"只支持 16bit wav，收到 {sw * 8}bit")
    samples = array("h")
    samples.frombytes(frames)
    if ch > 1:
        mono = array("h", bytes(len(samples) // ch * 2))
        for i in range(len(mono)):
            mono[i] = sum(samples[i * ch + k] for k in range(ch)) // ch
        samples = mono
    return samples, sr


def rms(samples: array) -> float:
    if not samples:
        return 0.0
    if _np is not None:
        arr = _np.frombuffer(samples.tobytes(), dtype="<i2").astype("float32")
        return float(_np.sqrt(_np.mean(arr ** 2)))
    total = sum(float(s) * float(s) for s in samples)
    return math.sqrt(total / len(samples))


def gain_to(samples: array, target_rms: float, peak_ceiling: float = 29000.0) -> array:
    """把一段的 RMS 拉到 target_rms，同时不越峰值。"""
    cur = rms(samples)
    if cur <= 1e-6 or target_rms <= 0:
        return samples
    gain = target_rms / cur
    peak = max(abs(s) for s in samples) if samples else 0
    if peak * gain > peak_ceiling:
        gain = peak_ceiling / max(1.0, peak)
    if abs(gain - 1.0) < 0.02:
        return samples
    if _np is not None:
        arr = _np.frombuffer(samples.tobytes(), dtype="<i2").astype("float32") * gain
        return array("h", _np.clip(arr, -32768, 32767).astype("<i2").tobytes())
    out = array("h", bytes(len(samples) * 2))
    for i, s in enumerate(samples):
        v = int(s * gain)
        out[i] = 32767 if v > 32767 else (-32768 if v < -32768 else v)
    return out


def compose(parts: list[tuple[bytes, int]], target_rms: float = 3300.0) -> bytes:
    """把多段 wav 拼成一个：段级音量归一 → 按标点留白 → 整体峰值收敛。返回 wav 字节。"""
    picked = [(read_pcm(data), gap) for data, gap in parts if data]
    if not picked:
        raise ValueError("没有可拼接的音频")
    sr = picked[0][0][1]
    out = array("h")
    for (samples, sr_i), gap in picked:
        if sr_i != sr:  # 采样率不一致就跳过（正常不会发生）
            continue
        out.extend(gain_to(samples, target_rms))
        out.extend(array("h", bytes(int(sr * gap / 1000) * 2)))  # 静音留白
    peak = max((abs(s) for s in out), default=0)
    if peak > 31000:
        out = gain_to(out, rms(out) * 31000.0 / peak)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(out.tobytes())
    return buf.getvalue()


def wav_seconds(data: bytes) -> float:
    with wave.open(io.BytesIO(data), "rb") as w:
        return w.getnframes() / float(w.getframerate() or 1)
