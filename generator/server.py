"""虚构仪器本地源站（标准库 HTTP 服务，无外部业务依赖）。

公开接口（采集器只允许经这些接口取得响应）：
  GET  /healthz              健康检查
  GET  /scenarios            场景目录（虚构数据说明）
  POST /soap?scenario=<name> 返回对应场景的完整 multipart/related 响应；
                              Fault 场景按协议使用 HTTP 500 + SOAP Fault
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .mime_builder import build_mime_entity
from .scenarios import DATASETS, SCENARIO_ORDER, SCENARIO_TITLES_CN


class _InstrumentHandler(BaseHTTPRequestHandler):
    server_version = "FictionalLabInstrument/1.0"

    def log_message(self, fmt: str, *args) -> None:  # 安静日志，避免刷屏
        return

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("MIME-Version", "1.0")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._send(200, "application/json; charset=utf-8", b'{"status":"ok"}')
            return
        if parsed.path == "/scenarios":
            catalog = [
                {"scenario": s, "title": SCENARIO_TITLES_CN[s],
                 "kind": "fault" if "fault" in DATASETS[s] else "measurement"}
                for s in SCENARIO_ORDER
            ]
            self._send(200, "application/json; charset=utf-8",
                       json.dumps({"scenarios": catalog}, ensure_ascii=False).encode("utf-8"))
            return
        self._send(404, "application/json; charset=utf-8", b'{"error":"not found"}')

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/soap":
            self._send(404, "application/json; charset=utf-8", b'{"error":"not found"}')
            return
        # 读取并丢弃请求体（协议要求 SOAP 请求存在，内容不影响虚构响应）
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length:
            self.rfile.read(length)
        qs = parse_qs(parsed.query)
        scenario = (qs.get("scenario") or ["complete"])[0]
        if scenario not in DATASETS:
            self._send(400, "application/json; charset=utf-8",
                       json.dumps({"error": f"未知场景 {scenario}"}, ensure_ascii=False).encode("utf-8"))
            return
        body = build_mime_entity(scenario, DATASETS[scenario])
        # body 是完整 MIME 消息（外层 Content-Type 头 + 空行 + multipart 实体）；
        # 直接作为 HTTP 实体写出，采集器剥掉 HTTP 帧后仍保留 MIME 外层头与 boundary
        head_sep = body.find(b"\r\n\r\n")
        header_block = body[:head_sep].decode("ascii")
        content_type = "multipart/related"
        for line in header_block.split("\r\n"):
            if line.lower().startswith("content-type:"):
                content_type = line.split(":", 1)[1].strip()
        http_status = 500 if "fault" in DATASETS[scenario] else 200
        self._send(http_status, content_type, body)


def build_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _InstrumentHandler)


def serve_in_background(host: str, port: int) -> tuple[ThreadingHTTPServer, threading.Thread]:
    httpd = build_server(host, port)
    thread = threading.Thread(target=httpd.serve_forever, name="fictional-instrument", daemon=True)
    thread.start()
    return httpd, thread


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="虚构实验仪器源站（独立运行模式）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    args = parser.parse_args()
    httpd = build_server(args.host, args.port)
    print(f"虚构仪器源站监听 http://{args.host}:{args.port}（场景：{', '.join(SCENARIO_ORDER)}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
