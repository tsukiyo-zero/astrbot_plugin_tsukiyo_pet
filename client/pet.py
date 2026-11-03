"""月夜宠物 · 桌面客户端（PySide6）。

只做三件事：
1. 在桌面上画一只透明的宠物（拖拽 / 单击 / 右键菜单）
2. 轮询插件桥（默认 http://127.0.0.1:18790），显示气泡、做动作、播语音
3. 上报心跳与身体事件（点击/拖动/喂食/移动完成）

用法：
    pip install PySide6 httpx
    python pet.py                 # 有控制台
    pythonw pet.py                # 无控制台
"""
from __future__ import annotations

import json
import math
import os
import queue
import random
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, QPropertyAnimation, QRectF, Qt, QTimer, QUrl
from PySide6.QtGui import (QAction, QColor, QCursor, QFont, QFontInfo, QFontMetrics, QPainter,
                           QPen, QPixmap, QPolygonF)
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QSystemTrayIcon, QWidget

try:  # 语音是可选能力
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

    HAS_AUDIO = True
except Exception:  # pragma: no cover - 没装 QtMultimedia 也能跑
    HAS_AUDIO = False

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
SPRITES = ROOT / "sprites"


def _pick_log_path() -> Path | None:
    """挑一个真的能写的日志文件；一个都挑不到就不记文件（绝不因为日志挂掉）。"""
    for base in (os.environ.get("TEMP"), os.environ.get("TMP"), str(ROOT), "."):
        if not base:
            continue
        candidate = Path(base) / "tsukiyo_pet_client.log"
        try:
            with candidate.open("a", encoding="utf-8"):
                return candidate
        except OSError:
            continue
    return None


LOG_PATH = _pick_log_path()
_log_state = {"t": 0.0, "last": ""}


def log(msg: str) -> None:
    """客户端诊断日志：只写能写的目录，1 秒内相同内容只记一次，绝不抛异常、绝不阻塞 UI。"""
    now = time.time()
    text = str(msg)
    if text == _log_state["last"] and now - _log_state["t"] < 1.0:
        return
    _log_state["t"] = now
    _log_state["last"] = text
    line = f"{time.strftime('%H:%M:%S')} {text}\n"
    try:
        print(line, end="", flush=True)
    except Exception:  # pythonw 没有控制台
        pass
    if LOG_PATH is None:
        return
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


