"""端到端冒烟脚本：对合成示例案例跑完整判定并做真实断言。"""

from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from readonly_forensics.cli import main as cli_main  # noqa: E402
from readonly_forensics.model import RecoveryCase  # noqa: E402
from readonly_forensics.engine import recover_file  # noqa: E402

CASE_PATH = os.path.join(os.path.dirname(__file__), "case.sample.json")


def run() -> None:
    # CLI JSON 路径
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli_main([CASE_PATH])
    assert code == 0
    report = json.loads(buf.getvalue())

    assert report["file_size"] == 36
    assert report["reason_counts"] == {
        "overwritten": 2,
        "reallocated": 16,
        "recovered": 18,
    }
    reasons = [seg["reason"] for seg in report["segments"]]
    assert reasons == [
        "recovered",
        "reallocated",
        "recovered",
        "overwritten",
    ], reasons

    # 物理回查不变量：文件偏移 - 逻辑簇起点 == 簇内偏移
    for seg in report["segments"]:
        if seg["physical_cluster"] is None:
            continue
        expected = (
            seg["file_offset_start"]
            - seg["logical_cluster"] * report["cluster_size"]
        )
        assert expected == seg["cluster_offset_start"], seg

    # API 路径：materialize 只含已恢复字节，不做猜测填充
    with open(CASE_PATH, "r", encoding="utf-8") as handle:
        case = RecoveryCase.from_dict(json.load(handle))
    api_report = recover_file(case)
    assert api_report.materialize() == b"0123456789abcdef" + b"WX"
    assert not api_report.fully_recovered
    assert sum(s.length for s in api_report.segments) == 36

    print("smoke assertions OK: 4 segments, 18 recovered / 18 rejected bytes")


if __name__ == "__main__":
    run()
