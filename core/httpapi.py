"""桌宠桥的 HTTP 接口（客户端 → 插件 的入口）。

只用标准库，跑在独立线程里，绑 0.0.0.0 以便 Docker 端口映射。

  POST /pet/register    {client_id, screen, version}
  POST /pet/heartbeat   {client_id, state}
  GET  /pet/poll?client_id=xxx      → {ok, items, state}
  POST /pet/event       {client_id, kind, ...}
  POST /pet/unregister  {client_id}
  POST /pet/push        {text|action|mode, token}   调试/联动用：直接往桌宠推一条
  GET  /pet/health                  → {ok, online, clients}
  GET  /pet/audio/<file>            → wav 文件（语音）
"""
from __future__ import annotations

import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .hub import Hub
from .protocol import parse_event


class PetHTTP:
    def __init__(self, hub: Hub, *, host: str = "0.0.0.0", port: int = 18790,
                 audio_dir: Path | None = None, push_token: str = "",
                 log: Callable[[str], None] | None = None) -> None:
        self.hub = hub
        self.host = host
        self.port = int(port)
        self.audio_dir = Path(audio_dir) if audio_dir else None
        self.push_token = str(push_token or "")
        self._log = log or (lambda _msg: None)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if self._server is not None:
            return
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认访问日志
                return

            # ---- 工具 ----
            def _body(self) -> dict[str, Any]:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    length = 0
                if length <= 0:
                    return {}
                raw = self.rfile.read(length)
                try:
                    data = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return {}
                return data if isinstance(data, dict) else {}

            def _send(self, code: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _file(self, name: str) -> None:
                if api.audio_dir is None:
                    self._send(404, {"ok": False, "error": "未启用音频"})
                    return
                safe = Path(name).name
                target = (api.audio_dir / safe).resolve()
                if target.parent != api.audio_dir.resolve() or not target.is_file():
                    self._send(404, {"ok": False, "error": "没有这个文件"})
                    return
                data = target.read_bytes()
                ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            # ---- 路由 ----
            def do_GET(self) -> None:
                path = urlparse(self.path).path
                query = parse_qs(urlparse(self.path).query)
                if path == "/pet/health":
                    snap = api.hub.snapshot()
                    self._send(200, {"ok": True, "online": snap["online"], "clients": snap["clients"]})
                    return
                if path == "/pet/poll":
                    cid = (query.get("client_id") or [""])[0]
                    self._send(200, api.hub.poll(cid))
                    return
                if path.startswith("/pet/audio/"):
                    self._file(path[len("/pet/audio/"):])
                    return
                self._send(404, {"ok": False, "error": "未知路径"})

            def do_POST(self) -> None:
                path = urlparse(self.path).path
                body = self._body()
                if path == "/pet/register":
                    try:
                        client = api.hub.register(body.get("client_id"), body.get("screen") or {})
                    except ValueError as exc:
                        self._send(400, {"ok": False, "error": str(exc)})
                        return
                    api._log(f"客户端上线：{client.client_id}")
                    self._send(200, {"ok": True, "client_id": client.client_id})
                    return
                if path == "/pet/heartbeat":
                    ok = api.hub.heartbeat(body.get("client_id"), body.get("state") or {})
                    self._send(200 if ok else 404, {"ok": ok})
                    return
                if path == "/pet/unregister":
                    ok = api.hub.unregister(body.get("client_id"))
                    api._log(f"客户端下线：{body.get('client_id')}")
                    self._send(200, {"ok": ok})
                    return
                if path == "/pet/event":
                    try:
                        event = parse_event(body)
                    except ValueError as exc:
                        self._send(400, {"ok": False, "error": str(exc)})
                        return
                    api.hub.record(body.get("client_id") or "", event)
                    self._send(200, {"ok": True})
                    return
                if path == "/pet/push":
                    token = str(body.get("token") or "")
                    if api.push_token and token != api.push_token:
                        self._send(403, {"ok": False, "error": "token 不对"})
                        return
                    from . import protocol as _protocol

                    item = None
                    if body.get("text"):
                        item = _protocol.bubble(str(body["text"]))
                    elif body.get("action"):
                        item = _protocol.action(str(body["action"]))
                    if item is None:
                        self._send(400, {"ok": False, "error": "要有 text 或 action"})
                        return
                    queued = api.hub.push(item, body.get("client_id"))
                    self._send(200 if queued else 503, {"ok": queued})
                    return
                self._send(404, {"ok": False, "error": "未知路径"})

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="tsukiyo-pet-http", daemon=True)
        self._thread.start()
        self._log(f"桌宠桥已监听 {self.host}:{self.port}")

    def stop(self) -> None:
        if self._server is None:
            return
        try:
            self._server.shutdown()
            self._server.server_close()
        finally:
            self._server = None
            self._thread = None
            self._log("桌宠桥已停止")
