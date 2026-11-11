# -*- coding: utf-8 -*-
"""本地虚构源站：模拟“清河环境观测站（虚构）”公开数据页面与接口。

仅提供虚构数据，不连接任何真实业务服务。
路由挂在 /source 前缀下，与操作台（/）分离。
"""
from __future__ import annotations

import csv
import io
import json
import os

from flask import Blueprint, Response, abort, current_app

source_bp = Blueprint("source", __name__, url_prefix="/source")


def _dataset() -> dict:
    path = os.path.join(current_app.config["SAMPLES_DIR"], "source", "measurements_en.json")
    with io.open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@source_bp.get("/measurements.html")
def measurements_html():
    data = _dataset()
    rows_html = []
    for row in data["rows"]:
        # data-value 与可见文本一致；空白字符在 HTML 中真实保留
        rows_html.append(
            "<tr data-id='{rid}'><td>{rid}</td><td>{analyte}</td>"
            "<td>{code}</td><td class='value' data-value='{val}'>{val}</td></tr>".format(
                rid=row["id"], analyte=row["analyte"],
                code=row["sample_code"], val=row["value"],
            )
        )
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="measurement-locale" content="en_US">
<title>清河环境观测站（虚构）- 公开测量数据</title>
<style>
 body {{ font-family: sans-serif; margin: 24px; }}
 table {{ border-collapse: collapse; }}
 td, th {{ border: 1px solid #888; padding: 6px 10px; }}
 .note {{ color: #666; font-size: 12px; }}
</style>
</head>
<body>
<h1>清河环境观测站 · 水质测量公开页（虚构）</h1>
<p class="note">数据集 {data['dataset']} ｜ 页面显式声明数字地区规则：
<b>en_US</b>（meta name=measurement-locale）｜ 全部数据为虚构。</p>
<table id="measurements">
<thead><tr><th>编号</th><th>分析项</th><th>样品号</th><th>测量原文</th></tr></thead>
<tbody>
{''.join(rows_html)}
</tbody>
</table>
</body>
</html>"""
    return Response(html, mimetype="text/html; charset=utf-8")


@source_bp.get("/measurements.json")
def measurements_json():
    return Response(json.dumps(_dataset(), ensure_ascii=False),
                    mimetype="application/json; charset=utf-8")


@source_bp.get("/measurements.csv")
def measurements_csv():
    data = _dataset()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "analyte", "sample_code", "value", "locale"])
    for row in data["rows"]:
        w.writerow([row["id"], row["analyte"], row["sample_code"],
                    row["value"], data["locale"]])
    return Response(buf.getvalue(), mimetype="text/csv; charset=utf-8")


@source_bp.get("/de/measurements.json")
def measurements_de_json():
    path = os.path.join(current_app.config["SAMPLES_DIR"],
                        "source", "measurements_de.json")
    if not os.path.isfile(path):
        abort(404)
    with io.open(path, "r", encoding="utf-8") as f:
        return Response(f.read(), mimetype="application/json; charset=utf-8")
