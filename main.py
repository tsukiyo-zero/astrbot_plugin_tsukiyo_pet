"""月夜宠物（tsukiyo_pet）· AstrBot 入口。

设计边界：
- 插件只做「大脑到身体」的转发：收 QQ 事件 → 推进桥的队列；桌宠客户端自己来拉。
- 不起 GUI、不 Popen 任何窗口进程，所以 AstrBot 跑 Docker 也能用。
- 与平台无关：aiocqhttp / qq_official 都行，主人用 sender_id 字符串比对。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from . import __version__
from .core import protocol
from .core.hub import Hub
from .core.httpapi import PetHTTP
from .core.owner import OwnerGuard
from .core.tts import SoVITSClient, TTSError

PLUGIN_NAME = "astrbot_plugin_tsukiyo_pet"


@register("tsukiyo_pet", "Mio (tsukiyo-zero)",
          "月夜宠物：把 AstrBot 接成桌面透明桌宠（气泡同步 / 动作控制 / 可选语音）",
          __version__)
class TsukiyoPetPlugin(Star):
    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir = self.data_dir / "audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)

        self.hub = Hub()
        self.owner = OwnerGuard(str(self._cfg("owner_id") or ""))
        self.http: PetHTTP | None = None
        self.tts: SoVITSClient | None = None
        self._setup()

    # ------------------------------------------------------------------ 配置
    def _cfg(self, key: str, default: Any = None) -> Any:
        return (self.config or {}).get(key, default)

    def _bridge_cfg(self) -> dict[str, Any]:
        value = self._cfg("bridge") or {}
        return value if isinstance(value, dict) else {}

    def _tts_cfg(self) -> dict[str, Any]:
        value = self._cfg("tts") or {}
        return value if isinstance(value, dict) else {}

    def _setup(self) -> None:
        bridge = self._bridge_cfg()
        host = str(bridge.get("host") or "0.0.0.0")
        port = int(bridge.get("port") or 18790)
        self.http = PetHTTP(self.hub, host=host, port=port, audio_dir=self.audio_dir,
                            push_token=str(bridge.get("push_token") or ""),
                            public_base=str(bridge.get("public_base") or "http://127.0.0.1:18790"),
                            log=lambda m: logger.info("[tsukiyo_pet] %s", m))
        try:
            self.http.start()
        except OSError as exc:
            logger.error("[tsukiyo_pet] 桌面桥启动失败（端口被占？）：%s", exc)
            self.http = None

        tts = self._tts_cfg()
        self.tts = SoVITSClient(tts, self.audio_dir, log=lambda m: logger.info("[tsukiyo_pet] %s", m)) \
            if tts.get("enabled") else None
        logger.info("[tsukiyo_pet] 就绪：桥=%s 音频目录=%s 语音=%s 主人=%s",
                    f"{host}:{port}", self.audio_dir,
                    "开" if self.tts else "关", self.owner.bound or "(未绑定)")

    # ------------------------------------------------------------------ 工具方法
    def _is_owner(self, event: AstrMessageEvent) -> bool:
        try:
            return self.owner.is_owner(event.get_sender_id(), is_private=bool(event.is_private_chat()))
        except Exception:  # pragma: no cover - 事件对象异常不影响主流程
            return False

    def _audio_url(self, name: str) -> str:
        base = str(self._bridge_cfg().get("public_base") or "http://127.0.0.1:18790").rstrip("/")
        return f"{base}/pet/audio/{name}"

    async def _say(self, text: str) -> tuple[bool, str]:
        """推一个气泡；开了语音就异步合成，合好再推一条带音频的指令。

        语音合成要 1~4 秒，绝不能卡住 QQ 回复——所以气泡先推，音频后台补。
        """
        bubble_text = protocol.clamp_text(text, int(self._cfg("bubble_max_chars") or 40))
        item = protocol.bubble(bubble_text)
        if item is None:
            return False, "内容为空"
        online = self.hub.push(item)
        if not online:
            return False, "桌宠不在线"
        if self.tts is not None:
            asyncio.create_task(self._speak_async(str(text)))
        return True, ""

    async def _speak_async(self, text: str) -> None:
        if self.tts is None:
            return
        try:
            wav = await self.tts.synthesize(text)
        except TTSError as exc:
            logger.warning("[tsukiyo_pet] 语音合成失败：%s", exc)
            return
        # 气泡已经在前面显示过，这里只带声音，不重复弹字
        if not self.hub.push(protocol.speak(self._audio_url(wav.name), "")):
            logger.info("[tsukiyo_pet] 语音已生成但桌宠不在线：%s", wav.name)

    # ------------------------------------------------------------------ 命令
    @filter.command("pet")
    async def pet_command(self, event: AstrMessageEvent, sub: str = "status", rest: str = ""):
        """桌宠控制：/pet status | say <文本> | action <动作> | mode <模式>"""
        if not self._is_owner(event):
            yield event.plain_result("桌宠只接受主人私聊控制（检查 owner_id 配置）")
            return
        action = (sub or "status").strip().lower()

        if action == "status":
            snap = self.hub.snapshot()
            primary = self.hub.primary()
            lines = [f"桌宠在线：{snap['online']} 个"]
            if primary:
                st = primary.state
                lines.append(f"位置：({st.get('x')}, {st.get('y')}) 模式：{st.get('mode')}")
            events = self.hub.recent_events(3)
            if events:
                lines.append("最近互动：" + "、".join(e.get("kind", "?") for e in events))
            if self.tts is not None:
                lines.append("语音：开")
            yield event.plain_result("\n".join(lines))
            return

        if action in ("say", "说"):
            text = (rest or "").strip()
            if not text:
                yield event.plain_result("用法：/pet say 要说的话")
                return
            ok, note = await self._say(text)
            yield event.plain_result(("已发送给桌宠" if ok else f"没发出去：{note}") + note if ok else note)
            return

        if action == "action":
            item = protocol.action(rest)
            if item is None:
                allowed = "、".join(list(protocol.ACTIONS) + list(protocol.DIRECTIONS))
                yield event.plain_result(f"未知动作。可用：{allowed}")
                return
            ok = self.hub.push(item)
            yield event.plain_result("已执行" if ok else "桌宠不在线")
            return

        if action == "mode":
            mode = (rest or "").strip().lower()
            if mode not in protocol.MODES:
                yield event.plain_result("模式只有：idle / wander / follow")
                return
            ok = self.hub.push(protocol.Item("mode", {"mode": mode}))
            yield event.plain_result("已切换" if ok else "桌宠不在线")
            return

        yield event.plain_result("用法：/pet status | say <文本> | action <动作> | mode <模式>")

    # ------------------------------------------------------------------ 回复同步
    @filter.on_decorating_result()
    async def sync_bubble(self, event: AstrMessageEvent):
        """主人的私聊回复同步到桌宠气泡（开了语音就一起说）。"""
        if not bool(self._cfg("bubble_enabled", True)):
            return
        if not self._is_owner(event):
            return
        if self.hub.primary() is None:
            return
        text = self._result_text(event)
        if not text:
            return
        try:
            await self._say(text)
        except Exception as exc:  # pragma: no cover - 同步失败不能影响正常回复
            logger.warning("[tsukiyo_pet] 气泡同步失败：%s", exc)

    @staticmethod
    def _result_text(event: AstrMessageEvent) -> str:
        try:
            result = event.get_result()
        except Exception:  # pragma: no cover
            return ""
        if result is None:
            return ""
        chain = getattr(result, "chain", None)
        if isinstance(chain, str):
            return chain.strip()
        parts: list[str] = []
        for comp in chain or []:
            text = getattr(comp, "text", None)
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        if parts:
            return "\n".join(parts)
        raw = getattr(result, "message_str", "") or ""
        return str(raw).strip()

    # ------------------------------------------------------------------ LLM 工具
    @filter.llm_tool(name="pet_action")
    async def pet_action(self, event: AstrMessageEvent, action: str, reason: Optional[str] = None) -> str:
        """让桌面上的桌宠做一个动作。用户说“跳一下 / 摇一摇 / 伸个懒腰 / 挥手 / 过来 / 往左走 / 跟着我”时调用。

        Args:
            action(string): 动作名。抖动类：jump（跳）/ shake（摇一摇）/ stretch（伸懒腰）/ wave（挥手）；移动类：left/right/up/down/left_up/right_up/left_down/right_down/center；模式类：idle（待着）/ wander（散步）/ follow（跟鼠标）
            reason(string, optional): 一句话说明为什么要它做这个动作，会记录在日志里
        """
        item = protocol.action(action)
        if item is None:
            return json.dumps({"ok": False, "error": f"未知动作：{action}"}, ensure_ascii=False)
        ok = self.hub.push(item)
        if reason:
            logger.info("[tsukiyo_pet] 动作 %s（%s）", action, reason)
        return json.dumps({"ok": ok, "action": action, "queued": ok}, ensure_ascii=False)

    @filter.llm_tool(name="pet_state")
    async def pet_state(self, event: AstrMessageEvent) -> str:
        """查询桌面桌宠的在线状态、当前位置、移动模式和最近的互动（点击/拖动/喂食）。

        用户问“它在哪 / 它在干嘛 / 刚才是不是戳你了 / 桌宠开着吗”时调用。
        """
        snap = self.hub.snapshot()
        primary = self.hub.primary()
        payload = {
            "online": snap["online"],
            "client": primary.as_dict() if primary else None,
            "recent_events": self.hub.recent_events(5),
        }
        return json.dumps(payload, ensure_ascii=False)

    # ------------------------------------------------------------------ 生命周期
    async def terminate(self) -> None:
        if self.http is not None:
            self.http.stop()

