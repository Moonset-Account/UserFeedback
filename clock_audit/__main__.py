"""命令行入口：python -m clock_audit <flow.json>

读取 JSON 描述的简化数据流，输出每个 Compare 的审计结论。
退出码：存在 REJECT 时为 1，否则为 0（PENDING 不视为失败，但会标注）。
"""

from __future__ import annotations

import sys

from .audit import VERDICT_REJECT, Auditor
from .loader import load_events


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("用法: python -m clock_audit <flow.json>", file=sys.stderr)
        return 2
    events = load_events(argv[0])
    auditor = Auditor()
    findings = auditor.ingest(events)
    for f in findings:
        print(f"[{f.verdict}] {f.compare_id}: {f.reason}")
        print(f"  deadline 证据路径: {' -> '.join(f.deadline_evidence)}")
        print(f"  now      证据路径: {' -> '.join(f.now_evidence)}")
        if f.diff_ns is not None:
            print(f"  diff = {f.diff_ns} ns, expired = {f.expired}")
        for note in f.notes:
            print(f"  备注: {note}")
    return 1 if any(f.verdict == VERDICT_REJECT for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
