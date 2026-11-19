"""生成端到端示例案例 JSON（纯合成数据，仅用于演示与手工核验）。

运行::

    python3 examples/generate_sample_case.py
"""

from __future__ import annotations

import base64
import json
import os

CLUSTER = 16  # 演示用小簇，便于肉眼核对偏移


def main() -> None:
    # 原文件 36 字节 = 2 个满簇(16+16) + 末簇 4 字节，共 3 个逻辑簇。
    original = (
        b"0123456789abcdef"   # 逻辑簇 0 -> 物理簇 101，完好
        b"FEDCBA9876543210"   # 逻辑簇 1 -> 物理簇 202，删除后被 obj-42 占用又释放
        b"WXYZ"               # 逻辑簇 2 -> 物理簇 303，窗内 [2,4) 被改写
    )
    assert len(original) == 36

    chunks = [
        # 簇 101：采集成功，内容即原文。
        {
            "physical_cluster": 101,
            "status": "ok",
            "data": base64.b64encode(original[0:16]).decode("ascii"),
        },
        # 簇 202：虽然又被释放且采集内容与原文逐字节相同，
        # 但删除后曾分配给其他对象，必须拒绝。
        {
            "physical_cluster": 202,
            "status": "ok",
            "data": base64.b64encode(original[16:32]).decode("ascii"),
        },
        # 簇 303：前 2 字节未变，[2,4) 被改写；簇内其余为采集到的填充。
        {
            "physical_cluster": 303,
            "status": "ok",
            "data": base64.b64encode(b"WX##" + b"padding-data").decode("ascii"),
        },
    ]

    case = {
        "file_name": "deleted_evidence.dat",
        "file_size": 36,
        "cluster_size": CLUSTER,
        "deletion_time": 1000,
        "acquisition_time": 2000,
        "cluster_chain": [101, 202, 303],
        "chunks": chunks,
        "allocations": [
            {
                "physical_cluster": 202,
                "start_time": 1200,
                "end_time": 1500,
                "object_id": "obj-42",
                "event_id": "EV-ALLOC-202",
            }
        ],
        "overwrites": [
            {
                "physical_cluster": 303,
                "phy_start": 2,
                "phy_end": 4,
                "time": 1700,
                "event_id": "EV-OW-303",
            },
            # 删除前的旧改写，不影响删除时原文判定，应被忽略。
            {
                "physical_cluster": 101,
                "phy_start": 0,
                "phy_end": 1,
                "time": 900,
                "event_id": "EV-OW-OLD",
            },
        ],
        "record_gaps": [],
    }

    out_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(out_dir, "case.sample.json")
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(case, handle, ensure_ascii=False, indent=2)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
