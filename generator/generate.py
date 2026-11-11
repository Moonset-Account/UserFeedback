"""生成器命令行：产出虚构数据、验收场景的独立真值。

用法：
    python3 generator/generate.py --out data
产物：
    data/experiments.json     源站数据（源站与采集端可见的唯一数据通道是 HTTP）
    data/ground_truth.json    独立真值（仅供 tests 使用，采集器不读取）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许以脚本方式直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector.loader import load_spec  # noqa: E402
from collector.serializer import serialize_path, serialize_query  # noqa: E402
from generator.dataset import build_experiments  # noqa: E402
from generator.reference_filters import apply_filters  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "openapi" / "experiments.openapi.yaml"
SCENARIOS_PATH = ROOT / "fixtures" / "scenarios.json"


def _expected_request(spec, scenario: dict) -> dict:
    """用采集器自身构造“期望请求”，并由测试中的黄金串二次校验。"""
    op = spec.operation(scenario["operationId"])
    params = scenario.get("params") or {}
    path = op.path_template
    query_frags: list[str] = []
    omitted: list[str] = []
    for parameter in op.path_params:
        if parameter.name in params:
            frag = serialize_path(parameter.name, params[parameter.name],
                                  parameter.raw)
            path = path.replace("{" + parameter.name + "}", frag)
    for parameter in op.query_params:
        if parameter.name in params:
            frag, miss = serialize_query(parameter.name, params[parameter.name],
                                         parameter.raw)
            if miss:
                omitted.extend(miss)
            elif frag is not None:
                query_frags.append(frag)
    return {
        "method": "GET",
        "path": spec.server_path + path,
        "query_string": "&".join(query_frags),
        "omitted": omitted,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成虚构实验数据与测试真值")
    parser.add_argument("--out", default=str(ROOT / "data"),
                        help="输出目录（默认项目内 data/）")
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    experiments = build_experiments()
    (out_dir / "experiments.json").write_text(
        json.dumps(experiments, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    spec = load_spec(SPEC_PATH)
    scenarios_raw = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))
    ground_truth: dict = {"scenarios": {}}

    for group in ("run_demo", "acceptance"):
        for scenario in scenarios_raw.get(group, []):
            if scenario.get("disabled"):
                continue
            sid = scenario["id"]
            entry: dict = {"kind": scenario["kind"], "title": scenario["title"]}
            if scenario["kind"] == "rejection":
                entry["expect"] = scenario["expect"]
                ground_truth["scenarios"][sid] = entry
                continue
            entry["request"] = _expected_request(spec, scenario)
            parsed = _simulate_parse(spec, scenario, entry["request"])
            if scenario["operationId"] == "listExperiments":
                rows = apply_filters(experiments, parsed)
                entry["result_ids"] = [r["id"] for r in rows]
            else:
                wanted = _path_wanted_id(scenario, parsed)
                rows = [r for r in experiments if r["id"] == wanted]
                entry["result_ids"] = [r["id"] for r in rows]
            entry["expect"] = scenario.get("expect", {})
            ground_truth["scenarios"][sid] = entry

    (out_dir / "ground_truth.json").write_text(
        json.dumps(ground_truth, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[generator] 写入 {out_dir / 'experiments.json'}（{len(experiments)} 条）")
    print(f"[generator] 写入 {out_dir / 'ground_truth.json'}"
          f"（{len(ground_truth['scenarios'])} 个场景真值）")
    return 0


def _simulate_parse(spec, scenario: dict, expected: dict) -> dict:
    """用源站解析器对生成出的期望请求做一次往返解析，得到语义参数。"""
    from server.echo_parser import parse_path, parse_query, split_raw_query

    op = spec.operation(scenario["operationId"])
    parsed: dict = {}
    if op.path_params:
        parsed.update(parse_path(expected["path"],
                                 spec.server_path + op.path_template,
                                 op.path_params))
    if op.query_params:
        parsed.update(parse_query(
            split_raw_query(expected["query_string"]), op.query_params
        ))
    return parsed


def _path_wanted_id(scenario: dict, parsed: dict) -> str | None:
    if "exp_id" in parsed:
        val = parsed["exp_id"]
        if isinstance(val, dict):
            return val.get("id")
        return val
    return None


if __name__ == "__main__":
    raise SystemExit(main())
