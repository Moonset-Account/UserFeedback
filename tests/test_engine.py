"""核心引擎测试：乱序、重复、部分字段出错、未收到结束标志，以及重跑去重。

所有采集都走引擎公开入口 ingest_line / 传输层 run_file_collection，
断言数据来自 tests/expected_truth.py（独立手写真值）。
"""
from __future__ import annotations

from conftest import collect_scenario_in_memory
from expected_truth import (
    COMPREHENSIVE_EXPECTED,
    DEMO_EDGE_EXPECTED,
    DEMO_NORMAL_EXPECTED,
    DUPLICATES_EXPECTED,
    FIELD_ERRORS_EXPECTED,
    ILLEGAL_PATHS_EXPECTED,
    OUT_OF_ORDER_EXPECTED,
)

from rip_collector import CollectorEngine
from rip_collector.parser import PROTOCOL_VERSION, parse_line


def _rejected_reasons(engine: CollectorEngine) -> list[str]:
    return [d.reason for d in engine.decisions if not d.accepted]


# ---------------------------------------------------------------------
# 场景 1：正常流 —— 子先于父、乱序插入、错误恢复、重复补丁同时验证
# ---------------------------------------------------------------------
def test_demo_normal_matches_independent_truth():
    engine = collect_scenario_in_memory("demo_normal")
    result = engine.result()
    truth = DEMO_NORMAL_EXPECTED

    assert result["status"] == truth["status"]
    assert result["complete"] is True
    assert result["final_snapshot"]["tree"] == truth["tree"]
    assert result["final_snapshot"]["errors"] == []
    assert result["final_snapshot"]["unresolved"] == []
    assert _rejected_reasons(engine) == truth["rejected_reasons"]


def test_child_patch_replayed_after_parent_arrives():
    """子补丁先于父节点：seq=3 先 pending，seq=5 创建父节点后自动 replay。"""
    engine = collect_scenario_in_memory("demo_normal")
    actions = [(d.seq, d.action) for d in engine.decisions]
    assert (3, "pending") in actions
    assert (3, "replay") in actions
    # replay 由 seq=5 的父补丁触发，挂起决策必然早于重放决策
    assert actions.index((3, "pending")) < actions.index((3, "replay"))
    # 重放发生时父补丁已在暂存树生效（重放快照里 views 已存在）
    replay_snapshot = engine.snapshots[
        next(d.snapshot_no for d in engine.decisions if (d.seq, d.action) == (3, "replay"))
    ]
    assert replay_snapshot["tree"]["metrics"]["views"] == 1280


def test_every_publish_is_immutable_snapshot():
    """发布快照不可变：后续补丁不能改写历史快照对象。"""
    engine = collect_scenario_in_memory("demo_normal")
    first = engine.snapshots[1]
    first_tree_id = id(first["tree"])
    first_views = (first["tree"].get("metrics") or {}).get("views")
    final = engine.snapshots[-1]
    assert id(final["tree"]) != first_tree_id
    # 历史快照保持当时形态（seq=1 时 metrics 尚不存在）
    assert first_views is None


# ---------------------------------------------------------------------
# 场景 2：未收到结束标志 —— 暂存树绝不能标成完整
# ---------------------------------------------------------------------
def test_demo_edge_interrupted_never_complete():
    engine = collect_scenario_in_memory("demo_edge")
    result = engine.result()
    truth = DEMO_EDGE_EXPECTED

    assert result["status"] == truth["status"]
    assert result["complete"] is False
    assert result["interrupted"] is True
    assert result["interrupt_reason"]
    pending_paths = [u["path"] for u in result["final_snapshot"]["unresolved"]]
    assert pending_paths == truth["pending_paths"]
    # 中断前已恢复的字段与已收敛的列表仍然保留
    tree = result["final_snapshot"]["tree"]
    assert tree["funding"] == DEMO_NORMAL_EXPECTED["tree"]["funding"]
    assert len(tree["references"]) == truth["references_len"]
    # 所有历史快照都不允许出现 complete=True
    assert all(snap["complete"] is False for snap in engine.snapshots)


