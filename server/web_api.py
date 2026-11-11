"""Web 内部 API：页面操作入口背后的任务接口（不是采集目标接口）。"""

from __future__ import annotations

import json
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from collector import CollectorClient, UnsupportedCombinationError, ValidationError
from collector.builder import RequestBuilder

web_bp = Blueprint("web_api", __name__, url_prefix="/api")


def _scenarios() -> dict:
    path = Path(current_app.config["SCENARIOS_PATH"])
    return json.loads(path.read_text(encoding="utf-8"))


@web_bp.get("/spec")
def get_spec():
    spec = current_app.config["SPEC"]
    return jsonify({
        "title": spec.title,
        "version": spec.version,
        "server_path": spec.server_path,
        "operations": [
            {
                "operationId": op.operation_id,
                "method": op.method,
                "path_template": op.path_template,
                "parameters": [
                    {
                        "name": p.name, "in": p.location, "required": p.required,
                        "style": p.style, "explode": p.explode,
                        "allowReserved": p.allow_reserved,
                        "type": p.schema.get("type"),
                        "properties": (p.schema.get("properties") or {}),
                        "items": p.schema.get("items"),
                    }
                    for p in op.parameters
                ],
            }
            for op in spec.operations.values()
        ],
    })


@web_bp.get("/scenarios")
def get_scenarios():
    return jsonify(_scenarios())


def _build_response(operation_id: str, params: dict) -> tuple[dict, int]:
    spec = current_app.config["SPEC"]
    builder = RequestBuilder(spec)
    try:
        built = builder.build(operation_id, params)
    except (UnsupportedCombinationError, ValidationError) as exc:
        # 关键：构造阶段拒绝，尚未发出任何 HTTP 请求
        return {
            "ok": False,
            "rejected_before_request": True,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }, 422
    return {
        "ok": True,
        "rejected_before_request": False,
        "request": {
            "method": built.method,
            "path": built.path,
            "query_string": built.query_string,
            "url": built.url,
            "request_line": built.request_line,
            "omitted": built.omitted,
            "parameter_plan": built.parameter_plan,
        },
    }, 200


@web_bp.post("/preview")
def preview():
    payload = request.get_json(force=True)
    body, status = _build_response(
        payload.get("operationId", ""), payload.get("params") or {}
    )
    return jsonify(body), status


@web_bp.post("/run")
def run():
    payload = request.get_json(force=True)
    operation_id = payload.get("operationId", "")
    params = payload.get("params") or {}
    spec = current_app.config["SPEC"]

    # 先构造；构造失败直接拒绝，不产生网络流量
    try:
        builder = RequestBuilder(spec)
        built = builder.build(operation_id, params)
    except (UnsupportedCombinationError, ValidationError) as exc:
        return jsonify({
            "ok": False,
            "rejected_before_request": True,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }), 422

    client = CollectorClient(spec, current_app.config["BASE_URL"])
    result = client.run(operation_id, params)
    if result.error:
        return jsonify({
            "ok": False, "rejected_before_request": False,
            "error": result.error,
            "request": _request_view(built),
        }), 502
    return jsonify({
        "ok": True,
        "rejected_before_request": False,
        "request": _request_view(built),
        "response": {
            "status_code": result.status_code,
            "elapsed_ms": result.elapsed_ms,
            "final_url": result.final_url,
            "echo": result.echo,
            "data": result.data,
            "response_headers": result.response_headers,
        },
    }), 200


def _request_view(built) -> dict:
    return {
        "method": built.method,
        "path": built.path,
        "query_string": built.query_string,
        "url": built.url,
        "request_line": built.request_line,
        "omitted": built.omitted,
        "parameter_plan": built.parameter_plan,
    }
