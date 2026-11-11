"""Web 任务管理器的重启恢复测试（真实 HTTP 服务 + 真实引擎）。"""

import json
import os
import tempfile
import threading
import time
import unittest

from rec.engine import Engine
from rec.web import TaskManager
from rec.origin import OriginServer, build_server


class TestTaskManagerRecovery(unittest.TestCase):
    def setUp(self) -> None:
        self.data_dir = tempfile.mkdtemp(prefix="rpa-recover-")
        # 直接复用项目生成器产物
        project_data = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data",
        )
        os.makedirs(os.path.join(self.data_dir, "objects"), exist_ok=True)
        objects_src = os.path.join(project_data, "objects")
        for name in os.listdir(objects_src):
            with open(os.path.join(objects_src, name), "rb") as src, \
                    open(os.path.join(self.data_dir, "objects", name), "wb") as dst:
                dst.write(src.read())

        self.origin = OriginServer()
        self.origin.load_objects(os.path.join(self.data_dir, "objects"))
        self.server = build_server(self.origin, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True,
        )
        self.thread.start()
        time.sleep(0.05)
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.manager = TaskManager(self.data_dir, self.origin, self.base_url)

    def tearDown(self) -> None:
        for engine in list(self.manager.engines.values()):
            engine.stop()
        self.server.shutdown()
        self.server.server_close()

    def test_pause_then_new_manager_resumes(self) -> None:
        # 手工装配任务，确保暂停钩子在启动前就位
        import uuid

        token = "t" + uuid.uuid4().hex[:12]
        self.origin.configure(token, "large-v1.expb", "benign")
        task_id = time.strftime("%Y%m%d%H%M%S") + "-recover"
        url = f"{self.base_url}/origin/s/{token}/large-v1.expb"
        task_dir = os.path.join(self.data_dir, "tasks", task_id)
        engine = Engine(task_dir, url, "恢复测试", chunk_size=2048,
                        pause_after_accepts=2)
        self.manager.engines[task_id] = engine
        self.manager.meta[task_id] = {
            "label": "恢复测试", "created_at": time.time(),
        }
        self.manager.start(task_id)
        deadline = time.time() + 10
        while time.time() < deadline:
            snap = self.manager.describe(task_id)
            if snap["status"] == "paused":
                break
            time.sleep(0.05)
        snap1 = self.manager.describe(task_id)
        self.assertEqual(snap1["status"], "paused")
        self.assertGreater(snap1["covered"], 0)
        self.assertLess(snap1["covered"], snap1["expected_length"])

        # 模拟进程重启：丢弃全部内存管理器，仅靠磁盘任务目录重建
        manager2 = TaskManager(self.data_dir, self.origin, self.base_url)
        self.assertIn(task_id, manager2.meta)
        # 恢复出的任务被标记为 recovered，且不会自动运行
        snap2 = manager2.describe(task_id)
        self.assertTrue(snap2["recovered"])
        self.assertEqual(snap2["status"], "paused")
        # 通过“继续”动作在新管理器中启动续取
        self.assertTrue(manager2.start(task_id))
        deadline = time.time() + 20
        while time.time() < deadline:
            snap = manager2.describe(task_id)
            if snap["status"] in ("complete", "failed", "stopped"):
                break
            time.sleep(0.1)
        snap3 = manager2.describe(task_id)
        self.assertEqual(snap3["status"], "complete")
        self.assertIsNotNone(snap3["result"])
        self.assertEqual(snap3["gaps"], [])

    def test_running_state_after_crash_recovers_to_paused(self) -> None:
        info = self.manager.create_task(
            "drop-first", "tiny-v1.expb", None, 512, "崩溃恢复",
        )
        task_id = info["task_id"]
        # 直接把磁盘状态改成 running，模拟进程被强杀时的遗留状态
        state_path = os.path.join(
            self.data_dir, "tasks", task_id, "state.json",
        )
        runner = threading.Thread(
            target=self.manager.engines[task_id].run, daemon=True,
        )
        runner.start()
        time.sleep(0.2)
        self.manager.engines[task_id].stop()
        runner.join(timeout=5)
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
        state["status"] = "running"
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        # 新管理器装配引擎时必须把 running 回收为 paused
        manager2 = TaskManager(self.data_dir, self.origin, self.base_url)
        engine2 = Engine(
            os.path.join(self.data_dir, "tasks", task_id),
            state["url"], "crash", chunk_size=512,
        )
        self.assertEqual(engine2.snapshot()["status"], "paused")
        self.assertTrue(manager2.describe(task_id)["recovered"])


if __name__ == "__main__":
    unittest.main()
