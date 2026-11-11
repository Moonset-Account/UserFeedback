"""本地源站蓝图：只通过公开 HTTP 接口提供 RIP/1.0 NDJSON 流与场景清单。

源站不暴露任何“真值”接口；采集器与浏览器只能拿到协议消息本身。
"""
from __future__ import annotations

import time

from flask import Blueprint, Response, abort, jsonify

from sample_generator import SCENARIOS, raw_ndjson_lines

source_bp = Blueprint("source", __name__)


@source_bp.get("/source/scenarios")
def list_scenarios():
    return jsonify({
        "protocol": "RIP/1.0",
        "scenarios": [
            {
                "key": s.key,
                "title": s.title,
                "group": s.group,
                "description": s.description,
                "message_count": len(s.messages),
                "drop_connection": s.drop_connection,
            }
            for s in SCENARIOS.values()
        ],
    })


@source_bp.get("/source/stream/<scenario_key>")
def stream_scenario(scenario_key: str):
    scenario = SCENARIOS.get(scenario_key)
    if scenario is None:
        abort(404, description=f"未知场景: {scenario_key}")

    lines = raw_ndjson_lines(scenario)
    if scenario.drop_connection:
        # 边界场景：发送全部消息后直接关闭连接，不发 end（end 已被场景剔除），
        # 模拟源站故障导致的流中断。
        payload = lines
    else:
        payload = lines

    def generate():
        for line in payload:
            yield line + "\n"
            if scenario.delay_seconds:
                time.sleep(scenario.delay_seconds)
        # 正常场景最后一条就是 end；drop_connection 场景到此连接关闭，无 end。

    return Response(
        generate(),
        status=200,
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Protocol": "RIP/1.0"},
    )
