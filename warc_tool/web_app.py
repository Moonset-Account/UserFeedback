"""WARC 研究资料字段提取 · Web 应用（中文界面）。

只提供：上传样例归档 → 运行提取 → 查看字段卡片/来源证据/错误 → 跳转原始响应。
不提供归档文件管理（无删除、列表、重命名、下载接口）。
"""
from __future__ import annotations

import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from .runner import MAX_UPLOAD_BYTES, JobManager
from .samplegen import build_acceptance, build_demo_boundary, build_demo_normal

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UPLOAD_DIR = PROJECT_ROOT / "uploads"
SAMPLE_DIR = PROJECT_ROOT / "sample_outputs"
ALLOWED_SUFFIXES = (".warc", ".warc.gz", ".gz")


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(PROJECT_ROOT / "web" / "templates"),
        static_folder=str(PROJECT_ROOT / "web" / "static"),
    )
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    UPLOAD_DIR.mkdir(exist_ok=True)
    SAMPLE_DIR.mkdir(exist_ok=True)
    jobs = JobManager()
    sample_cache: dict[str, str] = {}

    def _ensure_samples() -> dict[str, str]:
        if not sample_cache:
            normal = build_demo_normal(str(SAMPLE_DIR))
            boundary = build_demo_boundary(str(SAMPLE_DIR))
            sample_cache["normal"] = normal.path
            sample_cache["boundary"] = boundary.path
        return sample_cache

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.post("/api/upload")
    def upload():
        file = request.files.get("warc")
        if file is None or not file.filename:
            return jsonify({"error": "未选择文件"}), 400
        filename = file.filename.lower()
        if not filename.endswith(ALLOWED_SUFFIXES):
            return jsonify({"error": "仅支持 .warc / .warc.gz 文件"}), 400
        safe_name = f"{uuid.uuid4().hex}_{Path(file.filename).name}"
        target = UPLOAD_DIR / safe_name
        file.save(target)
        if target.stat().st_size > MAX_UPLOAD_BYTES:
            target.unlink(missing_ok=True)
            return jsonify({"error": "文件超过 50MB 限制"}), 413
        job = jobs.start(str(target), label=file.filename)
        return jsonify({"job_id": job.job_id, "status": job.status})

    @app.post("/api/demo")
    def run_demo():
        """运行示例：依次跑正常场景与边界场景，两个结果都保留。"""
        samples = _ensure_samples()
        job_normal = jobs.start(samples["normal"], label="示例 · 正常场景（demo-normal.warc.gz）")
        job_boundary = jobs.start(samples["boundary"], label="示例 · 边界场景（demo-boundary.warc.gz）")
        return jsonify({
            "jobs": [
                {"job_id": job_normal.job_id, "label": job_normal.label},
                {"job_id": job_boundary.job_id, "label": job_boundary.label},
            ]
        })

    @app.post("/api/acceptance")
    def run_acceptance():
        sample = build_acceptance(str(SAMPLE_DIR))
        job = jobs.start(sample.path, label="完整验收场景（acceptance.warc.gz）")
        return jsonify({"job_id": job.job_id, "label": job.label})

    @app.get("/api/jobs")
    def list_jobs():
        return jsonify({"jobs": jobs.list_jobs()})

    @app.get("/api/jobs/<job_id>")
    def job_detail(job_id: str):
        job = jobs.get(job_id)
        if job is None:
            return jsonify({"error": "任务不存在"}), 404
        return jsonify({
            "job_id": job.job_id,
            "label": job.label,
            "status": job.status,
            "duration_ms": job.duration_ms,
            "error": job.error,
            "result": job.result,
        })

    @app.get("/api/jobs/<job_id>/records/<int:index>")
    def raw_record(job_id: str, index: int):
        try:
            payload = jobs.raw_record(job_id, index)
        except KeyError:
            return jsonify({"error": "任务不存在或尚未完成"}), 404
        except IndexError:
            return jsonify({"error": "记录序号超出范围"}), 404
        return jsonify(payload)

    @app.errorhandler(413)
    def too_large(_err):
        return jsonify({"error": "上传文件超过 50MB 限制"}), 413

    return app
