"""任务执行器：把协议步骤编排为真实浏览器操作，并产出证据报告。

失败语义：任何步骤出现歧义、禁用、未找到、状态校验失败等，
立即停止后续步骤（stop-on-error），报告 status=failed 并保留全部已采集证据。
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .collector import LabSession, StepEvidence
from .errors import RpaError
from .protocol import Task, parse_task
from .resolver import DYNAMIC_PENDING


@dataclass
class RunReport:
    task_name: str
    status: str  # success | failed
    started_at: float
    finished_at: float | None = None
    steps: list[dict] = field(default_factory=list)
    error: dict | None = None
    screenshots: list[str] = field(default_factory=list)
    boundary_violations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def run_task_file(
    task_path: Path,
    base_url: str,
    run_dir: Path,
    *,
    headless: bool = True,
) -> RunReport:
    task = parse_task(task_path.read_text(encoding="utf-8"))
    return run_task(task, base_url, run_dir, headless=headless)


def run_task(
    task: Task,
    base_url: str,
    run_dir: Path,
    *,
    headless: bool = True,
) -> RunReport:
    run_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    report = RunReport(task_name=task.name, status="success", started_at=time.time())

    with LabSession(base_url, evidence_dir, headless=headless) as session:
        try:
            for index, step in enumerate(task.steps):
                # _execute_step 在 finally 中把证据（含失败步骤）发布到 report。
                _execute_step(session, index, step, evidence_dir, report)
        except RpaError as exc:
            report.status = "failed"
            report.error = exc.to_dict()
            try:
                shot = session.screenshot("error-stop")
                report.screenshots.append(shot)
                report.boundary_violations.extend(session.blocked_requests())
            except Exception:
                pass
        except Exception as exc:  # 浏览器层面的未预期异常也要显式上报
            report.status = "failed"
            report.error = {
                "code": "UNEXPECTED_ERROR",
                "message": f"{type(exc).__name__}: {exc}",
                "detail": {},
            }
        finally:
            report.finished_at = time.time()
            # 汇总越界请求（去重保序）。
            seen: set[str] = set()
            deduped = []
            for url in report.boundary_violations + session.blocked_requests():
                if url not in seen:
                    seen.add(url)
                    deduped.append(url)
            report.boundary_violations = deduped

    (run_dir / "report.json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def _execute_step(
    session: LabSession,
    index: int,
    step,
    evidence_dir: Path,
    report: RunReport,
) -> StepEvidence:
    action = step.action
    payload = step.payload
    ev = StepEvidence(index=index, action=action, summary="")
    try:
        if action == "switch_context":
            observed = session.switch_context(payload["context"])
            ev.summary = f"切换实验上下文为 {payload['context']}"
            ev.observed = observed
            ev.screenshot = session.screenshot("context")

        elif action == "snapshot_menu":
            snapshot = session.fetch_snapshot()
            commands = [
                {
                    "id": cmd.id,
                    "path": list(cmd.path),
                    "scope": cmd.scope,
                    "enabled": cmd.enabled,
                }
                for cmd in snapshot.commands()
            ]
            ev.summary = f"采集菜单快照：上下文 {snapshot.context}，共 {len(commands)} 条命令"
            ev.observed = {"context": snapshot.context, "commands": commands}
            ev.screenshot = session.screenshot("snapshot")

        elif action == "search_commands":
            limit = payload.get("limit", 8)
            observed = session.search(payload["query"], limit)
            ev.summary = (
                f"搜索 {payload['query']!r}：按页面实时排序返回 "
                f"{len(observed['ordered_results'])} 条（顺序来自 DOM，不做本地重排）"
            )
            ev.observed = observed
            ev.screenshot = observed.pop("screenshot")

        elif action == "open_path":
            # 关键：先采集真实快照再做路径/作用域/可用状态决策。
            snapshot, decision = session.resolve_live(
                payload["menu_path"], payload.get("scope")
            )
            # 动态子菜单：先在浏览器里展开父菜单、重新采集，再做一次决策。
            if decision.status == DYNAMIC_PENDING:
                snapshot, decision = session.expand_dynamic_then_resolve(
                    payload["menu_path"], payload.get("scope")
                )
                ev.note = "检测到动态子菜单，已先展开父菜单并重新采集快照后再决策"
            ev.decision = decision.to_dict()
            if decision.status == DYNAMIC_PENDING:
                ev.summary = "动态子菜单展开后仍不可用，停止"
                ev.blocked_requests = session.blocked_requests()
                raise _to_error(decision)
            if decision.status != "unique":
                ev.summary = f"决策状态 {decision.status}，按规则停止，不执行点击"
                ev.note = (ev.note + "；" if ev.note else "") + decision.reason
                ev.screenshot = session.screenshot("blocked")
                ev.blocked_requests = session.blocked_requests()
                raise _to_error(decision)
            click_result = session.open_path(decision)
            ev.summary = (
                f"按完整路径点击命令 {click_result['clicked_node_id']}："
                f"{' > '.join(click_result['clicked_path'])}"
            )
            ev.observed = click_result
            ev.screenshot = session.screenshot("open-path")

        elif action == "expect_state":
            observed = session.expect_state(payload["target"], payload["equals"])
            ev.summary = (
                f"可见状态校验通过：{payload['target']} == {payload['equals']!r}"
            )
            ev.observed = observed

        elif action == "wait_visible":
            timeout_ms = payload.get("timeout_ms", 5000)
            observed = session.wait_visible_text(payload["target"], timeout_ms)
            ev.summary = f"目标 {payload['target']} 已可见，文本={observed['text']!r}"
            ev.observed = observed

        ev.blocked_requests = session.blocked_requests()
        return ev
    finally:
        # 无论成功还是抛错，当前步骤证据都先发布到报告（失败步骤也保留决策理由）。
        if not ev.blocked_requests:
            ev.blocked_requests = session.blocked_requests()
        report.steps.append(ev.to_dict())
        if ev.screenshot:
            report.screenshots.append(ev.screenshot)


def _to_error(decision):
    from .errors import (
        AmbiguousCommandError,
        CommandNotFoundError,
        DisabledCommandError,
    )
    from .resolver import AMBIGUOUS, DISABLED

    if decision.status == AMBIGUOUS:
        return AmbiguousCommandError(decision.reason, detail=decision.to_dict())
    if decision.status == DISABLED:
        return DisabledCommandError(decision.reason, detail=decision.to_dict())
    return CommandNotFoundError(decision.reason, detail=decision.to_dict())
