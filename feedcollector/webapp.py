"""Web 应用：中文操作入口 + JSON API + 内置本地源站托管。

路由概览：
  GET  /                      静态前端页面（static/index.html）
  GET  /api/scenarios         样例场景清单（已剥离测试真值，仅含来源与标签）
  POST /api/generate-samples  （重新）生成虚构夹具并启动本地源站
  POST /api/runs              提交采集任务（后台线程执行）
  GET  /api/runs              历史任务列表
  GET  /api/runs/<id>         任务完整结果（含证据与具体错误）
  /source/...                 源站公开资源（与 API 同源，采集器经 HTTP 访问）
"""
from __future__ import annotations

import json
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .collector import run_collection
from .storage import RunStore

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"
DATA_DIR = PROJECT_ROOT / "data"
FIXTURE_DIR = DATA_DIR / "fixtures"

# 样例生成器是可选依赖式的隔离模块：仅在“生成样例/源站”路由中导入，
# 采集核心路径（collector/parser/...）从不导入它。
_source_server = None
_source_lock = threading.Lock()
_manifest: dict | None = None
_store = RunStore(DATA_DIR)


def _ensure_samples() -> dict:
    """惰性生成夹具并启动本地源站（线程安全，幂等）。"""
    global _source_server, _manifest
    with _source_lock:
        if _source_server is not None and _manifest is not None:
            return _manifest
        from samplegen.scenarios import generate_fixtures
        from samplegen.source_server import build_server

        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        server = build_server(FIXTURE_DIR)
        server._thread.start()
        _source_server = server
        _manifest = generate_fixtures(FIXTURE_DIR, server.base_url)
        (DATA_DIR / "sample-manifest.json").write_text(
            json.dumps(_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return _manifest


def _public_manifest(manifest: dict) -> dict:
    """剥离仅供测试的真值字段：前端/采集器只能看到来源地址与标签。"""
    scenarios = {}
    for key, sc in manifest["scenarios"].items():
        scenarios[key] = {
            "label": sc["label"],
            "sources": sc["sources"],
            "timeout": sc.get("timeout", 5.0),
        }
    return {"base_url": manifest["base_url"], "scenarios": scenarios}


class _ApiHandler(BaseHTTPRequestHandler):
    server_version = "FeedIdentityCollector/1.0"

    def log_message(self, fmt: str, *args) -> None:
        return

    def handle_one_request(self) -> None:
        # 探测端可能在收到响应前断开（HEAD/Range 探测常见），忽略管道破裂噪音
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # ---- 基础工具 ----
    def _json(self, payload: dict | list, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, content_type: str, status: int = 200) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            return self._json({"error": f"文件不存在：{path.name}"}, 404)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"请求体不是合法 JSON：{exc}") from exc

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/":
                return self._file(STATIC_DIR / "index.html",
                                  "text/html; charset=utf-8")
            if path == "/app.js":
                return self._file(STATIC_DIR / "app.js",
                                  "application/javascript; charset=utf-8")
            if path == "/styles.css":
                return self._file(STATIC_DIR / "styles.css", "text/css; charset=utf-8")
            if path == "/api/scenarios":
                manifest = _ensure_samples()
                return self._json(_public_manifest(manifest))
            if path == "/api/runs":
                return self._json({"runs": _store.list_runs()})
            if path.startswith("/api/runs/"):
                run_id = urllib.parse.unquote(path[len("/api/runs/"):])
                report = _store.load_run(run_id)
                if report is None:
                    return self._json({"error": "任务不存在或已被删除"}, 404)
                return self._json(report)
            if path.startswith("/source/"):
                return self._serve_source(path[len("/source/"):])
            self._json({"error": f"未知路径：{path}"}, 404)
        except Exception as exc:  # 页面需要看到具体错误，不吞异常
            traceback.print_exc()
            self._json({"error": f"服务器内部错误：{exc}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/api/generate-samples":
                manifest = _ensure_samples()
                return self._json(_public_manifest(manifest))
            if parsed.path == "/api/runs":
                return self._create_run()
            self._json({"error": f"未知路径：{parsed.path}"}, 404)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:
            traceback.print_exc()
            self._json({"error": f"服务器内部错误：{exc}"}, 500)

    # ---- 源站资源（经同源 HTTP 公开，采集器的唯一样例访问方式） ----
    def _serve_source(self, relative: str) -> None:
        manifest = _ensure_samples()
        root = Path(manifest["source_root"]).resolve()
        target = (root / relative).resolve()
        if root not in target.parents and target != root:
            return self._json({"error": "禁止访问源站目录之外"}, 403)
        if not target.is_file():
            return self._json({"error": f"源站资源不存在：/{relative}"}, 404)
        ctype = {
            ".xml": "application/xml; charset=utf-8",
            ".mp3": "audio/mpeg", ".wav": "audio/wav",
            ".png": "image/png", ".html": "text/html; charset=utf-8",
        }.get(target.suffix, "application/octet-stream")
        self._file(target, ctype)

    # ---- 任务执行 ----
    def _create_run(self) -> None:
        payload = self._read_json()
        sources = payload.get("sources")
        scenario = str(payload.get("scenario", "custom"))[:80] or "custom"
        timeout = float(payload.get("timeout", 5.0))
        probe_media = bool(payload.get("probe_media", True))
        if not isinstance(sources, list) or not sources:
            raise ValueError("sources 必须是非空数组，每项含 url 与可选 version_label")
        clean_sources = []
        for item in sources:
            if not isinstance(item, dict) or not str(item.get("url", "")).strip():
                raise ValueError("每个来源必须包含非空 url")
            clean_sources.append({
                "url": str(item["url"]).strip()[:2000],
                "version_label": str(item.get("version_label", ""))[:80],
            })
        timeout = min(max(timeout, 0.5), 30.0)

        def worker() -> None:
            try:
                report = run_collection(
                    clean_sources, scenario=scenario,
                    timeout=timeout, probe_media=probe_media)
            except Exception as exc:  # 兜底：任何意外都落库为失败任务
                from datetime import datetime, timezone
                report = {
                    "run_id": "fatal-" + datetime.now(timezone.utc).strftime(
                        "%Y%m%dT%H%M%S%f"),
                    "scenario": scenario, "status": "failed",
                    "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "elapsed_ms": 0, "feeds": [],
                    "catalog": {"feed_groups": {}, "entries": [],
                                "conflicts": [], "snapshots": []},
                    "errors": [{"stage": "orchestrator",
                                "error_type": type(exc).__name__,
                                "message": str(exc),
                                "traceback": traceback.format_exc()}],
                }
            _store.save_run(report["run_id"], report)

        threading.Thread(target=worker, name="collection-run", daemon=True).start()
        self._json({"accepted": True, "message": "任务已提交，可在任务列表轮询结果"})


def serve(host: str = "127.0.0.1", port: int = 8765) -> int:
    # 启动即准备样例（首次生成极小文件，耗时可忽略）
    _ensure_samples()
    httpd = ThreadingHTTPServer((host, port), _ApiHandler)
    actual_host, actual_port = httpd.server_address[:2]
    print("=" * 64)
    print("研究讲座音频目录采集器已启动（仅采集元数据与 enclosure 可用性，无播放器）")
    print(f"操作入口：http://{actual_host}:{actual_port}/")
    print(f"本地源站：{_manifest['base_url']}/ （虚构数据）")
    print("按 Ctrl+C 停止")
    print("=" * 64)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止...")
    finally:
        httpd.server_close()
    return 0
