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
        self.label = QLabel(self)
        self.label.setPixmap(self.sprites["front"])
        self.label.resize(self.sprites["front"].size())
        self.resize(self.sprites["front"].size())

        self.bubble = Bubble()
        self.mode = "idle"
        self.target: QPoint | None = None
        self.facing = "front"
        self._drag_from: QPoint | None = None
        self._drag_win: QPoint | None = None
        self._bob = 0
        self._act_until = 0.0
        self._act = ""

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
        self.ui_timer.start(50)

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._drain)
        self.poll_timer.start(int(cfg.get("poll_ms") or 500))


        self._make_tray()

    # ---------- 素材 ----------
    def _load_sprites(self) -> dict[str, QPixmap]:
        out: dict[str, QPixmap] = {}
        for key, name in (("front", "正面_187.png"), ("side", "侧面_187.png"), ("back", "背面_187.png")):
            path = SPRITES / name
            if not path.exists():
                path = SPRITES / "正面.png"
            pix = QPixmap(str(path))
            if self.scale != 1.0 and not pix.isNull():
                pix = pix.scaled(int(pix.width() * self.scale), int(pix.height() * self.scale),
                                 Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            out[key] = pix
        return out

    def _move_home(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 60, screen.bottom() - self.height() - 20)

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
        for text, act in (("跳一下", "jump"), ("摇一摇", "shake"), ("伸个懒腰", "stretch"), ("挥手", "wave")):
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
            self.bridge.report("click", x=self.x(), y=self.y())

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_from is None or self._drag_win is None:
            return
        delta = event.globalPosition().toPoint() - self._drag_from
        self.move(self._drag_win + delta)
        self.target = None

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drag_from is not None:
            self.bridge.report("drag", x=self.x(), y=self.y())
            self._drag_from = None
            self._drag_win = None

    # ---------- 行为 ----------
    def _set_mode(self, mode: str) -> None:
        self.mode = mode
        self.bubble.fade_out()
        self.bridge.report("mode", mode=mode)
        self._show_text({"idle": "好呀，我待着。", "wander": "那我到处走走～", "follow": "跟着你走~"}.get(mode, ""))

    def _do_action(self, name: str) -> None:
        self._act = name
        self._act_until = time.time() + {"jump": 0.6, "shake": 0.9, "stretch": 1.2, "wave": 1.0}.get(name, 0.8)
        if name == "stretch":
            self.label.setPixmap(self.sprites["back"])
            QTimer.singleShot(1200, lambda: self.label.setPixmap(self.sprites[self.facing]))

    def _bubble_anchor(self) -> QPoint:
        """桌宠头顶中点（全局坐标），气泡挂在这儿。"""
        return self.pos() + QPoint(self.width() // 2, 0)

    def _follow_bubble(self) -> None:
        """拖动/散步/跟随时让气泡跟着走。"""
        if self.bubble.isVisible():
            self.bubble.move_near(self._bubble_anchor())

    def _show_text(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        width = min(320, max(160, self.width() + 80))
        self.bubble.show_text(text, self._bubble_anchor(), int(self.cfg.get("bubble_ms") or 2800))
        self.bridge.report("bubble_shown", x=len(text))

    def _speak(self, url: str) -> None:
        """播放语音：优先直接流式播放 URL（不落盘，避免目录不可写的问题）。"""
        if not url:
            log('speak: url 为空')
            return
        if not HAS_AUDIO:
            log('speak: 缺少 QtMultimedia，装 PySide6 完整版')
            return
        if not self.player:
            log('speak: 播放器未初始化')
            return
        log('speak: ' + str(url))
        try:
            if url.startswith("http"):
                self.player.setSource(QUrl(url))
            else:
                self.player.setSource(QUrl.fromLocalFile(url))
            self.player.play()
            log('speak: play() 之后 state=' + str(self.player.playbackState()))
        except Exception as exc:  # pragma: no cover - 播放失败不影响气泡
            log("语音播放失败：" + str(exc))

    def _start_wander(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        self.target = QPoint(random.randint(screen.left(), max(screen.left(), screen.right() - self.width())),
                             random.randint(screen.top(), max(screen.top(), screen.bottom() - self.height())))

    def _tick(self) -> None:
        now = time.time()
        # 动作：上下小跳
        if self._act and now < self._act_until:
            name = self._act
            if name == "jump":
                self.label.move(0, -abs(int(10 * (1 - (self._act_until - now) / 0.6))))
            elif name == "shake":
                self.label.move(int(6 * (1 if int(now * 20) % 2 else -1)), 0)
            elif name == "wave":
                self.label.move(int(4 * (1 if int(now * 8) % 2 else -1)), 0)
        elif self._act:
            self._act = ""
            self.label.move(0, 0)

        # 移动
        if self.target is not None:
            dx = self.target.x() - self.x()
            dy = self.target.y() - self.y()
            if abs(dx) <= self.speed and abs(dy) <= self.speed:
                self.move(self.target)
                self.target = None
                self.bridge.report("move_done", x=self.x(), y=self.y())
            else:
                self.move(self.x() + max(-self.speed, min(self.speed, dx)),
                          self.y() + max(-self.speed, min(self.speed, dy)))
                want = "side" if abs(dx) > 3 else self.facing
                if want != self.facing:
                    self.facing = want
                    self.label.setPixmap(self.sprites[want])
        elif self.mode == "wander" and random.random() < 0.01:
            self._start_wander()
        elif self.mode == "follow":
            cursor = QCursor.pos()
            if abs(cursor.x() - self.x()) > 120 or abs(cursor.y() - self.y()) > 80:
                self.target = QPoint(cursor.x() - self.width() // 2, cursor.y() - self.height() // 2)

        # 待着时的呼吸
        if self.mode == "idle" and not self._act:
            self._bob = (self._bob + 1) % 60
            self.label.move(0, -1 if self._bob < 30 else 0)

        # 气泡跟着桌宠走（拖动、散步、跟随都覆盖）
        self._follow_bubble()

        # 桥通知
        if not self.bridge.online and now - getattr(self, "_offline_hint", 0) > 30:
            self._offline_hint = now
            log("没连上插件桥，检查 base_url：" + str(self.cfg.get("base_url")))

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



