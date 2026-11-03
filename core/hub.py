"""桥的中枢：客户端注册表 + 每客户端指令队列 + 近期事件。

线程安全：HTTP 线程写、AstrBot 事件循环线程写，都用同一把锁。
不 import AstrBot，可单测。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque

from .protocol import Item

ONLINE_WINDOW = 15.0  # 秒；超过没心跳就算离线
MAX_QUEUE = 32
MAX_EVENTS = 50


class Client:
    __slots__ = ("client_id", "screen", "state", "last_seen", "queue", "registered_at")

    def __init__(self, client_id: str, screen: dict[str, Any] | None = None) -> None:
        self.client_id = client_id
        self.screen = screen or {}
        self.state: dict[str, Any] = {"x": 0, "y": 0, "mode": "idle", "size": 187}
        self.last_seen = time.time()
        self.registered_at = time.time()
        self.queue: Deque[Item] = deque(maxlen=MAX_QUEUE)

    def touch(self) -> None:
        self.last_seen = time.time()

    @property
    def online(self) -> bool:
        return (time.time() - self.last_seen) <= ONLINE_WINDOW

    def as_dict(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "online": self.online,
            "screen": self.screen,
            "state": self.state,
            "queue": len(self.queue),
            "last_seen": round(self.last_seen, 3),
        }


class Hub:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._clients: dict[str, Client] = {}
        self._events: Deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)

    # ---------- 客户端生命周期 ----------
    def register(self, client_id: str, screen: dict[str, Any] | None = None) -> Client:
        cid = str(client_id or "").strip()
        if not cid:
            raise ValueError("client_id 不能为空")
        with self._lock:
            client = self._clients.get(cid)
            if client is None:
                client = Client(cid, screen)
                self._clients[cid] = client
            else:
                client.touch()
                if screen:
                    client.screen = screen
            return client

    def heartbeat(self, client_id: str, state: dict[str, Any] | None = None) -> bool:
        with self._lock:
            client = self._clients.get(str(client_id or ""))
            if client is None:
                return False
            client.touch()
            if isinstance(state, dict):
                client.state.update({k: v for k, v in state.items() if v is not None})
            return True

    def unregister(self, client_id: str) -> bool:
        with self._lock:
            return self._clients.pop(str(client_id or ""), None) is not None

    def online_clients(self) -> list[Client]:
        with self._lock:
            return [c for c in self._clients.values() if c.online]

    def primary(self) -> Client | None:
        """优先最近活跃的在线客户端。"""
        online = self.online_clients()
        if not online:
            return None
        return max(online, key=lambda c: c.last_seen)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "clients": [c.as_dict() for c in self._clients.values()],
                "online": len([c for c in self._clients.values() if c.online]),
                "events": list(self._events)[-10:],
            }

    # ---------- 指令队列 ----------
    def push(self, item: Item | None, client_id: str | None = None) -> bool:
        if item is None:
            return False
        with self._lock:
            target = self._clients.get(str(client_id)) if client_id else self.primary()
            if target is None:
                return False
            target.queue.append(item)
            return True

    def poll(self, client_id: str) -> dict[str, Any]:
        """客户端拉取：返回积压指令 + 当前状态，顺带续期。"""
        cid = str(client_id or "")
        with self._lock:
            client = self._clients.get(cid)
            if client is None:
                return {"ok": False, "items": [], "state": {}}
            client.touch()
            items = [i.as_dict() for i in client.queue]
            client.queue.clear()
            return {"ok": True, "items": items, "state": dict(client.state)}

    def broadcast(self, item: Item | None) -> int:
        if item is None:
            return 0
        with self._lock:
            n = 0
            for client in self._clients.values():
                client.queue.append(item)
                n += 1
            return n

    # ---------- 事件 ----------
    def record(self, client_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            row = {"client_id": str(client_id or ""), **event}
            self._events.append(row)
            client = self._clients.get(str(client_id or ""))
            if client is not None and event.get("kind") in ("move_done", "mode"):
                for key in ("x", "y", "mode"):
                    if event.get(key) is not None:
                        client.state[key] = event[key]

    def recent_events(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)[-limit:]

    def prune(self, window: float = 3600.0) -> int:
        """清掉离线超过 window 秒的客户端，避免长期堆积。"""
        now = time.time()
        with self._lock:
            dead = [cid for cid, c in self._clients.items() if now - c.last_seen > window]
            for cid in dead:
                self._clients.pop(cid, None)
            return len(dead)