DEFAULTS = {
    "client_id": os.environ.get("COMPUTERNAME", "pc") + "-pet",
    "base_url": "http://127.0.0.1:18790",
    "scale": 1.0,
    "poll_ms": 500,
    "heartbeat_s": 5,
    "bubble_ms": 2800,
    "speed_px": 26,
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    except FileNotFoundError:
        CONFIG.write_text(json.dumps(DEFAULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    except json.JSONDecodeError as exc:
        print("config.json 解析失败，用默认值：", exc)
    return cfg


class Bridge:
    """后台线程轮询插件桥，结果丢进队列给 UI 线程。"""

    def __init__(self, base_url: str, client_id: str, poll_ms: int, heartbeat_s: float) -> None:
        self.base = base_url.rstrip("/")
        self.client_id = client_id
        self.poll_ms = max(200, int(poll_ms))
        self.heartbeat_s = max(1.0, float(heartbeat_s))
        self.events: "queue.Queue[dict]" = queue.Queue()
        self.outbox: "queue.Queue[dict]" = queue.Queue()
        self._stop = threading.Event()
        self._last_hb = 0.0
        self.online = False
        self._thread = threading.Thread(target=self._loop, name="pet-bridge", daemon=True)

    # ---------- HTTP ----------
    def _post(self, path: str, payload: dict, timeout: float = 3.0) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _get(self, path: str, timeout: float = 3.0) -> dict:
        with urllib.request.urlopen(self.base + path, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ---------- 线程 ----------
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._post("/pet/unregister", {"client_id": self.client_id}, timeout=1.5)
        except Exception:
            pass

    def _loop(self) -> None:
        screen = {}
        while not self._stop.is_set():
            now = time.time()
            try:
                if not self.online:
                    self._post("/pet/register", {"client_id": self.client_id, "screen": screen,
                                                 "version": "0.1.0"})
                    self.online = True
                    self.events.put({"kind": "_online"})
                self._drain_outbox()
                if now - self._last_hb >= self.heartbeat_s:
                    self._post("/pet/heartbeat", {"client_id": self.client_id, "state": {"mode": "idle"}})
                    self._last_hb = now
                data = self._get(f"/pet/poll?client_id={self.client_id}")
                if not data.get("ok"):
                    self.online = False
                else:
                    for item in data.get("items") or []:
                        self.events.put(item)
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if self.online:
                    self.online = False
                    self.events.put({"kind": "_offline"})
            except Exception as exc:  # pragma: no cover - 兜底，别让线程死掉
                log("桥通信异常：" + str(exc))
                self.online = False
            self._stop.wait(self.poll_ms / 1000.0)

    def _drain_outbox(self) -> None:
        while True:
            try:
                payload = self.outbox.get_nowait()
            except queue.Empty:
                return
            try:
                self._post("/pet/event", payload, timeout=2.0)
            except Exception:
                pass

    def report(self, kind: str, **extra) -> None:
        payload = {"client_id": self.client_id, "kind": kind, "ts": time.time(), **extra}
        self.outbox.put(payload)


class Bubble(QWidget):
    """白色对话气泡：圆角 + 小尾巴 + 淡入淡出，并且跟着桌宠走。

    必须是独立顶层窗口——做成桌宠的子控件会被 140×187 的父窗口裁剪，
    气泡画在窗口外就完全看不见（踩过的坑）。
    """

    PAD_X = 14
    PAD_Y = 11
    RADIUS = 13
    TAIL_H = 9
    TAIL_W = 16
    MAX_W = 300
    MAX_LINES = 6

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self._text = ""
        self._wrapped = ""
        self._tail_x = 40
        # 淡入 / 淡出各用独立动画：共用同一个动画会让 finished 信号串台
        # （淡入结束也会触发 hide，气泡就"一闪而逝"）
        self._fade_in = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_in.setDuration(120)
        self._fade_in.setStartValue(0.0)
        self._fade_in.setEndValue(1.0)
        self._fade_out = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_out.setDuration(140)
        self._fade_out.setStartValue(1.0)
        self._fade_out.setEndValue(0.0)
        self._fade_out.finished.connect(self.hide)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.fade_out)

        font = QFont("Microsoft YaHei UI")
        if not QFontInfo(font).exactMatch():
            font = QFont("Microsoft YaHei")
        font.setPointSize(10)
        self.setFont(font)
        self._metrics = QFontMetrics(font)
        self.hide()

    # ---------- 排版 ----------
    def _layout_text(self, text: str) -> tuple[int, int, str]:
        """按标点分段 → 逐字折行 → 超过 MAX_LINES 省略。返回 (文字宽, 文字高, 折好的文本)。"""
        chunks = [c for c in re.split(r"(?<=[。！？!?；;…\n])", str(text).strip()) if c.strip()]
        if not chunks:
            chunks = [str(text).strip()]

        lines: list[str] = []
        for chunk in chunks:
            cur = ""
            for ch in chunk:
                probe = cur + ch
                if self._metrics.horizontalAdvance(probe) > self.MAX_W:
                    if cur:
                        lines.append(cur)
                    cur = ch
                else:
                    cur = probe
            if cur:
                lines.append(cur)

        if len(lines) > self.MAX_LINES:
            lines = lines[: self.MAX_LINES]
            last = lines[-1]
            while last and self._metrics.horizontalAdvance(last + "…") > self.MAX_W:
                last = last[:-1]
            lines[-1] = last + "…"

        text_w = max((self._metrics.horizontalAdvance(line) for line in lines), default=0)
        text_h = self._metrics.height() * max(1, len(lines))
        return text_w, text_h, "\n".join(lines)

    def set_text(self, text: str) -> None:
        self._text = str(text or "").strip()
        text_w, text_h, wrapped = self._layout_text(self._text)
        self._wrapped = wrapped
        width = min(self.MAX_W, max(24, text_w)) + self.PAD_X * 2
        height = text_h + self.PAD_Y * 2 + self.TAIL_H
        self.resize(int(width), int(height))
        self.update()

    # ---------- 位置 ----------
    def move_near(self, anchor: QPoint) -> None:
        """anchor = 桌宠顶部中点（全局坐标）：气泡浮在它上方，尾巴指向它。"""
        screen = QApplication.primaryScreen().availableGeometry()
        x = anchor.x() - self.width() // 2
        x = max(screen.left() + 4, min(screen.right() - self.width() - 4, x))
        y = anchor.y() - self.height() - 2
        if y < screen.top() + 4:  # 顶到屏幕上沿就翻到下面去
            y = anchor.y() + 4
        self._tail_x = int(max(self.RADIUS + self.TAIL_W,
                               min(self.width() - self.RADIUS - self.TAIL_W, anchor.x() - x)))
        self.move(int(x), int(y))
        self.update()

    # ---------- 绘制 ----------
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        try:
            self._paint(painter)
        except Exception as exc:  # 画崩了不能让整个桌宠跟着挂
            log("气泡绘制失败：" + str(exc))
        finally:
            painter.end()

    def _paint(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        body = QRectF(0, 0, self.width(), self.height() - self.TAIL_H)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(90, 120, 160, 38))
        painter.drawRoundedRect(body.translated(0, 2), self.RADIUS, self.RADIUS)

        painter.setBrush(QColor(255, 255, 255, 246))
        painter.setPen(QPen(QColor(150, 190, 225, 200), 1.2))
        painter.drawRoundedRect(body, self.RADIUS, self.RADIUS)

        tail = QPolygonF([
            QPointF(self._tail_x - self.TAIL_W / 2, body.bottom() - 1),
            QPointF(self._tail_x + self.TAIL_W / 2, body.bottom() - 1),
            QPointF(self._tail_x, body.bottom() + self.TAIL_H),
        ])
        painter.drawPolygon(tail)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 246))
        painter.drawRect(QRectF(self._tail_x - self.TAIL_W / 2 + 1.5, body.bottom() - 2.5,
                                self.TAIL_W - 3, 3))

        painter.setPen(QColor(38, 44, 58))
        text_rect = QRectF(self.PAD_X, self.PAD_Y, self.width() - self.PAD_X * 2,
                           self.height() - self.PAD_Y * 2 - self.TAIL_H)
        painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop),
                         self._wrapped)

    # ---------- 显示 ----------
    def show_text(self, text: str, anchor: QPoint, hold_ms: int) -> None:
        if not str(text or "").strip():
            return
        self.set_text(text)
        self.move_near(anchor)
        self._hide_timer.stop()
        self._fade_out.stop()
        self._fade_in.stop()
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self._fade_in.start()
        self._hide_timer.start(max(800, int(hold_ms)))

    def fade_out(self) -> None:
        if not self.isVisible():
            return
        self._hide_timer.stop()
        self._fade_in.stop()
        self._fade_out.stop()
        self._fade_out.setStartValue(float(self.windowOpacity() or 1.0))
        self._fade_out.start()

    @property
    def visible_text(self) -> str:
        return self._wrapped if self.isVisible() else ""