def test_file_transport_marks_eof_without_end(tmp_path, file_collect):
    """文件传输：录制文件没有 end 时，EOF 自动标记 interrupted。"""
    engine = file_collect("demo_edge", tmp_path)
    assert engine.result()["status"] == "interrupted"
    assert engine.result()["complete"] is False


# ---------------------------------------------------------------------
# 场景 3：乱序插入与位置漂移
# ---------------------------------------------------------------------
def test_out_of_order_list_converges():
    engine = collect_scenario_in_memory("out_of_order")
    result = engine.result()
    truth = OUT_OF_ORDER_EXPECTED
    refs = result["final_snapshot"]["tree"]["references"]

    assert result["status"] == truth["status"]
    assert [r["id"] for r in refs] == truth["references_ids"]
    assert [r["title"] for r in refs] == truth["references_titles"]
    assert result["final_snapshot"]["tree"]["tags"] == truth["tags"]
    assert _rejected_reasons(engine) == truth["rejected_reasons"]


def test_list_order_visible_in_intermediate_snapshots():
    """位置变化必须能在逐步快照里看到，而不是只在最终结果正确。"""
    engine = collect_scenario_in_memory("out_of_order")
    # 乱序到达：先 REF-A(index=2) 再 REF-B(index=0)，第二帧按 (index, seq) 重排为 B,A
    snap_after_two = next(
        s for s in engine.snapshots
        if s["stats"].get("inserts") == 2
    )
    assert [r["id"] for r in snap_after_two["tree"]["references"]] == ["REF-B", "REF-A"]
    # 第三帧 REF-C(index=3)：按 index 排序仍在最后
    snap_after_three = next(
        s for s in engine.snapshots
        if s["stats"].get("inserts") == 3
    )
    assert [r["id"] for r in snap_after_three["tree"]["references"]] == ["REF-B", "REF-A", "REF-C"]


# ---------------------------------------------------------------------
# 场景 4：重复补丁 + 重复列表项 + 任务重跑不产生重复项
# ---------------------------------------------------------------------
def test_duplicate_seq_and_item_dedup():
    engine = collect_scenario_in_memory("duplicates")
    result = engine.result()
    truth = DUPLICATES_EXPECTED

    assert result["status"] == truth["status"]
    assert result["final_snapshot"]["tree"]["metrics"]["views"] == truth["metrics_views"]
    assert result["final_snapshot"]["tree"]["references"] == truth["references"]
    assert _rejected_reasons(engine) == truth["rejected_reasons"]


def test_rerun_produces_no_duplicate_list_items(tmp_path, file_collect):
    """同一录制文件连续采集两遍（模拟任务重跑），各自列表都不得出现重复项。"""
    run1 = file_collect("duplicates", tmp_path / "run1")
    run2 = file_collect("duplicates", tmp_path / "run2")
    for engine in (run1, run2):
        ids = [r["id"] for r in engine.result()["final_snapshot"]["tree"]["references"]]
        assert ids == ["REF-X", "REF-Y"]
        assert len(ids) == len(set(ids))
    # 两个引擎实例的暂存状态完全隔离（duplicates 场景共 7 条协议消息）
    assert run1.stats["received"] == run2.stats["received"] == 7
    assert run1.result()["status"] == run2.result()["status"] == "complete"


# ---------------------------------------------------------------------
# 场景 5：部分字段出错 —— 错误留源、错误恢复、其他字段不受影响
# ---------------------------------------------------------------------
def test_field_errors_keep_source_and_recover():
    engine = collect_scenario_in_memory("field_errors")
    result = engine.result()
    truth = FIELD_ERRORS_EXPECTED
    snap = result["final_snapshot"]
    tree = snap["tree"]

    assert result["status"] == truth["status"]
    assert tree["funding"] == truth["funding"]
    license_marker = tree["license"]
    assert license_marker[truth["license_error"]["marker"]] is True
    assert license_marker["code"] == truth["license_error"]["code"]
    assert license_marker["source_seq"] == truth["license_error"]["source_seq"]
    # 错误清单保留同一路径与原始错误体
    license_err = next(e for e in snap["errors"] if e["path"] == "/license")
    assert license_err["raw"]["code"] == "FORBIDDEN"
    assert len(tree["authors"]) == truth["authors_len"]
    # funding 已恢复，不在错误清单中
    assert all(e["path"] != "/funding" for e in snap["errors"])


