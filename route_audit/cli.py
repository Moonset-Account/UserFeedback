"""命令行入口：对本地合成场景文件做一次完整隔离审查并打印 JSON。

用法::

    python3 -m route_audit.cli examples/withdraw_scenario.json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .loader import load_scenario_file
from .serialize import finding_to_dict, trace_to_dict


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="路由隔离审查：逐跳 LPM 核对出域路径与中转授权"
    )
    parser.add_argument("scenario", help="本地 JSON 场景文件路径")
    args = parser.parse_args(argv)

    scenario = load_scenario_file(args.scenario)
    report = scenario.run()

    payload = {
        "baseline": {
            probe_id: trace_to_dict(trace)
            for probe_id, trace in report.baseline.items()
        },
        "findings": [finding_to_dict(f) for f in report.findings],
        "traces_by_change": {
            change_id: {pid: trace_to_dict(t) for pid, t in traces.items()}
            for change_id, traces in report.traces_by_change.items()
        },
    }
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    # 存在未授权中转结论时以非零码退出，方便流水线判定
    return 1 if report.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