class Pet(QWidget):
    """桌宠窗口：底图 + 程序化动画（呼吸/眨眼/阴影/惯性/动作曲线）。

    刻意不引入 Live2D：先用「有生命感的程序动画」把静态立绘撑起来，
    素材升级（表情差分/动作视频）走另一条线。
    """

    SHADOW_H = 18          # 底部阴影预留高度
    LIFT_SPACE = 24        # 顶部预留的跳跃空间（不预留会把角色裁掉）
    BREATH_PERIOD = 3.4    # 呼吸周期（秒）
    BREATH_AMP = 2         # 呼吸幅度（像素）

    def __init__(self, cfg: dict) -> None:
        super().__init__()
        self.cfg = cfg
        self.scale = float(cfg.get("scale") or 1.0)
        self.speed = int(cfg.get("speed_px") or 26)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                            | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self.sprites = self._load_sprites()
        self.base_pix = self.sprites["front"]
        self.base_w = self.base_pix.width()
        self.base_h = self.base_pix.height()

        self.label = QLabel(self)
        self.label.setPixmap(self.base_pix)
        self.label.resize(self.base_w, self.base_h)
        self.label.move(0, self.LIFT_SPACE)
        self.resize(self.base_w, self.base_h + self.SHADOW_H + self.LIFT_SPACE)

        self.bubble = Bubble()
        self.mode = "idle"
        self.target: QPoint | None = None
        self.facing = "front"
        self._drag_from: QPoint | None = None
        self._drag_win: QPoint | None = None

        # 动画状态
        self._t0 = time.perf_counter()
        self._act = ""
        self._act_start = 0.0
        self._act_dur = 0.0
        self._act_data: dict = {}
        self._emotion = ""
        self._emotion_until = 0.0
        self._blink_at = time.perf_counter() + random.uniform(2.0, 5.0)
        self._blink_until = 0.0
        self._squash_until = 0.0
        self._fidget_at = time.perf_counter() + random.uniform(12.0, 26.0)
        self._vel = [0.0, 0.0]
        self._last_drag_t = 0.0

        self.bridge = Bridge(str(cfg["base_url"]), str(cfg["client_id"]),
                             int(cfg.get("poll_ms") or 500), float(cfg.get("heartbeat_s") or 5))
        self.bridge.start()

        self.player = None
        if HAS_AUDIO:
            self.player = QMediaPlayer(self)
            self.audio_out = QAudioOutput(self)
            self.audio_out.setVolume(0.9)
            self.player.setAudioOutput(self.audio_out)

        self._move_home()

        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self._tick)
        self.ui_timer.start(16)          # ~60fps，动效才顺

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._drain)
        self.poll_timer.start(int(cfg.get("poll_ms") or 500))

        self._make_tray()

    # ---------- 素材 ----------
    def _load_sprites(self) -> dict[str, QPixmap]:
        out: dict[str, QPixmap] = {}
        mapping = (("front", "正面_187.png"), ("side", "侧面_187.png"),
                   ("back", "背面_187.png"), ("blink", "闭眼_187.png"))
        for key, name in mapping:
            path = SPRITES / name
            if not path.exists():
                continue
            pix = QPixmap(str(path))
            if pix.isNull():
                continue
            if self.scale != 1.0:
                pix = pix.scaled(int(pix.width() * self.scale), int(pix.height() * self.scale),
                                 Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            out[key] = pix
        if "front" not in out:                      # 兜底
            for fallback in ("正面.png", "icon.png"):
                path = SPRITES / fallback
                if path.exists():
                    pix = QPixmap(str(path))
                    if not pix.isNull():
                        out["front"] = pix
                        break
        for key in ("side", "back"):
            out.setdefault(key, out.get("front"))
        return out

    def _move_home(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 60, screen.bottom() - self.height() - 20)

    # ---------- 绘制（阴影）----------
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        try:
            lift = getattr(self, "_lift", 0.0)      # 跳起来的高度，影响阴影
            fade = max(0.25, 1.0 - lift / 26.0)
            w = self.base_w * (0.62 - min(0.18, lift / 140.0))
            h = 10 * fade
            x = self.width() / 2 - w / 2
            y = self.height() - self.SHADOW_H + (self.SHADOW_H - h) / 2
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(30, 40, 60, int(70 * fade)))
            painter.drawEllipse(QRectF(x, y, w, h))
        except Exception as exc:  # 阴影画坏不影响桌宠
            log("阴影绘制失败：" + str(exc))
        finally:
            painter.end()

    # ---------- UI ----------
    def _make_tray(self) -> None:
        icon_path = SPRITES / "icon.png"
        icon = QSystemTrayIcon(QPixmap(str(icon_path)) if icon_path.exists() else self.sprites["front"], self)
        menu = QMenu()
        for text, mode in (("待着", "idle"), ("散步", "wander"), ("跟随鼠标", "follow")):
            act = QAction(text, menu)
            act.triggered.connect(lambda _=False, m=mode: self._set_mode(m))
            menu.addAction(act)
        menu.addSeparator()
        quit_act = QAction("退出", menu)
        quit_act.triggered.connect(QApplication.quit)
        menu.addAction(quit_act)
        icon.setContextMenu(menu)
        icon.setToolTip("月夜宠物")
        icon.show()
        self.tray = icon

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        menu = QMenu(self)
        for text, act in (("跳一下", "jump"), ("摇一摇", "shake"), ("伸个懒腰", "stretch"),
                          ("挥手", "wave"), ("开心", "happy"), ("难过", "sad")):
            a = QAction(text, menu)
            a.triggered.connect(lambda _=False, k=act: self._do_action(k))
            menu.addAction(a)
        menu.addSeparator()
        for text, mode in (("待着", "idle"), ("散步", "wander"), ("跟随鼠标", "follow")):
            a = QAction(text, menu)
            a.triggered.connect(lambda _=False, m=mode: self._set_mode(m))
            menu.addAction(a)
        menu.addSeparator()
        q = QAction("退出", menu)
        q.triggered.connect(QApplication.quit)
        menu.addAction(q)
        menu.exec(event.globalPos())

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = event.globalPosition().toPoint()
            self._drag_win = self.pos()
            self._vel = [0.0, 0.0]
            self._last_drag_t = time.perf_counter()
            self.bridge.report("click", x=self.x(), y=self.y())

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_from is None or self._drag_win is None:
            return
        now = time.perf_counter()
        pos = event.globalPosition().toPoint()
        delta = pos - self._drag_from
        new_pos = self._drag_win + delta
        dt = max(0.001, now - self._last_drag_t)
        self._vel = [(new_pos.x() - self.x()) / dt, (new_pos.y() - self.y()) / dt]
        self._last_drag_t = now
        self.move(new_pos)
        self.target = None

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drag_from is not None:
            self.bridge.report("drag", x=self.x(), y=self.y())
            self._drag_from = None
            self._drag_win = None
            # 甩出去太快就衰减掉，手感更"有重量"
            speed = (self._vel[0] ** 2 + self._vel[1] ** 2) ** 0.5
            if speed > 2600:
                self._vel = [self._vel[0] * 0.05, self._vel[1] * 0.05]
            self._squash_until = time.perf_counter() + 0.18

    # ---------- 行为 ----------
    def _set_mode(self, mode: str) -> None:
        self.mode = mode
        self.bubble.fade_out()
        self.bridge.report("mode", mode=mode)
        self._show_text({"idle": "好呀，我待着。", "wander": "那我到处走走～", "follow": "跟着你走~"}.get(mode, ""))

    def _do_action(self, name: str) -> None:
        """动作/情绪：全部走时间轴插值，不再用整块位移。"""
        name = str(name or "").strip().lower()
        if name in ("happy", "sad", "angry", "surprised", "sleepy"):
            self._emotion = name
            self._emotion_until = time.perf_counter() + 6.0
        dur = {
            "jump": 0.62, "shake": 0.9, "stretch": 1.3, "wave": 1.1,
            "happy": 1.3, "sad": 2.4, "angry": 1.2, "surprised": 0.7, "sleepy": 2.6,
        }.get(name, 0.8)
        self._act = name
        self._act_start = time.perf_counter()
        self._act_dur = dur
        if name in ("stretch", "sleepy"):
            self._set_sprite("back")
            QTimer.singleShot(int(dur * 1000), lambda: self._set_sprite(self.facing))

    def _set_sprite(self, key: str) -> None:
        pix = self.sprites.get(key) or self.sprites["front"]
        self.label.setPixmap(pix)

    def _bubble_anchor(self) -> QPoint:
        """桌宠头顶中点（全局坐标），气泡挂在这儿。"""
        return self.pos() + QPoint(self.width() // 2, 0)

    def _follow_bubble(self) -> None:
        if self.bubble.isVisible():
            self.bubble.move_near(self._bubble_anchor())

    def _show_text(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.bubble.show_text(text, self._bubble_anchor(), int(self.cfg.get("bubble_ms") or 2800))
        self.bridge.report("bubble_shown", x=len(text))

    def _speak(self, url: str) -> None:
        """播放语音：直接流式播放 URL（不落盘）。"""
        if not url:
            log("speak: url 为空")
            return
        if not (HAS_AUDIO and self.player):
            log("speak: 没有播放器")
            return
        log("speak: " + str(url))
        try:
            self.player.setSource(QUrl(url) if url.startswith("http") else QUrl.fromLocalFile(url))
            self.player.play()
        except Exception as exc:  # pragma: no cover
            log("语音播放失败：" + str(exc))

    def _start_wander(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        self.target = QPoint(random.randint(screen.left(), max(screen.left(), screen.right() - self.width())),
                             random.randint(screen.top(), max(screen.top(), screen.bottom() - self.height())))

    # ---------- 每帧 ----------
    def _tick(self) -> None:
        now = time.perf_counter()
        t = now - self._t0
        dx = dy = 0.0

        # 1) 拖动惯性
        if self._drag_from is None:
            vx, vy = self._vel
            if abs(vx) > 2 or abs(vy) > 2:
                nx, ny = self.x() + vx * 0.016, self.y() + vy * 0.016
                screen = QApplication.primaryScreen().availableGeometry()
                nx = max(screen.left(), min(screen.right() - self.width(), nx))
                ny = max(screen.top(), min(screen.bottom() - self.height(), ny))
                self.move(int(nx), int(ny))
                self._vel = [vx * 0.86, vy * 0.86]
            elif self._vel != [0.0, 0.0]:
                self._vel = [0.0, 0.0]

        # 2) 目标移动（缓动逼近，不是匀速）
        if self.target is not None:
            gx, gy = self.target.x() - self.x(), self.target.y() - self.y()
            dist = max(1.0, (gx * gx + gy * gy) ** 0.5)
            if dist <= 3:
                self.move(self.target)
                self.target = None
                self.bridge.report("move_done", x=self.x(), y=self.y())
            else:
                step = self.speed * (0.55 + min(1.0, dist / 240.0))
                self.move(int(self.x() + gx / dist * step), int(self.y() + gy / dist * step))
                if abs(gx) > 4:
                    self.facing = "side"
                    self._set_sprite("side")
        elif self.mode == "wander" and t % 1.0 < 0.02:
            self._start_wander()
        elif self.mode == "follow":
            cursor = QCursor.pos()
            if abs(cursor.x() - self.x()) > 140 or abs(cursor.y() - self.y()) > 90:
                self.target = QPoint(cursor.x() - self.width() // 2, cursor.y() - self.height() // 2)

        # 3) 动作时间轴
        lift = 0.0
        if self._act:
            p = (now - self._act_start) / max(0.01, self._act_dur)
            if p >= 1.0:
                self._act = ""
            else:
                name = self._act
                if name == "jump":
                    lift = 20 * math.sin(math.pi * p)
                elif name == "surprised":
                    lift = 14 * math.sin(math.pi * min(1.0, p * 1.6))
                elif name == "happy":
                    lift = 16 * abs(math.sin(math.pi * p * 2))
                elif name == "shake" or name == "angry":
                    dx = 5 * math.sin(p * math.pi * 8) * (1 - p)
                elif name == "wave":
                    dx = 3 * math.sin(p * math.pi * 10) * (1 - p)
                elif name == "sad":
                    dy = 4 * math.sin(p * math.pi)
                elif name == "sleepy":
                    dy = 2.5 * math.sin(p * math.pi * 2)
        self._lift = lift

        # 4) 呼吸（待着时更明显）
        amp = self.BREATH_AMP * (1.4 if self.mode == "idle" else 1.0)
        if not self._act:
            dy += math.sin(t / self.BREATH_PERIOD * 2 * math.pi) * amp

        # 5) 眨眼
        if "blink" in self.sprites:
            if now >= self._blink_at and now > self._blink_until:
                self._blink_until = now + 0.11
                self._set_sprite("blink")
            elif self._blink_until and now > self._blink_until:
                self._blink_until = 0.0
                self._set_sprite(self.facing)
                self._blink_at = now + random.uniform(2.2, 6.5)

        # 6) 说话时轻微点头
        speaking = False
        if self.player is not None:
            try:
                speaking = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
            except Exception:
                speaking = False
        if speaking:
            dy += 1.5 * math.sin(t * 2 * math.pi * 5.0)

        # 7) 待机小动作
        if not self._act and now > self._fidget_at:
            self._fidget_at = now + random.uniform(14.0, 30.0)
            self._do_action(random.choice(["wave", "stretch", "happy"]))

        # 8) 落地压扁 / 无素材时的"眨眼"（整体轻微压缩）
        scale_y = 1.0
        if now < self._squash_until:
            scale_y = 1.0 - 0.05 * ((self._squash_until - now) / 0.18)
        if self._blink_until and now < self._blink_until and "blink" not in self.sprites:
            scale_y = min(scale_y, 0.93)
        if scale_y < 0.999:
            h = max(1, int(self.base_h * scale_y))
            self.label.setPixmap(self.base_pix.scaled(self.base_w, h,
                                                      Qt.AspectRatioMode.IgnoreAspectRatio,
                                                      Qt.TransformationMode.SmoothTransformation))
        elif self.label.pixmap().height() != self.base_h:
            self._set_sprite("blink" if self._blink_until else self.facing)

        self.label.move(int(dx), int(self.LIFT_SPACE + dy - lift))
        self._follow_bubble()
        self.update()          # 阴影随动作重绘


    def _drain(self) -> None:
        handled = 0
        while handled < 16:
            try:
                item = self.bridge.events.get_nowait()
            except queue.Empty:
                return
            handled += 1
            kind = item.get("kind")
            if kind == "bubble":
                self._show_text(str(item.get("text") or ""))
            elif kind == "action":
                self._do_action(str(item.get("name") or ""))
            elif kind == "move":
                self._move_direction(str(item.get("direction") or ""))
            elif kind == "mode":
                self._set_mode(str(item.get("mode") or "idle"))
            elif kind == "speak":
                if item.get("text"):
                    self._show_text(str(item["text"]))
                self._speak(str(item.get("url") or ""))

    def _move_direction(self, direction: str) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        x, y = self.x(), self.y()
        step = 220
        mapping = {
            "left": (-step, 0), "right": (step, 0), "up": (0, -step), "down": (0, step),
            "left_up": (-step, -step), "right_up": (step, -step),
            "left_down": (-step, step), "right_down": (step, step),
            "center": (None, None),
        }
        if direction == "center":
            self.target = QPoint(screen.center().x() - self.width() // 2,
                                 screen.center().y() - self.height() // 2)
            return
        dx, dy = mapping.get(direction, (0, 0))
        nx = max(screen.left(), min(screen.right() - self.width(), x + dx))
        ny = max(screen.top(), min(screen.bottom() - self.height(), y + dy))
        self.target = QPoint(nx, ny)
        self.facing = "side" if dx else self.facing
        self.label.setPixmap(self.sprites[self.facing])

def main() -> int:
    cfg = load_config()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    pet = Pet(cfg)

    def _bye() -> None:
        pet.bridge.stop()

    app.aboutToQuit.connect(_bye)
    pet.show()
    print(f"[pet] shown={pet.isVisible()} geometry={pet.geometry().getRect()} "
          f"sprite={pet.sprites['front'].size().toTuple()} base_url={cfg['base_url']}", flush=True)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())



