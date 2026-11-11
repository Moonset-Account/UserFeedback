"""本地公开实验源站（虚构）。

公开路由（采集器允许访问的全部交互面）：
  GET /                         实验索引页
  GET /experiment/<scenario_id> 实验详情页（服务端渲染 或 JS 渲染）
  GET /api/manifest/<scenario_id> 页面同源 JSON 清单（仅服务端渲染场景提供）
  GET /api/bootstrap/<scenario_id> JS 渲染页面的脚本引导数据
  GET /download/<token>         短时一次性导出链接

管理路由（测试/演示重置用，采集器禁止依赖）：
  POST /admin/reset
  GET  /admin/state

支持的协议子集（与采集器对应）：
  HTTP/1.1 上的 HTML4/5 锚点解析、同源 JSON、application/zip 二进制下载；
  浏览器自动化子集：等待 JS 注入 DOM 后读取锚点与版本号。
"""
from __future__ import annotations

from pathlib import Path

from flask import Blueprint, Flask, abort, jsonify, render_template, request, send_file

from generator.generate import experiment_id_for
from generator.scenarios import BASE_VERSION, SCENARIOS, SCENARIO_ORDER
from source_site.token_store import TokenStore

bp = Blueprint("source", __name__)

store: TokenStore | None = None  # create_app 时注入
DATA_DIR: Path | None = None


def create_app(data_dir: Path) -> Flask:
    global store, DATA_DIR
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    store = TokenStore(data_dir)
    DATA_DIR = data_dir
    app.register_blueprint(bp)
    return app


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------
@bp.get("/")
def index():
    items = [
        {
            "id": sid,
            "title": SCENARIOS[sid].title,
            "experiment_id": experiment_id_for(sid),
            "js_rendered": SCENARIOS[sid].js_rendered,
        }
        for sid in SCENARIO_ORDER
    ]
    return render_template("index.html", items=items)


@bp.get("/experiment/<scenario_id>")
def experiment_page(scenario_id: str):
    spec = SCENARIOS.get(scenario_id)
    if spec is None:
        abort(404)
    exp_id = experiment_id_for(scenario_id)
    if spec.js_rendered:
        # 初始 HTML 不含任何下载锚点与版本，完全由脚本注入
        return render_template("experiment_js.html", spec=spec, experiment_id=exp_id)

    token, version = store.issue(scenario_id)
    return render_template(
        "experiment.html",
        spec=spec,
        experiment_id=exp_id,
        version=version,
        token=token,
    )


# --------------------------------------------------------------------------
# 同源 JSON 清单 / JS 引导数据
# --------------------------------------------------------------------------
@bp.get("/api/manifest/<scenario_id>")
def api_manifest(scenario_id: str):
    spec = SCENARIOS.get(scenario_id)
    if spec is None:
        abort(404)
    if spec.js_rendered:
        # JS 渲染场景不提供静态 JSON 清单，链接只能从渲染后 DOM 取得
        return jsonify({"error": "manifest_unavailable",
                        "message": "该实验页面由 JavaScript 渲染，请使用浏览器自动化读取 DOM。"}), 404
    token, version = store.issue(scenario_id)
    return jsonify({
        "experiment_id": experiment_id_for(scenario_id),
        "version": version,
        "download_url": f"/download/{token.token}",
        "expires_in": token.ttl_seconds,
        "token_state": "active" if token.state == "active" else token.state,
    })


@bp.get("/api/bootstrap/<scenario_id>")
def api_bootstrap(scenario_id: str):
    spec = SCENARIOS.get(scenario_id)
    if spec is None:
        abort(404)
    token, version = store.issue(scenario_id)
    return jsonify({
        "experiment_id": experiment_id_for(scenario_id),
        "version": version,
        "download_url": f"/download/{token.token}",
        "expires_in": token.ttl_seconds,
    })


# --------------------------------------------------------------------------
# 短时一次性下载链接
# --------------------------------------------------------------------------
ERROR_PAGES = {
    "expired": ("链接已过期", "该导出链接的有效时间已结束，请回到实验页面重新获取地址。"),
    "used": ("链接已被使用", "该一次性导出链接已被消费，每个链接仅允许下载一次。"),
}


@bp.get("/download/<token>")
def download(token: str):
    tok = store.get(token)
    if tok is None:
        return render_template("error.html", code=404, title="链接不存在",
                               message="找不到对应的导出链接，它可能已被系统回收。"), 404

    if tok.state == "error_html":
        # 关键边界：HTTP 状态码 200，但内容是错误 HTML，必须靠内容类型识别
        return render_template("error.html", code=200, title="导出暂不可用",
                               message="源站临时故障，导出内容未能生成（HTTP 200 错误页）。",
                               kind="error_html")

    if tok.state in ("expired", "used") or tok.consumed:
        kind = "used" if (tok.state == "used" or tok.consumed) else "expired"
        title, message = ERROR_PAGES[kind]
        return render_template("error.html", code=410, title=title, message=message, kind=kind), 410

    # active：一次性消费
    if not store.consume_if_active(tok):
        title, message = ERROR_PAGES["used"]
        return render_template("error.html", code=410, title=title, message=message, kind="used"), 410

    payload = Path(tok.payload_path)
    if not payload.exists():
        abort(500)
    resp = send_file(
        payload,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{tok.experiment_id}-export.zip",
    )
    resp.headers["X-Experiment-Id"] = tok.experiment_id
    return resp


# --------------------------------------------------------------------------
# 管理/诊断（采集器不允许使用）
# --------------------------------------------------------------------------
@bp.post("/admin/reset")
def admin_reset():
    store.reset()
    return jsonify({"ok": True, "message": "源站链接状态已重置"})


@bp.get("/admin/state")
def admin_state():
    return jsonify(store.stats())
