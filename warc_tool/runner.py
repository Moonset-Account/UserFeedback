"""Web 后端的任务执行与原始记录取证接口。

提取子进程使用隔离模式（``-I``）运行，上传的 WARC 视为不可信数据：
* 只传绝对路径，不切换工作目录；
* 项目根目录在子进程内显式加入 sys.path；
* 不提供任何归档文件管理（删除/列表/重命名）接口。
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .reader import read_warc_file

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAX_JOBS = 50
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


@dataclass
class Job:
    job_id: str
    label: str
    source_path: str
    status: str = "pending"  # pending / running / done / error
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result: dict | None = None
    error: str | None = None
    command: list[str] | None = None
    duration_ms: int | None = None


class JobManager:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def start(self, source_path: str, label: str) -> Job:
        job_id = uuid.uuid4().hex[:12]
        job = Job(job_id=job_id, label=label, source_path=str(Path(source_path).resolve()))
        with self._lock:
            self._jobs[job_id] = job
            if len(self._jobs) > MAX_JOBS:
                oldest = sorted(self._jobs.values(), key=lambda j: j.created_at)[0]
                self._jobs.pop(oldest.job_id, None)
        thread = threading.Thread(target=self._run, args=(job,), daemon=True)
        thread.start()
        return job

    def _run(self, job: Job) -> None:
        job.status = "running"
        runner = f"""
import json, sys
sys.path.insert(0, {str(PROJECT_ROOT)!r})
from warc_tool.pipeline import run_pipeline
try:
    result = run_pipeline({job.source_path!r})
except Exception as exc:
    print(json.dumps({{"fatal": type(exc).__name__ + ": " + str(exc)}}, ensure_ascii=False))
    raise SystemExit(1)
print("@@RESULT@@")
json.dump(result, sys.stdout, ensure_ascii=False)
"""
        cmd = [sys.executable, "-I", "-c", runner]
        job.command = [sys.executable, "-I", "-m", "warc_tool.cli_extract", job.source_path]
        started = time.time()
        try:
            proc = subprocess.run(
                cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=120
            )
        except subprocess.TimeoutExpired:
            job.status = "error"
            job.error = "提取超时（>120s），任务终止"
            job.finished_at = time.time()
            job.duration_ms = int((job.finished_at - started) * 1000)
            return
        job.duration_ms = int((time.time() - started) * 1000)
        if proc.returncode != 0:
            job.status = "error"
            job.error = (proc.stderr.strip() or proc.stdout.strip() or "子进程退出码非零")[:2000]
        else:
            stdout = proc.stdout
            marker = "@@RESULT@@"
            if marker not in stdout:
                job.status = "error"
                job.error = f"子进程输出缺少结果标记：{stdout[:500]}"
            else:
                payload = stdout.split(marker, 1)[1].strip()
                try:
                    job.result = json.loads(payload)
                    job.status = "done"
                except json.JSONDecodeError as exc:
                    job.status = "error"
                    job.error = f"结果 JSON 解析失败：{exc}"
        job.finished_at = time.time()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return [self._summary(j) for j in jobs]

    @staticmethod
    def _summary(job: Job) -> dict:
        return {
            "job_id": job.job_id,
            "label": job.label,
            "status": job.status,
            "created_at": job.created_at,
            "duration_ms": job.duration_ms,
            "error": job.error,
            "record_count": job.result["total_records"] if job.result else None,
        }

    def raw_record(self, job_id: str, index: int) -> dict:
        """返回选中记录的原始响应视图（取证/跳转用）。"""
        job = self.get(job_id)
        if job is None or job.status != "done":
            raise KeyError("任务不存在或尚未完成")
        records, member_errors = read_warc_file(job.source_path)
        if index < 0 or index >= len(records):
            raise IndexError("记录序号超出范围")
        raw = records[index]
        return {
            "summary": raw.to_summary(),
            "warc_headers": raw.warc_headers,
            "http_statusline": raw.http_statusline,
            "http_headers": raw.http_headers,
            "payload_hex_prefix": raw.payload[:64].hex(),
            "payload_text": _safe_text(raw.payload),
            "payload_length": len(raw.payload),
            "block_length": len(raw.block),
            "errors": raw.errors,
            "member_errors": [vars(e) for e in member_errors],
        }


def _safe_text(data: bytes, limit: int = 200_000) -> str:
    text = data[:limit].decode("utf-8", errors="replace")
    if len(data) > limit:
        text += f"\n\n[……已截断，共 {len(data)} 字节，仅展示前 {limit} 字节……]"
    return text
