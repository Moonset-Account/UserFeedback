"""批准流程测试：未批准不发布、批准保留旧规则与受影响范围、批准后重采放行。"""

import json
from pathlib import Path

import pytest


def _run(engine, generated_dir: Path, scenario: str):
    url = (generated_dir / "samples" / scenario / "index.html").as_uri()
    return engine.run(scenario=scenario, url=url)


def test_unapproved_semantics_never_published(engine, generated_dir):
    result = _run(engine, generated_dir, "label")
    assert result["published"] == []
    # 存储中的正式结果文件同样不含湿重记录
    persisted = engine.storage.load_run(result["run_id"])
    assert persisted["published"] == []
    assert len(persisted["quarantined"]) == 4


def test_approval_creates_version_and_preserves_old_rule(engine, generated_dir):
    result = _run(engine, generated_dir, "label")
    old_field = engine.storage.load_contract("v1").field("dry_weight")

    outcome = engine.approve(
        run_id=result["run_id"],
        field_keys=["dry_weight"],
        approved_by="审核员-李",
        reason="确认台账已切换为湿重口径",
    )
    assert outcome["new_contract_version"] == "v2"
    assert outcome["affected_scope"]["field_keys"] == ["dry_weight"]
    assert outcome["affected_scope"]["record_count"] == 4
    assert outcome["affected_scope"]["row_indices"] == [0, 1, 2, 3]

    # 旧契约 v1 原样保留
    v1 = engine.storage.load_contract("v1")
    assert v1.field("dry_weight").allowed_labels == old_field.allowed_labels
    assert "湿重" not in "".join(v1.field("dry_weight").allowed_labels)

    # 新契约保留父版本指针，且仅 dry_weight 改变，其余字段不动
    v2 = engine.storage.load_contract("v2")
    assert v2.parent_version == "v1"
    assert "湿重" in "".join(v2.field("dry_weight").allowed_labels)
    assert v2.field("moisture") == v1.field("moisture")
    assert v2.approved_by == "审核员-李"

    # 批准记录包含完整旧规则与受影响记录范围
    approvals = engine.storage.list_approvals()
    assert len(approvals) == 1
    record = approvals[0]
    assert record["old_rules"]["dry_weight"]["allowed_labels"] == list(old_field.allowed_labels)
    assert record["affected_scope"]["record_count"] == 4
    assert Path(record["contract_file"]).exists()


def test_rerun_with_approved_contract_publishes(engine, generated_dir):
    label = _run(engine, generated_dir, "label")
    engine.approve(run_id=label["run_id"], field_keys=["dry_weight"],
                   approved_by="审核员-李", reason="确认湿重口径")
    rerun = engine.run(
        scenario="label-rerun",
        url=(generated_dir / "samples" / "label" / "index.html").as_uri(),
        contract_version="v2",
    )
    assert rerun["status"] == "passed"
    assert len(rerun["published"]) == 4
    assert rerun["quarantined"] == []


def test_approval_of_unit_scale_then_rerun(engine, generated_dir):
    result = _run(engine, generated_dir, "unit")
    engine.approve(run_id=result["run_id"], field_keys=["dry_weight"],
                   approved_by="审核员-王", reason="确认单位切换为 mg")
    v2 = engine.storage.load_contract("v2")
    assert v2.field("dry_weight").expected_unit == "mg"
    rerun = engine.run(
        scenario="unit-rerun",
        url=(generated_dir / "samples" / "unit" / "index.html").as_uri(),
        contract_version="v2",
    )
    assert rerun["status"] == "passed"
    # 数值按页面单位原样解析，证据中保留单位倍数变化历史
    assert rerun["published"][0]["values"]["dry_weight"] == 2450.0


def test_partial_approval_leaves_other_fields_guarded(engine, generated_dir):
    # median 场景只批准 dry_weight：该列通过，但隔离范围逻辑仍记录完整
    result = _run(engine, generated_dir, "median")
    engine.approve(run_id=result["run_id"], field_keys=["dry_weight"],
                   approved_by="审核员-赵", reason="确认中位数口径")
    v2 = engine.storage.load_contract("v2")
    assert v2.field("dry_weight").required_stat == "median"
    assert v2.field("crude_protein").required_stat == "mean"  # 未批准字段规则不变


def test_approve_without_fields_raises(engine, generated_dir):
    result = _run(engine, generated_dir, "label")
    with pytest.raises(ValueError):
        engine.approve(run_id=result["run_id"], field_keys=[],
                       approved_by="x", reason="")


def test_second_approval_chains_v3(engine, generated_dir):
    r1 = _run(engine, generated_dir, "label")
    engine.approve(run_id=r1["run_id"], field_keys=["dry_weight"],
                   approved_by="审核员-李", reason="第一次批准")
    r2 = engine.run(scenario="median-2",
                    url=(generated_dir / "samples" / "median" / "index.html").as_uri(),
                    contract_version="v2")
    engine.approve(run_id=r2["run_id"], field_keys=["dry_weight"],
                   approved_by="审核员-李", reason="第二次批准")
    v3 = engine.storage.load_contract("v3")
    assert v3.parent_version == "v2"
    versions = [v["version"] for v in engine.storage.list_contract_versions()]
    assert versions == ["v1", "v2", "v3"]
