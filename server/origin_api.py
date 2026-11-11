"""本地源站 API：/v1 下的公开只读实验接口 + 原始请求回显。"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from .echo_parser import ParseError, parse_path, split_raw_query, parse_query

api_bp = Blueprint("origin_api", __name__, url_prefix="/v1")


def _raw_environ() -> tuple[str, str]:
    """取未经解码的原始 path 与 query string。"""
    environ = request.environ
    raw_path = environ.get("RAW_URI")
    if raw_path is None:
        # Werkzeug：SCRIPT_NAME + PATH_INFO 保留百分号编码
        raw_path = environ.get("SCRIPT_NAME", "") + environ.get("PATH_INFO", "")
    raw_query = environ.get("QUERY_STRING", "")
    # 若取到的 RAW_URI 带 query，剥离
    if "?" in raw_path:
        raw_path = raw_path.split("?", 1)[0]
    return raw_path, raw_query


def _echo(op, raw_path: str, raw_query: str, path_parsed: dict,
          query_parsed: dict) -> dict:
    return {
        "method": request.method,
        "raw_path": raw_path,
        "raw_query_string": raw_query,
        "path_parameters": path_parsed,
        "query_parameters": query_parsed,
        "headers": {
            "Accept": request.headers.get("Accept"),
            "User-Agent": request.headers.get("User-Agent"),
            "Host": request.headers.get("Host"),
        },
    }


def _error(code: str, message: str, status: int, echo: dict):
    return jsonify({"error": {"code": code, "message": message},
                    "echo": echo}), status


@api_bp.get("/experiments")
def list_experiments():
    spec = current_app.config["SPEC"]
    op = spec.operation("listExperiments")
    raw_path, raw_query = _raw_environ()
    try:
        parsed = parse_query(split_raw_query(raw_query), op.query_params)
    except ParseError as exc:
        echo = _echo(op, raw_path, raw_query, {}, {})
        return _error("PARSE_ERROR", str(exc), 400, echo)
    echo = _echo(op, raw_path, raw_query, {}, parsed)
    rows = current_app.config["ORIGIN_FILTER"](
        current_app.config["EXPERIMENTS"], parsed
    )
    return jsonify({"data": rows, "echo": echo})


@api_bp.get("/experiments/<path:exp_id_tail>")
def get_experiment(exp_id_tail: str):
    spec = current_app.config["SPEC"]
    op = spec.operation("getExperiment")
    raw_path, raw_query = _raw_environ()
    template_path = spec.server_path + op.path_template
    try:
        path_parsed = parse_path(raw_path, template_path, op.path_params)
        query_parsed = parse_query(split_raw_query(raw_query), op.query_params)
    except ParseError as exc:
        echo = _echo(op, raw_path, raw_query, {}, {})
        return _error("PARSE_ERROR", str(exc), 400, echo)
    echo = _echo(op, raw_path, raw_query, path_parsed, query_parsed)
    row = next(
        (r for r in current_app.config["EXPERIMENTS"]
         if r["id"] == path_parsed["exp_id"]),
        None,
    )
    if row is None:
        return _error("NOT_FOUND",
                      f"实验 {path_parsed['exp_id']!r} 不存在（虚构数据）",
                      404, echo)
    return jsonify({"data": row, "echo": echo})


@api_bp.get("/labs/<path:lab_tail>/experiments/<path:exp_tail>")
def get_lab_experiment(lab_tail: str, exp_tail: str):
    spec = current_app.config["SPEC"]
    op = spec.operation("getLabExperiment")
    raw_path, raw_query = _raw_environ()
    template_path = spec.server_path + op.path_template
    try:
        path_parsed = parse_path(raw_path, template_path, op.path_params)
        query_parsed = parse_query(split_raw_query(raw_query), op.query_params)
    except ParseError as exc:
        echo = _echo(op, raw_path, raw_query, {}, {})
        return _error("PARSE_ERROR", str(exc), 400, echo)
    echo = _echo(op, raw_path, raw_query, path_parsed, query_parsed)
    wanted_id = path_parsed["exp_id"]["id"]
    row = next(
        (r for r in current_app.config["EXPERIMENTS"] if r["id"] == wanted_id),
        None,
    )
    if row is None:
        return _error("NOT_FOUND", f"实验 {wanted_id!r} 不存在（虚构数据）",
                      404, echo)
    return jsonify({"data": row, "echo": echo})


@api_bp.errorhandler(405)
def method_not_allowed(_err):
    return jsonify({
        "error": {"code": "METHOD_NOT_ALLOWED",
                  "message": "源站仅暴露只读 GET 接口"}
    }), 405
