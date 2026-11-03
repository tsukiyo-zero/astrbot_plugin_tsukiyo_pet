"""GPT-SoVITS 客户端（可选）。

插件只负责「文本 → wav 文件」，把文件放进 audio_dir，客户端按 URL 拉走播放。
容器里访问宿主机的 SoVITS 用 host.docker.internal。
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx

from . import voice


class TTSError(RuntimeError):
    pass


def _num(value: Any, default: float) -> float:
    try:
        return float(value) if value not in (None, "") else float(default)
    except (TypeError, ValueError):
        return float(default)


def build_payload(text: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """按配置拼 /tts 请求体；供单测直接断言。"""
    ref = str(cfg.get("ref_audio_path") or "").strip()
    prompt_text = str(cfg.get("prompt_text") or "").strip()
    if not ref:
        raise TTSError("tts.ref_audio_path 未配置")
    if not str(text or "").strip():
        raise TTSError("要合成的文本为空")
    return {
        "text": str(text)[:500],
        "text_lang": str(cfg.get("text_lang") or "zh"),
        "ref_audio_path": ref,
        "prompt_text": prompt_text,
        "prompt_lang": str(cfg.get("prompt_lang") or "zh"),
        "speed_factor": _num(cfg.get("speed_factor"), 0.9),
        "text_split_method": str(cfg.get("text_split_method") or "cut0"),
        "media_type": "wav",
        # 采样参数：不传的话服务端默认 temperature=1.0（随机性拉满，语调会飘、偶发吞字）
        "temperature": _num(cfg.get("temperature"), 0.85),
        "top_k": int(_num(cfg.get("top_k"), 12)),
        "repetition_penalty": _num(cfg.get("repetition_penalty"), 1.35),
        "seed": int(_num(cfg.get("seed"), -1)),
        "fragment_interval": _num(cfg.get("fragment_interval"), 0.3),
    }


class SoVITSClient:
    def __init__(self, cfg: dict[str, Any], audio_dir: Path,
                 log: Callable[[str], None] | None = None) -> None:
        self.cfg = cfg or {}
        self.audio_dir = Path(audio_dir)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self._log = log or (lambda _m: None)

    @property
    def endpoint(self) -> str:
        return str(self.cfg.get("endpoint") or "http://127.0.0.1:9880/tts")

    async def _request(self, text: str, speed: float, *, split_method: str = "cut0") -> bytes:
        payload = build_payload(text, self.cfg)
        payload["speed_factor"] = speed
        payload["text_split_method"] = split_method
        timeout = float(self.cfg.get("timeout_seconds") or 120)
        try:
            async with httpx.AsyncClient(timeout=timeout) as http:
                resp = await http.post(self.endpoint, json=payload)
                resp.raise_for_status()
                return resp.content
        except httpx.HTTPError as exc:
            raise TTSError(f"SoVITS 请求失败：{exc}") from exc

    def _save(self, data: bytes, note: str = "") -> Path:
        if len(data) < 1024:
            raise TTSError("SoVITS 返回的音频太小，可能合成失败")
        name = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.wav"
        target = self.audio_dir / name
        target.write_bytes(data)
        self._log(f"语音已生成：{name}（{len(data) // 1024} KB）{note}")
        return target

    async def _best_of(self, text: str, chars: int, base_speed: float,
                       candidates: int, spike_hz: float, tag: str) -> bytes:
        """合成 1~N 条，按「不吞字 + 句末音调不甩」择优。

        SoVITS 的句末重音是随机的（实测同一句能 +0 / +137 / -16 Hz），
        光降 temperature 治不干净，多来一条挑更顺的才靠谱。
        """
        best: tuple[float, bytes] | None = None
        speed = base_speed
        for attempt in range(max(1, candidates)):
            data = await self._request(text, speed)
            seconds = voice.wav_seconds(data)
            delta = voice.final_pitch_delta(data)
            truncated = voice.is_truncated(seconds, chars)
            score = (1000.0 if truncated else 0.0) + abs(delta)
            if best is None or score < best[0]:
                best = (score, data)
            if not truncated and abs(delta) <= spike_hz:
                break
            if truncated:
                speed = round(max(0.7, speed - 0.05), 2)
                self._log(f"{tag}疑似吞字（{seconds:.2f}s/{chars}字），降到 {speed} 重试")
            else:
                self._log(f"{tag}句末音调偏了 {delta:+.0f} Hz，再合成一条择优")
        assert best is not None
        return best[1]

    async def synthesize(self, text: str) -> Path:
        """短句直接合成；长句（可配置）走分段合成 + 段间留白 + 音量和响度归一。"""
        base_speed = _num(self.cfg.get("speed_factor"), 0.9)
        chars = voice.visible_chars(text)
        candidates = max(1, min(3, int(_num(self.cfg.get("max_candidates"), 2))))
        spike_hz = _num(self.cfg.get("spike_hz"), voice.SPIKE_HZ)

        segmented = bool(self.cfg.get("segment_long", True)) and chars >= int(
            _num(self.cfg.get("segment_min_chars"), 40)
        )
        if not segmented:
            data = await self._best_of(text, chars, base_speed, candidates, spike_hz, "短句")
            return self._save(data)

        segments = voice.split_text(
            text,
            max_chars=int(_num(self.cfg.get("segment_max_chars"), 40)),
            max_segments=int(_num(self.cfg.get("max_segments"), 12)),
        )
        parts: list[tuple[bytes, int]] = []
        for i, seg in enumerate(segments, 1):
            data = await self._best_of(seg, voice.visible_chars(seg), base_speed,
                                       candidates, spike_hz, f"第 {i}/{len(segments)} 段")
            parts.append((data, voice.gap_ms(seg)))
        merged = voice.compose(parts)
        return self._save(merged, f"（{len(segments)} 段拼接，{voice.wav_seconds(merged):.1f}s）")
