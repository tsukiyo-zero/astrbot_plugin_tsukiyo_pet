"""桌宠与插件之间的协议：动作白名单、事件校验、指令构造。

这一层不 import AstrBot，也不 import PySide6，纯逻辑可单测。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 允许的动作。前端只认这些，模型也只能从这里挑。
ACTIONS: dict[str, str] = {
    "jump": "跳一下",
    "shake": "摇一摇",
    "stretch": "伸个懒腰",
    "wave": "挥手",
    "idle": "待着",
    "wander": "散步",
    "follow": "跟着鼠标",
}

#: 移动指令的合法方向（客户端自己算坐标，插件只传意图）
DIRECTIONS: dict[str, str] = {
    "left": "往左",
    "right": "往右",
    "up": "往上",
    "down": "往下",
    "left_up": "左上",
    "right_up": "右上",
    "left_down": "左下",
    "right_down": "右下",
    "center": "回屏幕中间",
}

MODES = ("idle", "wander", "follow")

#: 客户端会报上来的身体事件
EVENT_KINDS = ("click", "drag", "feed", "mode", "move_done", "hello")

MAX_TEXT = 300


@dataclass
class Item:
    """一条要推给桌宠的指令。"""

    kind: str  # bubble | action | move | mode | speak | quit
    payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, **self.payload}

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]


def bubble(text: str) -> Item | None:
    text = str(text or "").strip()
    if not text:
        return None
    return Item("bubble", {"text": text[:MAX_TEXT]})


def action(name: str) -> Item | None:
    key = str(name or "").strip().lower()
    if key in DIRECTIONS:
        return Item("move", {"direction": key})
    if key in ("idle", "wander", "follow"):
        return Item("mode", {"mode": key})
    if key in ACTIONS:
        return Item("action", {"name": key})
    return None


def speak(url: str, text: str = "") -> Item | None:
    url = str(url or "").strip()
    if not url:
        return None
    return Item("speak", {"url": url, "text": str(text or "")[:MAX_TEXT]})


def parse_event(payload: dict[str, Any]) -> dict[str, Any]:
    """校验并规整客户端上报的事件；不合法就抛 ValueError。"""
    if not isinstance(payload, dict):
        raise ValueError("事件必须是对象")
    kind = str(payload.get("kind") or "").strip()
    if kind not in EVENT_KINDS:
        raise ValueError(f"未知事件类型：{kind or '(空)'}")
    out: dict[str, Any] = {"kind": kind, "ts": float(payload.get("ts") or 0.0)}
    for key in ("x", "y", "mode"):
        if payload.get(key) is not None:
            out[key] = payload[key]
    return out


def clamp_text(text: str, limit: int = 40) -> str:
    """气泡分段用：按标点切，超长截断。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    limit = max(1, int(limit))
    chunks: list[str] = []
    buf = ""
    for ch in raw:
        buf += ch
        if ch in "。！？!?；;…\n" or len(buf) >= limit:
            chunks.append(buf.strip())
            buf = ""
    if buf.strip():
        chunks.append(buf.strip())
    return "\n".join(c for c in chunks if c)[: MAX_TEXT * 2]
