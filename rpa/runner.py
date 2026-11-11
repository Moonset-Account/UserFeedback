"""任务编排：采集 -> 可满足性判断 -> 浏览器执行 -> 独立验证 -> 证据留存。

网页与 CLI 都通过本模块运行任务；执行结果来自真实 Playwright 运行，
任何任务都不预填成功状态。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from collector.collector import CollectionError, collect_from_file, collect_via_http
from core.models import Spec
from core.parser import ProtocolError
from core.planner import UnsatisfiableError, check_satisfiable, plan_moves
from core.verifier import verify_order
from generator.sample_generator import SAMPLES_DIR
from rpa.executor import ExecutionResult, execute_in_browser

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = PROJECT_ROOT / "runs"

# 内存任务表（线程安全），证据同时落盘到 runs/
_LOCK = threading.Lock()
_RUNS: dict[str, dict] = {}


def list_scenarios() -> list[dict]:
    """从公开样例目录读取可运行场景（不触碰 truth 目录）。"""
    out = []
    for p in sorted(SAMPLES_DIR.glob("*.json")):
        try:
            spec = collect_from_file(p)
        except CollectionError:
            continue
        out.append({
            "scenario_id": spec.scenario_id,
            "title": spec.title,
            "n_steps": len(spec.steps),
            "has_fixed": bool(spec.fixed),
            "has_interference": spec.interference is not None,
            "file": str(p.relative_to(PROJECT_ROOT)),
        })
    return out


def load_spec(scenario_id: str) -> Spec:
    return collect_from_file(SAMPLES_DIR / f"{scenario_id}.json")


def initial_order_for(spec: Spec) -> list[str]:
    """读取样例配套的初始打乱顺序（公开数据，位于普通样例 JSON 之外的
    独立 initial 文件；由生成器在运行时写入 samples/initial/）。"""
    p = SAMPLES_DIR / "initial" / f"{spec.scenario_id}.json"
    if p.is_file():
        import json
        return json.loads(p.read_text(encoding="utf-8"))
    return spec.step_ids  # 无打乱文件时使用规约原始顺序


def _new_run(scenario_id: str, mode: str) -> dict:
    run_id = uuid.uuid4().hex[:12]
    record = {
        "run_id": run_id,
        "scenario_id": scenario_id,
        "mode": mode,
        "status": "running",
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": None,
        "satisfiable": None,
        "conflict_chains": [],
        "plan": None,
        "execution": None,
        "dry_run_verification": None,
        "errors": [],
    }
    with _LOCK:
        _RUNS[run_id] = record
    return record


def get_run(run_id: str) -> Optional[dict]:
    with _LOCK:
        record = _RUNS.get(run_id)
        return dict(record) if record else None


def list_runs(limit: int = 30) -> list[dict]:
    with _LOCK:
        return [dict(r) for r in list(_RUNS.values())[-limit:][::-1]]


def _save_evidence(run_id: str, record: dict) -> None:
    import json
    RUNS_DIR.mkdir(exist_ok=True)
    (RUNS_DIR / f"{run_id}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def run_scenario(
    scenario_id: str,
    base_url: str,
    headless: bool = True,
    mode: str = "browser",
    on_event=None,
) -> dict:
    """运行单个场景。mode: browser（真实拖拽）| check（仅约束检查与规划）。"""
    record = _new_run(scenario_id, mode)
    run_id = record["run_id"]
    try:
        # 1) 采集（文件通道；truth 目录在采集器内被硬拒绝）
        spec = load_spec(scenario_id)

        # 2) 可满足性判断：先判断，再规划
        try:
            check_satisfiable(spec)
            record["satisfiable"] = True
        except UnsatisfiableError as exc:
            record["satisfiable"] = False
            record["conflict_chains"] = exc.chains
            record["status"] = "unsatisfiable"
            record["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _save_evidence(run_id, record)
            return dict(record)

        # 3) 先基于规约原始顺序产出一份确定性计划（供界面展示依赖图与计划）
        seed_plan = plan_moves(spec, initial_order_for(spec))
        record["plan"] = {
            "target": seed_plan.target,
            "lower_bound": seed_plan.lower_bound,
            "moves": [asdict(m) for m in seed_plan.moves],
            "diagnostics": seed_plan.diagnostics,
            "optimal": seed_plan.optimal,
        }

        if mode == "check":
            # 仅检查模式：对目标序做独立验证，不启动浏览器
            verification = verify_order(spec, seed_plan.target)
            record["dry_run_verification"] = verification.to_dict()
            record["status"] = "checked"
            record["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _save_evidence(run_id, record)
            return dict(record)

        # 4) 浏览器真实执行（源页由本地 Flask 源站提供）
        page_url = f"{base_url.rstrip('/')}/source/{scenario_id}"
        exec_result: ExecutionResult = execute_in_browser(
            spec, page_url=page_url, headless=headless, on_event=on_event
        )
        record["execution"] = {
            "ok": exec_result.ok,
            "target": exec_result.target,
            "final_order": exec_result.final_order,
            "moves_executed": exec_result.moves_executed,
            "replans": exec_result.replans,
            "manual_swaps": exec_result.manual_swaps,
            "history": [asdict(h) for h in exec_result.history],
            "verification": exec_result.verification,
            "evidence": exec_result.evidence,
            "errors": exec_result.errors,
        }
        record["errors"] = exec_result.errors
        record["status"] = "success" if exec_result.ok else "failed"
        record["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save_evidence(run_id, record)
        return dict(record)
    except (CollectionError, ProtocolError) as exc:
        record["errors"].append(f"输入/采集错误：{exc}")
        record["status"] = "failed"
        record["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save_evidence(run_id, record)
        return dict(record)
    except Exception as exc:  # noqa: BLE001
        record["errors"].append(f"未预期错误：{type(exc).__name__}: {exc}")
        record["status"] = "failed"
        record["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save_evidence(run_id, record)
        return dict(record)


def run_demo_pair(base_url: str, headless: bool = True) -> dict:
    """“运行示例”按钮：一个正常场景 + 一个边界（循环依赖）场景，小样本快速结束。"""
    t0 = time.time()
    normal = run_scenario("normal-titration", base_url, headless=headless)
    boundary = run_scenario("cyclic-deps", base_url, headless=headless, mode="check")
    return {
        "normal_run_id": normal["run_id"],
        "boundary_run_id": boundary["run_id"],
        "elapsed_seconds": round(time.time() - t0, 2),
        "normal_status": normal["status"],
        "boundary_status": boundary["status"],
    }
