"""端到端综合场景：一份连续增量保存的文书，覆盖题面三个关键要求。

场景（修订 r0..r5，每个修订 100 字节）：
  r0  页面 P -> 正文 C1 -> C2（body）；表单 F 经 field 边指向 V1（字段 f1）
  r1  认证签章 S1，允许字段 f1 后填
  r2  允许字段 f1 填值（V1 摘要变化），同版新增 body 边 C2 -> V1，
      使 V1 在该版结束时已成为普通页面正文的一部分 -> 同版即越界（最早）
  r3  改写正文 C2                                                     S1 再次越界
  r4  把 C2 改回 r0 的原摘要                                          早先越界仍保留
  r4  新增认可签章 S2（从 r4 状态独立计算覆盖）
  r5  再次改写 C2                                                     S1、S2 均越界
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sigcheck import inspect_document, load_document

MODEL = {
    "revisions": [
        {"revision_id": f"r{i}", "start": i * 100, "end": (i + 1) * 100}
        for i in range(6)
    ],
    "objects": [
        {"object_id": "P", "revision_id": "r0", "digest": "d-P"},
        {"object_id": "C1", "revision_id": "r0", "digest": "d-C1"},
        {"object_id": "C2", "revision_id": "r0", "digest": "d-C2-original"},
        {"object_id": "F", "revision_id": "r0", "digest": "d-F"},
        {"object_id": "V1", "revision_id": "r0", "digest": "d-V1-empty"},
        {"object_id": "SIG1", "revision_id": "r1", "digest": "d-SIG1"},
        {"object_id": "V1", "revision_id": "r2", "digest": "d-V1-filled"},
        {"object_id": "C2", "revision_id": "r3", "digest": "d-C2-tampered"},
        {"object_id": "C2", "revision_id": "r4", "digest": "d-C2-original"},
        {"object_id": "SIG2", "revision_id": "r4", "digest": "d-SIG2"},
        {"object_id": "C2", "revision_id": "r5", "digest": "d-C2-again"},
    ],
    "references": [
        {
            "source_id": "P",
            "target_id": "C1",
            "kind": "body",
            "added_in": "r0",
            "removed_in": None,
            "field_name": None,
        },
        {
            "source_id": "C1",
            "target_id": "C2",
            "kind": "body",
            "added_in": "r0",
            "removed_in": None,
            "field_name": None,
        },
        {
            "source_id": "P",
            "target_id": "F",
            "kind": "field",
            "added_in": "r0",
            "removed_in": None,
            "field_name": "f1",
        },
        {
            "source_id": "F",
            "target_id": "V1",
            "kind": "field",
            "added_in": "r0",
            "removed_in": None,
            "field_name": "f1",
        },
        {
            "source_id": "C2",
            "target_id": "V1",
            "kind": "body",
            "added_in": "r2",
            "removed_in": None,
            "field_name": None,
        },
    ],
    "signatures": [
        {
            "signature_id": "S1",
            "revision_id": "r1",
            "byte_range": [100, 170],
            "signature_object_id": "SIG1",
            "sig_type": "certifying",
            "allowed_fields": ["f1"],
        },
        {
            "signature_id": "S2",
            "revision_id": "r4",
            "byte_range": [400, 460],
            "signature_object_id": "SIG2",
            "sig_type": "approval",
            "allowed_fields": [],
        },
    ],
    "page_root_ids": ["P"],
}


def main() -> int:
    document = load_document(MODEL)
    report = inspect_document(document)

    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))

    s1 = report.assessment_for("S1")
    s2 = report.assessment_for("S2")

    checks = [
        ("全文书存在越界", report.violated is True),
        (
            "S1 最早越界定格在 r2（同版填值并接入正文）",
            s1.earliest_violation.revision_id == "r2",
        ),
        (
            "S1 最早越界对象为 V1 且带 body 引用链 P->C1->C2->V1",
            s1.earliest_violation.object_id == "V1"
            and [h.target_id for h in s1.earliest_violation.chain]
            == ["C1", "C2", "V1"]
            and all(h.kind == "body" for h in s1.earliest_violation.chain),
        ),
        (
            "r3 的 C2 正文改写也被记录，且 r2 越界在改回原样后仍可查",
            {
                (f.revision_id, f.object_id)
                for f in s1.findings
                if f.rule_id == "object_modified"
            }
            >= {("r2", "V1"), ("r3", "C2"), ("r4", "C2"), ("r5", "C2")},
        ),
        (
            "r2 的 V1 未被记为允许后填（该版已被正文引用，豁免失效）",
            not any(
                f.rule_id == "allowed_field_fill"
                and f.revision_id == "r2"
                and f.object_id == "V1"
                for f in s1.findings
            ),
        ),
        ("S2 从 r4 独立计算，r3 事件不算 S2 越界", s2.violated is True),
        ("S2 最早越界为 r5", s2.earliest_violation.revision_id == "r5"),
        (
            "S2 覆盖范围独立包含 r4 时已 body 可达的 V1",
            "V1" in s2.covered_object_ids,
        ),
        (
            "全文书最早越界为 S1 的 r2",
            report.earliest_violation.revision_id == "r2",
        ),
    ]

    print("\n=== 断言结果 ===")
    ok = True
    for name, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
