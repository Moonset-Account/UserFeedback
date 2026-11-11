"""样例生成器：为每个虚构场景生成导出包与独立真值。

用法：
    python3 -m generator.generate --data-dir runtime_data

目录约定：
    <data-dir>/generated/<scenario>/payload.zip      源站下发的导出包
    <data-dir>/ground_truth/<scenario>.json          独立预期（仅供测试，采集器禁止读取）
    <data-dir>/downloads/                            采集器下载物（运行时产生）
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from generator.payloads import build_payload
from generator.scenarios import (
    BASE_VERSION,
    EXPERIMENT_PREFIX,
    SCENARIOS,
    SCENARIO_ORDER,
    VERSION_STEP,
)


def experiment_id_for(scenario_id: str) -> str:
    return f"{EXPERIMENT_PREFIX}-{scenario_id.upper()}"


def generate(data_dir: Path) -> dict:
    generated_dir = data_dir / "generated"
    truth_dir = data_dir / "ground_truth"
    downloads_dir = data_dir / "downloads"
    generated_dir.mkdir(parents=True, exist_ok=True)
    truth_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir.mkdir(parents=True, exist_ok=True)

    index: list[dict] = []
    for sid in SCENARIO_ORDER:
        spec = SCENARIOS[sid]
        exp_id = experiment_id_for(sid)
        # 身份不符场景：包内 manifest 写入另一个实验编号
        identity_id = exp_id if spec.identity_match else f"{EXPERIMENT_PREFIX}-FOREIGN"
        payload = build_payload(experiment_id=exp_id, version=BASE_VERSION, identity_id=identity_id)

        scen_dir = generated_dir / sid
        scen_dir.mkdir(parents=True, exist_ok=True)
        payload_path = scen_dir / "payload.zip"
        payload_path.write_bytes(payload)

        truth = {
            "scenario_id": sid,
            "scenario_title": spec.title,
            "experiment_id": exp_id,
            "declared_experiment_id": identity_id,
            "base_version": BASE_VERSION,
            "changed_version": VERSION_STEP,
            "js_rendered": spec.js_rendered,
            "token_plan": list(spec.token_plan),
            "version_bump_on_issue": spec.version_bump_on_issue,
            "identity_match": spec.identity_match,
            "payload_path": str(payload_path),
            "payload_size": len(payload),
            "expect": {
                "outcome": spec.expect_outcome,
                "attempts": spec.expect_attempts,
                "refreshes": spec.expect_refreshes,
            },
        }
        (truth_dir / f"{sid}.json").write_text(
            json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        index.append({
            "scenario_id": sid,
            "title": spec.title,
            "experiment_id": exp_id,
            "js_rendered": spec.js_rendered,
        })

    (data_dir / "scenario_index.json").write_text(
        json.dumps({"scenarios": index}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"data_dir": str(data_dir), "scenarios": len(index)}


def main() -> None:
    parser = argparse.ArgumentParser(description="生成虚构实验样例数据与独立真值")
    parser.add_argument("--data-dir", default="runtime_data", help="运行期数据目录")
    args = parser.parse_args()
    result = generate(Path(args.data_dir))
    print(f"已生成 {result['scenarios']} 个场景的数据 -> {result['data_dir']}")


if __name__ == "__main__":
    main()
