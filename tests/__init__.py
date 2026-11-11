"""测试公共夹具：在回环端口启动真实本地源站，加载虚构对象。"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
import uuid

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from rec.origin import OriginServer, build_server  # noqa: E402

DATA_DIR = os.path.join(PROJECT_DIR, "data")
OBJECTS_DIR = os.path.join(DATA_DIR, "objects")
MANIFEST_PATH = os.path.join(DATA_DIR, "manifest.json")


def ensure_dataset() -> dict:
    if not os.path.isdir(OBJECTS_DIR) or not os.path.exists(MANIFEST_PATH):
        from rec.generator import generate_all

        generate_all(DATA_DIR)
    with open(MANIFEST_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


class OriginTestBase(unittest.TestCase):
    """每个测试类一个独立源站（端口动态分配），场景计数互不串扰。"""

    server = None
    port = 0
    origin = None
    manifest: dict = {}
    thread = None

    @classmethod
    def setUpClass(cls) -> None:
        # 关键：给每个子类独立的类属性，避免沿继承链共享可变字典/对象
        cls.manifest = ensure_dataset()
        cls.origin = OriginServer()
        cls.origin.load_objects(OBJECTS_DIR)
        cls.server = build_server(cls.origin, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True,
        )
        cls.thread.start()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.server is not None:
            cls.server.shutdown()
            cls.server.server_close()
            if cls.thread is not None:
                cls.thread.join(timeout=5)
        cls.server = None
        cls.origin = None

    def setUp(self) -> None:
        self.assertIsNotNone(self.__class__.origin)

    def url_for(self, token: str, name: str) -> str:
        return f"http://127.0.0.1:{self.port}/origin/s/{token}/{name}"

    def unique_token(self, label: str) -> str:
        # 每个测试方法独立令牌，避免类属性共享导致场景互相覆盖
        return f"t{label}-{uuid.uuid4().hex[:10]}"

    def configure(self, token: str, name: str, scenario: str,
                  replacement: str | None = None) -> str:
        self.origin.configure(token, name, scenario, replacement)
        return self.url_for(token, name)

    def expected(self, name: str) -> dict:
        return self.manifest["objects"][name]["summary"]
