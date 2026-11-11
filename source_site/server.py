"""虚构标本目录源站（Flask）。

公开接口（即采集器支持的协议子集对应的服务端）：
* GET /api/catalog            JSON 目录第 1 页
* GET /api/catalog?cursor=..  JSON 后续页（游标不透明，无 total 字段）
* GET /catalog                HTML 目录第 1 页（data-role="catalog" 结构）
* GET /catalog?cursor=..      HTML 后续页（data-role="next-page" 翻页）
* GET /specimen/<id>          标本详情页（来源证据落地页）
* GET /healthz                健康检查

故障注入（仅本地演示/测试，环境变量 FAIL_MODE 控制）：
* none（默认）、http500:<页码>（该页首次访问返回 500）、intermittent（每 4 页一次 503）
"""
from __future__ import annotations

import os
from typing import Any

from flask import Flask, jsonify, request, abort, url_for

from .generator import Dataset, generate_dataset

# 进程内缓存两个内置虚构数据集，支持 ?dataset=demo|large 即时切换
_DATASETS: dict[str, Dataset] = {}


def _get_dataset(name: str) -> Dataset:
    if name not in ("demo", "large"):
        abort(400, description="dataset 仅支持 demo / large")
    if name not in _DATASETS:
        _DATASETS[name] = generate_dataset(name)
    return _DATASETS[name]


def _fail_if_configured(page_index: int) -> None:
    mode = os.environ.get("FAIL_MODE", "none")
    if mode.startswith("http500:"):
        try:
            target = int(mode.split(":", 1)[1])
        except ValueError:
            return
        if page_index == target:
            # 仅该页首次访问返回 500，用于验证客户端重试与预算记账；之后自动恢复
            flag = f"_failed_500_{target}"
            if not os.environ.get(flag):
                os.environ[flag] = "1"
                abort(500, description=f"注入故障：第 {target} 页首次访问返回 500（重试可恢复）")
    elif mode == "intermittent" and page_index >= 2 and page_index % 4 == 0:
        abort(503, description="注入故障：间歇性 503")


def create_app(dataset: Dataset | None = None) -> Flask:
    if dataset is not None:
        _DATASETS[dataset.name] = dataset
    # 预热两个内置数据集，使详情页不依赖目录页的访问顺序
    for _name in ("demo", "large"):
        _get_dataset(_name)
    app = Flask(__name__)

    def _ds() -> Dataset:
        # 查询参数 dataset 优先；默认 demo（与环境变量 DATASET 保持兼容）
        name = request.args.get("dataset") or os.environ.get("DATASET", "demo")
        return _get_dataset(name)

    def _cursor_for(page_index: int) -> str:
        # 页码从 1 开始；cursor 为不透明字符串，客户端不得解析其结构
        ds = _ds()
        return f"cursor={page_index + 1}" if page_index < ds.page_count else ""

    def _page_from_query() -> int:
        ds = _ds()
        page = request.args.get("cursor", type=int)
        if page is None:
            return 1
        if not 1 <= page <= ds.page_count:
            abort(400, description="游标越界")
        return page

    @app.get("/healthz")
    def healthz() -> Any:
        return jsonify(status="ok", dataset=_ds().name)

    @app.get("/api/catalog")
    def api_catalog() -> Any:
        ds = _ds()
        page_index = _page_from_query()
        _fail_if_configured(page_index)
        entries = ds.pages[page_index - 1]
        next_cursor = _cursor_for(page_index)
        # 切换数据集时把 dataset 参数透传进游标链接
        dataset_name = request.args.get("dataset")
        if next_cursor and dataset_name:
            next_cursor = f"{next_cursor}&dataset={dataset_name}"
        return jsonify(
            {
                "items": [s.to_public_dict() for s in entries],
                "next_cursor": next_cursor,
                # 刻意不提供 total：目录规模对客户端预先未知
            }
        )

    @app.get("/catalog")
    def html_catalog() -> str:
        ds = _ds()
        page_index = _page_from_query()
        _fail_if_configured(page_index)
        entries = ds.pages[page_index - 1]
        next_cursor = _cursor_for(page_index)
        dataset_name = request.args.get("dataset")
        if next_cursor and dataset_name:
            next_cursor = f"{next_cursor}&dataset={dataset_name}"
        cards = "\n".join(
            f'''      <article class="specimen" data-id="{s.id}">
        <h3>{s.title}</h3>
        <p>虚构产地：{s.region} ｜ 采集年份：{s.collected_year}</p>
        <a class="detail-link" href="{url_for('specimen_detail', specimen_id=s.id)}">查看 {s.id} 详情</a>
      </article>'''
            for s in entries
        )
        next_link = (
            f'\n  <nav class="pager"><a data-role="next-page" href="{url_for("html_catalog")}?{next_cursor}">下一页</a></nav>'
            if next_cursor
            else '\n  <nav class="pager"><span data-role="last-page">已到末页</span></nav>'
        )
        return f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>虚构标本目录（HTML）第 {page_index} 页</title></head>
<body>
  <h1>公开虚构标本目录 · HTML 协议子集 · 第 {page_index}/{ds.page_count} 页</h1>
  <div data-role="catalog">
{cards}
  </div>{next_link}
</body></html>"""

    @app.get("/specimen/<specimen_id>")
    def specimen_detail(specimen_id: str) -> str:
        # 详情页可被任一数据集的来源链接引用，跨两个内置数据集查找
        record = None
        for dataset in _DATASETS.values():
            record = next((s for s in dataset.unique_records if s.id == specimen_id), None)
            if record is not None:
                break
        if record is None:
            abort(404, description="虚构数据中不存在该标本")
        return f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>{record.id} 详情（虚构）</title></head>
<body>
  <h1>{record.title}</h1>
  <dl>
    <dt>标本 ID</dt><dd>{record.id}</dd>
    <dt>虚构学名</dt><dd>{record.species_name}</dd>
    <dt>虚构产地</dt><dd>{record.region}</dd>
    <dt>采集年份</dt><dd>{record.collected_year}</dd>
    <dt>虚构采集人</dt><dd>{record.collector}</dd>
  </dl>
  <p><a href="{url_for('html_catalog')}">返回目录首页</a></p>
</body></html>"""

    @app.get("/")
    def index() -> str:
        return (
            "<h1>虚构标本目录源站</h1>"
            f"<ul><li><a href='{url_for('api_catalog')}'>JSON 接口</a></li>"
            f"<li><a href='{url_for('html_catalog')}'>HTML 目录</a></li></ul>"
        )

    return app


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="启动虚构标本目录源站")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--dataset", default="demo", choices=("demo", "large"))
    args = ap.parse_args()
    os.environ["DATASET"] = args.dataset
    app = create_app()
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
