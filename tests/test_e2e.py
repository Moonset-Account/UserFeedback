"""端到端验证：真实启动 Web 服务，经 HTTP 公开接口采集，并验证 run-demo 流程。

采集请求使用 http://127.0.0.1:<port>/source/*.ics 作为来源，
与浏览器操作路径一致；测试不直接读取真值目录参与采集。
"""

from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.request
from urllib.error import HTTPError

from samples.generator import PRESETS
from webapp.server import create_server


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(url: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class EndToEndTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_01_index_page_served(self):
        with urllib.request.urlopen(f"{self.base}/", timeout=10) as resp:
            html = resp.read().decode("utf-8")
        self.assertIn('id="run-demo"', html)
        self.assertIn("iCalendar 采集器", html)

    def test_02_source_ics_publicly_served(self):
        preset = PRESETS["demo_boundary"]
        with urllib.request.urlopen(
                f"{self.base}/source/{preset['file']}", timeout=10) as resp:
            self.assertEqual(resp.headers.get("Content-Type"),
                             "text/calendar; charset=utf-8")
            self.assertIn("BEGIN:VCALENDAR", resp.read().decode("utf-8"))

    def test_03_path_traversal_blocked(self):
        with self.assertRaises(HTTPError) as ctx:
            urllib.request.urlopen(f"{self.base}/source/../app.py", timeout=10)
        self.assertEqual(ctx.exception.code, 404)

    def test_04_collect_via_http_matches_expectations(self):
        preset = PRESETS["demo_boundary"]
        status, result = _post(f"{self.base}/api/collect", {
            "source": f"/source/{preset['file']}",
            "range_start": preset["range_start"],
            "range_end": preset["range_end"],
            "target_name": "e2e_boundary",
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["source_evidence"]["kind"], "file")
        self.assertTrue(result["source_evidence"]["fetched_ok"])

        # 取消 + 移动 + EXDATE + DST 全链路
        by_rid = {i["instance_id"]: i for i in result["instances"]}
        cancelled = next(i for i in result["instances"]
                         if i["status"] == "CANCELLED")
        self.assertEqual(cancelled["origin"], "override_cancel")
        moved = next(i for i in result["instances"]
                     if i["origin"] == "override_move")
        self.assertEqual(moved["start"], "2026-11-13T19:00:00")
        self.assertIn(
            "fictional-hilltop-stargazing-forever::dt:2026-11-14T20:00:00|off:-300",
            by_rid)
        # DST 证据
        before = next(i for i in result["instances"]
                      if i["start"] == "2026-10-24T20:00:00")
        self.assertEqual(before["start_utc"], "2026-10-25T00:00:00+00:00")

    def test_05_run_demo_runs_both_scenarios_quickly(self):
        started = time.monotonic()
        status, payload = _post(f"{self.base}/api/run-demo", {})
        elapsed = time.monotonic() - started
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["runs"]), 2)
        self.assertLess(elapsed, 15, "run-demo 应在 15 秒内结束")
        normal, boundary = payload["runs"]
        self.assertEqual(normal["status"], "success")
        self.assertGreaterEqual(normal["stats"]["in_range"], 6)
        self.assertEqual(boundary["status"], "success")
        self.assertTrue(any(
            i["status"] == "CANCELLED" for i in boundary["instances"]))

    def test_06_bad_source_returns_structured_error(self):
        status, result = _post(f"{self.base}/api/collect", {
            "source": "/source/does_not_exist.ics",
            "range_start": "2026-11-01",
            "range_end": "2026-11-10",
        })
        self.assertEqual(status, 422)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(i["code"] == "FETCH_ERROR" for i in result["issues"]))

    def test_07_unsupported_scheme_rejected(self):
        status, result = _post(f"{self.base}/api/collect", {
            "source": "ftp://example.invalid/calendar.ics",
            "range_start": "2026-11-01",
            "range_end": "2026-11-10",
        })
        self.assertEqual(status, 422)
        self.assertTrue(
            any(i["code"] == "UNSUPPORTED_SCHEME" for i in result["issues"]))

    def test_08_history_endpoint_records_runs(self):
        runs = _get(f"{self.base}/api/runs")["runs"]
        self.assertGreaterEqual(len(runs), 3)  # collect + 两个 demo 运行
        run_id = runs[-1]["run_id"]
        detail = _get(f"{self.base}/api/runs/{run_id}")
        self.assertIn("instances", detail)


if __name__ == "__main__":
    unittest.main()
