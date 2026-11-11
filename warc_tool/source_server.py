"""虚构源站 HTTP 服务（独立进程）。

启动：python3 app.py source --host 127.0.0.1 --port <端口>
仅监听本机，提供 source_content 中定义的公开页面与 /healthz。
"""
from __future__ import annotations

from flask import Flask, make_response, request

from .source_content import (
    ARTICLE_VERSIONS,
    BASE_PAGES,
    SIM_DATE_HEADER,
    render_article,
    render_static,
    select_version,
)


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "site": "xinglan-fictional"}

    @app.get("/")
    def index():
        html, headers = render_static("/")
        resp = make_response(html)
        for k, v in headers.items():
            resp.headers[k] = v
        return resp

    @app.get("/about")
    def about():
        html, headers = render_static("/about")
        resp = make_response(html)
        for k, v in headers.items():
            resp.headers[k] = v
        return resp

    def _article():
        # 公开版本接口：X-Sim-Date 请求头选择同 URL 的历史版本
        sim_date = request.headers.get(SIM_DATE_HEADER)
        version_key = select_version(request.path, sim_date)
        html, headers = render_article(request.path, version_key)
        resp = make_response(html)
        for k, v in headers.items():
            resp.headers[k] = v
        resp.headers["X-Sim-Date-Resolved"] = version_key
        return resp

    for article_path in ARTICLE_VERSIONS:
        app.add_url_rule(article_path, view_func=_article,
                         endpoint=f"article_{article_path.strip('/').replace('/', '_')}")

    @app.errorhandler(404)
    def not_found(_err):
        resp = make_response("<h1>404 虚构页面不存在</h1>", 404)
        resp.headers["Content-Type"] = "text/html; charset=utf-8"
        return resp

    return app


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="星澜虚构源站")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8123)
    args = parser.parse_args(argv)
    create_app().run(host=args.host, port=args.port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
