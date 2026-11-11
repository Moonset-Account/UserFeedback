"""Playwright 浏览器端到端测试。

验证：
- 统一入口 python3 app.py --host 127.0.0.1 --port <port> 启动后根路径可操作；
- id=run-demo 的“运行示例”按钮依次执行正常与边界场景，15 秒内结束；
- 三栏（订阅源片段 / 采集条目 / 链接用途）有真实数据；
- 冲突卡片与具体错误显式展示，没有静默覆盖；
- 任务列表保留两个场景的结果可切换查看。

测试通过 API 精确获取本次 run_id 后再在页面点选，避免历史同名任务干扰。
未安装 Playwright 浏览器时本模块整体 skip（README 已给出安装命令）。
"""
import json
import subprocess
import sys
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE_PORT = 8791

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:  # pragma: no cover
    PLAYWRIGHT_AVAILABLE = False


def _wait_port(port: int, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/scenarios", timeout=1) as resp:
                if resp.status == 200:
                    return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError(f"服务在 {timeout}s 内未就绪")


def _get_json(url: str):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


@unittest.skipUnless(PLAYWRIGHT_AVAILABLE,
                     "未安装 Playwright（pip install -r requirements-dev.txt "
                     "&& python -m playwright install chromium）")
class WebAppPlaywrightTests(unittest.TestCase):
    proc = None

    @classmethod
    def setUpClass(cls):
        # 优先用固定端口；若被占用（如上次进程残留）则自动挑空闲端口
        import socket
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", BASE_PORT))
            cls.port = BASE_PORT
        except OSError:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        finally:
            sock.close()
        cls.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "app.py"),
             "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_port(cls.port)
        except Exception:
            out = cls.proc.stdout.read() if cls.proc.stdout else ""
            cls.proc.kill()
            raise RuntimeError(f"服务启动失败：\n{out}")
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            cls.proc.kill()

    def setUp(self):
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(headless=True)
        self.page = self.browser.new_page(viewport={"width": 1280, "height": 720})

    def tearDown(self):
        self.browser.close()
        self.pw.stop()

    def _goto(self):
        self.page.goto(f"{self.base}/", wait_until="networkidle")

    def _run_ids_before(self):
        return {r["run_id"] for r in _get_json(f"{self.base}/api/runs")["runs"]}

    def _wait_new_run_id(self, before: set, timeout_ms: int = 15000):
        """轮询直到出现一个新任务并进入终态，返回其 run_id。"""
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            runs = _get_json(f"{self.base}/api/runs")["runs"]
            new = [r for r in runs if r["run_id"] not in before]
            terminal = [r for r in new if r["status"]
                        in ("success", "completed_with_errors", "failed")]
            if terminal:
                return terminal[0]["run_id"]
            time.sleep(0.2)
        raise AssertionError("等待新任务进入终态超时")

    def _click_run_by_id(self, run_id: str):
        selector = f"li[data-id='{run_id}']"
        self.page.wait_for_selector(selector, timeout=8000)
        self.page.click(selector)
        # 等待详情区真正切换到该任务（active 标记落到被选项上）
        self.page.wait_for_selector(
            f"li.active[data-id='{run_id}']", timeout=5000)

    def _wait_two_new_runs(self, before: set):
        new_runs = []
        deadline = time.time() + 15
        while time.time() < deadline and len(new_runs) < 2:
            runs = _get_json(f"{self.base}/api/runs")["runs"]
            new_runs = [r for r in runs if r["run_id"] not in before
                        and r["status"] in
                        ("success", "completed_with_errors", "failed")]
            time.sleep(0.2)
        self.assertEqual(len(new_runs), 2, "15 秒内未完成两个示例任务")
        return new_runs

    def test_01_run_demo_completes_within_15s_and_renders_three_panes(self):
        self._goto()
        # 首屏可见性
        self.page.wait_for_selector("#run-demo", state="visible")
        for sel in ("#run-list", "#feed-pane", "#catalog-pane", "#links-pane"):
            self.assertTrue(self.page.is_visible(sel))

        before = self._run_ids_before()
        started = time.time()
        self.page.click("#run-demo")
        new_runs = self._wait_two_new_runs(before)
        elapsed = time.time() - started
        self.assertLess(elapsed, 15.0,
                        f"运行示例耗时 {elapsed:.1f}s，超过 15 秒要求")

        # 页面上两个任务卡片均可见且可切换
        for r in new_runs:
            self.page.wait_for_selector(
                f"li[data-id='{r['run_id']}']", timeout=5000)

    def test_02_edge_run_shows_conflicts_errors_and_evidence(self):
        self._goto()
        before = self._run_ids_before()
        self.page.click("#run-demo")
        new_runs = self._wait_two_new_runs(before)
        edge = next(r for r in new_runs if r["scenario"] == "demo-edge")
        self._click_run_by_id(edge["run_id"])

        # 冲突卡片：RSS guid 碰撞、Atom id 碰撞、标识重叠、同名警告
        self.page.wait_for_selector(".conflict-card", timeout=10000)
        self.assertGreaterEqual(
            self.page.locator(".conflict-card").count(), 4)
        # 冲突不静默：双方 XML 证据
        self.assertGreaterEqual(
            self.page.locator(".conflict-fragment").count(), 2)

        # 具体错误（404/410/503）
        self.page.wait_for_selector(".error-card", timeout=10000)
        error_text = self.page.locator("#catalog-pane").inner_text()
        for code in ("404", "410", "503"):
            self.assertIn(code, error_text)

        # 中栏条目、左栏 XML、右栏链接用途
        self.assertGreaterEqual(
            self.page.locator(".entry-card").count(), 10)
        # XML 证据可能位于折叠的 details 中，验证其已挂载且含真实标签
        self.page.wait_for_selector(".xml-doc", state="attached", timeout=5000)
        xml_text = self.page.locator(".xml-doc").first.text_content() or ""
        self.assertIn("<", xml_text)
        self.assertIn("enclosure",
                      self.page.locator("#links-pane").inner_text())

    def test_03_switching_runs_keeps_both_results(self):
        self._goto()
        before = self._run_ids_before()
        self.page.click("#run-demo")
        new_runs = self._wait_two_new_runs(before)
        normal = next(r for r in new_runs if r["scenario"] == "demo-normal")
        edge = next(r for r in new_runs if r["scenario"] == "demo-edge")

        self._click_run_by_id(normal["run_id"])
        self.page.wait_for_selector("#catalog-pane .entry-card", timeout=5000)
        self.assertIn("量子纠错入门",
                      self.page.locator("#catalog-pane").inner_text())

        self._click_run_by_id(edge["run_id"])
        self.page.wait_for_selector(".conflict-card", timeout=5000)
        self.assertIn("重复 guid",
                      self.page.locator("#catalog-pane").inner_text())

        # 切回正常场景，结果仍保留
        self._click_run_by_id(normal["run_id"])
        self.page.wait_for_selector("#catalog-pane .entry-card", timeout=5000)
        self.assertIn("量子纠错入门",
                      self.page.locator("#catalog-pane").inner_text())

    def test_04_full_acceptance_entry_point(self):
        self._goto()
        self.assertTrue(self.page.is_visible("#run-full"))
        before = self._run_ids_before()
        self.page.click("#run-full")
        run_id = self._wait_new_run_id(before)
        run = next(r for r in _get_json(f"{self.base}/api/runs")["runs"]
                   if r["run_id"] == run_id)
        self.assertIn(run["status"], ("completed_with_errors", "failed"))

        self._click_run_by_id(run_id)
        self.page.wait_for_selector(".entry-card", timeout=8000)
        text = self.page.locator("#catalog-pane").inner_text()
        self.assertIn("媒体更换", text)       # 媒体换址证据
        self.assertIn("订阅源自身标识冲突", text)  # 跨 feed 身份冲突


if __name__ == "__main__":
    unittest.main()
