"""虚构源站 Flask 服务。

启动时把独立真值写入 runtime/source_truth/ground_truth.json（测试专用）。
采集器只能通过 HTTP 访问 /scenarios/<id>，不读取真值文件。
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

from flask import Flask, Response, jsonify, request

from .scenarios import ALL_SCENARIOS, BY_ID, SCENARIOS, ground_truth_records
from .templates import render


def write_ground_truth(runtime_dir: str | Path) -> Path:
    runtime_dir = Path(runtime_dir)
    out_dir = runtime_dir / "source_truth"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "ground_truth.json"
    path.write_text(
        json.dumps(ground_truth_records(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def create_app(runtime_dir: str | Path | None = None) -> Flask:
    app = Flask(__name__)
    if runtime_dir is not None:
        write_ground_truth(runtime_dir)

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "site": "starport-archive-fictional"})

    @app.get("/")
    def index():
        rows = "\n".join(
            f"<li><a href=\"{s['path']}\">{s['id']}</a> — "
            f"{s['title']}（HTTP {s['status']}）</li>"
            for s in ALL_SCENARIOS
        )
        return (
            f"<html lang=\"zh\"><head><meta charset=\"utf-8\">"
            "<title>星港资料馆（虚构源站）</title></head><body>"
            "<h1>星港资料馆 · 虚构源站</h1>"
            "<p>本站点全部内容为虚构演示数据，仅用于响应语义判定器验收。</p>"
            f"<ul>{rows}</ul></body></html>"
        )

    @app.get("/scenarios/<page_id>")
    def scenario_page(page_id: str):
        spec = BY_ID.get(page_id)
        if spec is None or spec["status"] is None:
            return Response(f"未知场景：{page_id}", status=404)
        html = render(page_id)
        return Response(html, status=spec["status"], mimetype="text/html")

    @app.get("/__transport/reset")
    def transport_reset():
        """模拟传输层故障：声明的 Content-Length 大于实际响应体，
        客户端读取时连接提前结束，HTTP 库必然抛异常（采集失败场景）。"""
        resp = Response(b"connection truncated", status=200)
        resp.headers["Content-Length"] = "512"
        resp.direct_passthrough = True
        # 主动关闭底层 socket，进一步保证客户端收到不完整响应
        sock = request.environ.get("werkzeug.socket")
        if isinstance(sock, socket.socket):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        return resp

    @app.get("/__truth/scenario-ids")
    def truth_ids():
        # 注意：这是生成器提供的场景目录（公开接口），不含期望分类真值
        return jsonify({"scenario_ids": [s["id"] for s in SCENARIOS]})

    return app


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="启动虚构源站")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--runtime-dir", default="runtime")
    args = parser.parse_args()
    create_app(args.runtime_dir).run(
        host=args.host, port=args.port, debug=False, threaded=True
    )


if __name__ == "__main__":
    main()
