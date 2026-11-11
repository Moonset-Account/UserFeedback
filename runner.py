"""任务运行与结果存储。

每次运行产生一个 Job：采集结果 + 判定结果 + 证据全部落盘到 runtime/jobs/。
复判（reclassify）不重新采集，只基于保存的 FetchInput 重跑判定，
并把每次判定追加到 history —— 旧判断与新依据同时保留。
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from classifier import RuleError  # noqa: F401  (RuleError 属本模块公开异常)
from classifier.custom_rules import load_rules
from classifier.engine import FetchInput, classify_response
from classifier.models import (
    CATEGORY_LABELS_ZH,
    FetchResult,
)
from collector import FetchError, fetch


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.jobs_dir = self.root / "jobs"
        self.rules_dir = self.root / "rules"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.rules_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ---------- 规则 ----------

    def save_rules(self, rules_json: str) -> Path:
        """先校验再落盘，坏规则不会被保存。"""
        path = self.rules_dir / "custom_rules.json"
        tmp = self.rules_dir / ".custom_rules.tmp.json"
        tmp.write_text(rules_json, encoding="utf-8")
        load_rules(tmp)  # 校验，失败抛 RuleError
        tmp.replace(path)
        return path

    def current_rules_path(self) -> Path | None:
        path = self.rules_dir / "custom_rules.json"
        return path if path.exists() else None

    def load_current_rules(self) -> list[dict]:
        path = self.current_rules_path()
        return load_rules(path) if path else []

    # ---------- 任务 ----------

    def _job_path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def list_jobs(self) -> list[dict]:
        jobs = []
        for p in sorted(self.jobs_dir.glob("*.json"), reverse=True):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            jobs.append(self._summary(data))
        return jobs

    def get_job(self, job_id: str) -> dict | None:
        path = self._job_path(job_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _summary(data: dict) -> dict:
        latest = data["history"][-1]
        return {
            "job_id": data["job_id"],
            "label": data.get("label", ""),
            "target": data["target"],
            "mode": data["mode"],
            "created_at": data["created_at"],
            "latest_category": latest["category"],
            "latest_category_label": CATEGORY_LABELS_ZH.get(
                latest["category"], latest["category"]
            ),
            "isolated": latest["isolated"],
            "confidence": latest["confidence"],
            "revision_count": len(data["history"]),
            "fetch_ok": data["fetch"]["ok"],
        }

    def _write(self, job: dict) -> None:
        with self._lock:
            self._job_path(job["job_id"]).write_text(
                json.dumps(job, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def run(
        self,
        target: str,
        *,
        mode: str = "http",
        referer: str = "",
        label: str = "",
        rules: list[dict] | None = None,
        timeout: float = 8.0,
    ) -> dict:
        job_id = uuid.uuid4().hex[:12]
        created = _now_iso()

        # 1) 采集（只经公开接口/文件/浏览器）
        try:
            fetch_result = fetch(target, mode=mode, referer=referer, timeout=timeout)
            fetch_payload = fetch_result.to_dict()
        except FetchError as exc:
            failed = FetchResult(ok=False, url=target, error=str(exc),
                                 fetched_at=created, fetch_mode=mode)
            fetch_payload = failed.to_dict()

        # 2) 判定（传输失败直接 FETCH_FAILED；否则走语义引擎）
        if fetch_payload["ok"]:
            engine_input = FetchInput(
                html=fetch_payload["html"],
                status_code=fetch_payload["status_code"] or 200,
                url=fetch_payload["url"],
                content_type=fetch_payload["content_type"],
                referer=fetch_payload.get("referer", ""),
                nav_source=fetch_payload.get("nav_source", ""),
            )
            result = classify_response(engine_input, rules=rules)
        else:
            result = classify_response(FetchResult(**{
                k: v for k, v in fetch_payload.items()
                if k in FetchResult.__dataclass_fields__
            }))

        job = {
            "job_id": job_id,
            "label": label,
            "target": target,
            "mode": mode,
            "referer": referer,
            "created_at": created,
            "fetch": _compact_fetch(fetch_payload),
            "history": [
                {
                    "revision": 1,
                    "at": created,
                    "basis": "initial",
                    "rule_codes": result.custom_rule_codes,
                    **result.to_dict(),
                }
            ],
        }
        self._write(job)
        return job

    def reclassify(
        self,
        job_id: str,
        *,
        basis: str = "user-rule-review",
        note: str = "",
    ) -> dict:
        """复判：复用已保存的采集快照，不重新发起采集；历史判断原样保留。"""
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(f"任务不存在：{job_id}")
        fetch_payload = job["fetch"]
        if not fetch_payload["ok"]:
            raise RuleError("采集失败任务没有可复判的页面快照")

        rules = self.load_current_rules()
        engine_input = FetchInput(
            html=fetch_payload["html_snapshot"],
            status_code=fetch_payload["status_code"] or 200,
            url=fetch_payload["url"],
            content_type=fetch_payload["content_type"],
            referer=job.get("referer", ""),
            nav_source=fetch_payload.get("nav_source", ""),
        )
        result = classify_response(engine_input, rules=rules)
        previous = job["history"][-1]
        record = {
            "revision": len(job["history"]) + 1,
            "at": _now_iso(),
            "basis": basis,
            "note": note,
            "previous_category": previous["category"],
            "rule_codes": result.custom_rule_codes,
            **result.to_dict(),
        }
        job["history"].append(record)
        self._write(job)
        return job


def _compact_fetch(fetch_payload: dict) -> dict:
    """保存采集快照：完整 HTML 留作复判依据，同时给出长度等元信息。"""
    payload = dict(fetch_payload)
    html = payload.get("html", "") or ""
    payload["html_snapshot"] = html
    payload["html_length"] = len(html)
    return payload
