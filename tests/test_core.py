"""核心逻辑单测：不依赖 AstrBot、不依赖 PySide6、不联网。

跑法：python -m unittest discover -s tests -p "test_*.py"
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO.parent) not in sys.path:
    sys.path.insert(0, str(REPO.parent))

# 测试用的临时目录：优先环境变量，其次系统临时目录（某些沙箱下仓库目录不可写）
TEST_TMP = Path(os.environ.get("TSUKIYO_PET_TMP") or tempfile.gettempdir())
TEST_TMP.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(TEST_TMP)

from astrbot_plugin_tsukiyo_pet.core import protocol  # noqa: E402
from astrbot_plugin_tsukiyo_pet.core.hub import Hub  # noqa: E402
from astrbot_plugin_tsukiyo_pet.core.httpapi import PetHTTP  # noqa: E402
from astrbot_plugin_tsukiyo_pet.core.owner import OwnerGuard  # noqa: E402
from astrbot_plugin_tsukiyo_pet.core.tts import TTSError, build_payload  # noqa: E402


class TestProtocol(unittest.TestCase):
    def test_action_whitelist(self) -> None:
        self.assertEqual(protocol.action("jump").kind, "action")
        self.assertEqual(protocol.action("LEFT").payload["direction"], "left")
        self.assertEqual(protocol.action("follow").payload["mode"], "follow")
        self.assertIsNone(protocol.action("自爆"))

    def test_bubble_empty(self) -> None:
        self.assertIsNone(protocol.bubble("   "))
        self.assertEqual(protocol.bubble("你好")["text"], "你好")

    def test_bubble_truncates(self) -> None:
        item = protocol.bubble("啊" * 400)
        self.assertEqual(len(item.payload["text"]), protocol.MAX_TEXT)

    def test_clamp_text_splits(self) -> None:
        out = protocol.clamp_text("第一句。第二句！第三句", limit=40)
        self.assertIn("\n", out)
        self.assertEqual(out.count("\n"), 2)

    def test_parse_event(self) -> None:
        ev = protocol.parse_event({"kind": "click", "x": 10, "y": 20, "ts": 1.5})
        self.assertEqual(ev["kind"], "click")
        self.assertEqual(ev["x"], 10)
        with self.assertRaises(ValueError):
            protocol.parse_event({"kind": "hack"})
        with self.assertRaises(ValueError):
            protocol.parse_event("not-a-dict")  # type: ignore[arg-type]


class TestOwner(unittest.TestCase):
    def test_explicit_owner(self) -> None:
        guard = OwnerGuard("1C1F6D70A62AF46129E3A26AF3CFE9A8")
        self.assertTrue(guard.is_owner("1C1F6D70A62AF46129E3A26AF3CFE9A8", is_private=True))
        self.assertFalse(guard.is_owner("2022061000", is_private=True))

    def test_group_never_owner(self) -> None:
        guard = OwnerGuard("123")
        self.assertFalse(guard.is_owner("123", is_private=False))

    def test_bind_first_when_empty(self) -> None:
        guard = OwnerGuard("")
        self.assertTrue(guard.is_owner("AAA", is_private=True))
        self.assertFalse(guard.is_owner("BBB", is_private=True))
        self.assertEqual(guard.bound, "AAA")


class TestHub(unittest.TestCase):
    def test_register_push_poll(self) -> None:
        hub = Hub()
        hub.register("c1", {"w": 1920, "h": 1080})
        self.assertEqual(len(hub.online_clients()), 1)
        self.assertTrue(hub.push(protocol.bubble("hi")))
        data = hub.poll("c1")
        self.assertTrue(data["ok"])
        self.assertEqual(data["items"][0]["kind"], "bubble")
        # 拉过一次就清空
        self.assertEqual(hub.poll("c1")["items"], [])

    def test_push_without_client(self) -> None:
        hub = Hub()
        self.assertFalse(hub.push(protocol.bubble("hi")))

    def test_heartbeat_and_events(self) -> None:
        hub = Hub()
        hub.register("c1")
        self.assertTrue(hub.heartbeat("c1", {"mode": "wander"}))
        self.assertEqual(hub.primary().state["mode"], "wander")
        hub.record("c1", {"kind": "move_done", "x": 5, "y": 6})
        self.assertEqual(hub.primary().state["x"], 5)
        self.assertEqual(hub.recent_events(1)[0]["kind"], "move_done")

    def test_queue_capped(self) -> None:
        hub = Hub()
        hub.register("c1")
        for i in range(100):
            hub.push(protocol.bubble(f"m{i}"))
        self.assertLessEqual(hub.poll("c1")["items"].__len__(), 32)

    def test_prune(self) -> None:
        hub = Hub()
        hub.register("c1")
        hub._clients["c1"].last_seen = time.time() - 7200
        self.assertEqual(hub.prune(3600), 1)
        self.assertEqual(hub.online_clients(), [])


class TestHTTP(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = Hub()
        self.tmp = tempfile.TemporaryDirectory()
        self.api = PetHTTP(self.hub, host="127.0.0.1", port=0, audio_dir=Path(self.tmp.name))
        self.api.start()
        self.port = self.api._server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.api.stop()
        self.tmp.cleanup()

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(self.base + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path, timeout=5) as resp:
            return json.loads(resp.read().decode())

    def test_register_poll_roundtrip(self) -> None:
        self.assertTrue(self._post("/pet/register", {"client_id": "c1", "screen": {"w": 800}}).get("ok"))
        self.hub.push(protocol.bubble("hello"))
        data = self._get("/pet/poll?client_id=c1")
        self.assertTrue(data["ok"])
        self.assertEqual(data["items"][0]["text"], "hello")
        self.assertEqual(self._get("/pet/health")["online"], 1)

    def test_event_and_audio(self) -> None:
        self._post("/pet/register", {"client_id": "c1"})
        self.assertTrue(self._post("/pet/event", {"client_id": "c1", "kind": "click", "x": 1}).get("ok"))
        (Path(self.tmp.name) / "a.wav").write_bytes(b"RIFF" + b"0" * 2048)
        with urllib.request.urlopen(f"{self.base}/pet/audio/a.wav", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertTrue(resp.read().startswith(b"RIFF"))

    def test_unknown_event_rejected(self) -> None:
        with self.assertRaises(urllib.error.HTTPError):
            self._post("/pet/event", {"client_id": "c1", "kind": "nope"})


class TestTTS(unittest.TestCase):
    def test_payload_requires_ref(self) -> None:
        with self.assertRaises(TTSError):
            build_payload("hi", {})

    def test_payload_defaults(self) -> None:
        payload = build_payload("你好", {"ref_audio_path": "/x/y.wav", "prompt_text": "p"})
        self.assertEqual(payload["text_lang"], "zh")
        self.assertEqual(payload["speed_factor"], 0.9)
        self.assertEqual(payload["text_split_method"], "cut0")
        self.assertEqual(payload["media_type"], "wav")

    def test_payload_carries_sampling_params(self) -> None:
        """不传采样参数时服务端 temperature=1.0，语调会飘、偶发吞字，必须显式给默认值。"""
        payload = build_payload("你好", {"ref_audio_path": "/x/y.wav"})
        self.assertEqual(payload["temperature"], 0.85)
        self.assertEqual(payload["top_k"], 12)
        self.assertEqual(payload["repetition_penalty"], 1.35)
        self.assertEqual(payload["seed"], -1)

    def test_payload_sampling_overrides(self) -> None:
        payload = build_payload("你好", {"ref_audio_path": "/x/y.wav",
                                         "temperature": 0.7, "top_k": 10, "seed": 42})
        self.assertEqual(payload["temperature"], 0.7)
        self.assertEqual(payload["top_k"], 10)
        self.assertEqual(payload["seed"], 42)

    def test_payload_bad_number_falls_back(self) -> None:
        payload = build_payload("你好", {"ref_audio_path": "/x/y.wav", "temperature": "abc"})
        self.assertEqual(payload["temperature"], 0.85)


class TestConcurrency(unittest.TestCase):
    def test_many_threads_push_poll(self) -> None:
        hub = Hub()
        hub.register("c1")

        def worker() -> None:
            for _ in range(50):
                hub.push(protocol.bubble("x"))
                hub.poll("c1")

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertGreaterEqual(len(hub.recent_events(1)) + 1, 1)


if __name__ == "__main__":
    unittest.main()
