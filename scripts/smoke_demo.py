"""端到端冒烟脚本：合成一批含全部违规类型与失败留档的数据并打印报告。

运行方式（仓库根目录）：python3 -I scripts/smoke_demo.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 直接以脚本路径运行时，把仓库根目录加入模块搜索路径。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transit_audit import (
    Attempt,
    Batch,
    ByteRange,
    DeliveryResult,
    HopReceipt,
    NodeKind,
    RouteNode,
    Segment,
    verify_batch,
)


def main() -> None:
    seg = Segment("seg-1", ByteRange(0, 100))
    route = (
        RouteNode("relay-a", 1, NodeKind.RELAY),
        RouteNode("relay-b", 2, NodeKind.RELAY),
        RouteNode("region-c", 3, NodeKind.REGION),
    )

    failed = Attempt(1, route, (), DeliveryResult.FAILED)
    receipts = (
        # relay-a：覆盖缺口 [45,55) 且越界 [100,130)
        HopReceipt("relay-a", NodeKind.RELAY, (ByteRange(0, 45), ByteRange(55, 130))),
        # relay-b 整跳缺失（无回执）
        HopReceipt("region-c", NodeKind.REGION, (ByteRange(0, 100),)),
    )
    delivered = Attempt(2, route, receipts, DeliveryResult.DELIVERED)

    report = verify_batch(Batch((seg,), {"seg-1": (failed, delivered)}))
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    assert not report.compliant
    kinds = [v.kind.value for v in report.violations]
    assert kinds == ["out_of_bounds", "missing_coverage", "missing_hop"], kinds
    assert report.segment_reports[0].archived_attempts[0].attempt_no == 1
    print("\nsmoke demo assertions passed")


if __name__ == "__main__":
    main()
