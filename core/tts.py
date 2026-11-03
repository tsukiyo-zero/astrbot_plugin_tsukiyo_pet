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

    async def synthesize(self, text: str) -> Path:
        payload = build_payload(text, self.cfg)
        timeout = float(self.cfg.get("timeout_seconds") or 120)
        try:
            async with httpx.AsyncClient(timeout=timeout) as http:
                resp = await http.post(self.endpoint, json=payload)
                resp.raise_for_status()
                data = resp.content
        except httpx.HTTPError as exc:
            raise TTSError(f"SoVITS 请求失败：{exc}") from exc
        if len(data) < 1024:
            raise TTSError("SoVITS 返回的音频太小，可能合成失败")
        name = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.wav"
        target = self.audio_dir / name
        target.write_bytes(data)
        self._log(f"语音已生成：{name}（{len(data) // 1024} KB）")
        return target