# ---------------------------------------------------------------------
# 场景 6：非法路径 —— 拒绝且留证，流不中断
# ---------------------------------------------------------------------
def test_illegal_paths_are_rejected_with_evidence():
    engine = collect_scenario_in_memory("illegal_paths")
    result = engine.result()
    truth = ILLEGAL_PATHS_EXPECTED

    assert result["status"] == truth["status"]
    assert result["final_snapshot"]["tree"]["metrics"]["views"] == truth["metrics_views"]
    assert result["final_snapshot"]["tree"]["references"] == truth["references"]
    assert _rejected_reasons(engine) == truth["rejected_reasons"]
    # 每条拒绝都有完整证据（含原始消息或坏行文本）
    for decision in (d for d in engine.decisions if not d.accepted):
        assert decision.raw is not None
        assert decision.reason


# ---------------------------------------------------------------------
# 场景 7：综合 —— end 到达但仍有未决项，不得标记完整
# ---------------------------------------------------------------------
def test_comprehensive_ended_unresolved():
    engine = collect_scenario_in_memory("comprehensive")
    result = engine.result()
    truth = COMPREHENSIVE_EXPECTED
    snap = result["final_snapshot"]

    assert result["status"] == truth["status"]
    assert result["complete"] is False
    assert [r["id"] for r in snap["tree"]["references"]] == truth["references_ids"]
    assert [u["path"] for u in snap["unresolved"]] == truth["pending_paths"]
    assert snap["tree"]["license"]["source_seq"] == truth["license_error_seq"]
    assert _rejected_reasons(engine) == truth["rejected_reasons"]


# ---------------------------------------------------------------------
# 协议级单元行为
# ---------------------------------------------------------------------
def test_after_end_messages_are_rejected():
    engine = CollectorEngine()
    engine.ingest_line(f'{{"type":"initial","seq":0,"version":"{PROTOCOL_VERSION}","data":{{"x":1}}}}')
    engine.ingest_line('{"type":"end","seq":1}')
    decision = engine.ingest_line('{"type":"patch","seq":2,"path":"/x","value":2}')
    assert decision.accepted is False
    assert decision.reason == "after_end"
    assert engine.result()["complete"] is True
    assert engine.result()["final_snapshot"]["tree"]["x"] == 1


def test_messages_before_initial_rejected():
    engine = CollectorEngine()
    decision = engine.ingest_line('{"type":"patch","seq":1,"path":"/x","value":1}')
    assert decision.reason == "before_initial"


def test_duplicate_initial_rejected():
    engine = CollectorEngine()
    line = f'{{"type":"initial","seq":0,"version":"{PROTOCOL_VERSION}","data":{{"x":1}}}}'
    engine.ingest_line(line)
    decision = engine.ingest_line(line)
    assert decision.reason == "duplicate_initial"


def test_bad_json_line_does_not_kill_stream():
    engine = CollectorEngine()
    engine.ingest_line(f'{{"type":"initial","seq":0,"version":"{PROTOCOL_VERSION}","data":{{}}}}')
    bad = engine.ingest_line("{not json")
    assert bad.accepted is False and bad.reason == "bad_json"
    good = engine.ingest_line('{"type":"end","seq":1}')
    assert good.accepted is True
    assert engine.result()["status"] == "complete"


def test_interrupt_freezes_staging_and_rejects_later_lines():
    engine = CollectorEngine()
    engine.ingest_line(f'{{"type":"initial","seq":0,"version":"{PROTOCOL_VERSION}","data":{{}}}}')
    engine.mark_interrupted("unit_test_cut")
    decision = engine.ingest_line('{"type":"end","seq":1}')
    assert decision.reason == "after_interrupt"
    assert engine.result()["status"] == "interrupted"


def test_parser_rejects_foreign_protocol_version():
    try:
        parse_line('{"type":"initial","seq":0,"version":"GRAPHQL-DRAFT-X","data":{}}')
    except Exception as exc:  # noqa: BLE001
        assert exc.code == "version_mismatch"
    else:  # pragma: no cover
        raise AssertionError("外部协议版本必须被拒绝")
