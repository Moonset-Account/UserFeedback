"""采集器核心逻辑测试：正常场景与边界场景，对照独立真值断言。"""
from __future__ import annotations

from iiif_collector import collect

from .conftest import manifest_url
from .expected_truth import render


def _issue_codes(result, severity=None):
    return {
        i.code
        for i in result.issues
        if severity is None or i.severity == severity
    }


def test_demo_normal_matches_truth(server):
    base_url = server["base_url"]
    truth = render("demo-normal", base_url)
    result = collect(manifest_url(base_url, "demo-normal"))
    d = result.to_dict()

    # ---- 整体状态与统计 ----
    assert d["status"] == truth["status"]
    assert d["manifest_label"] == truth["manifest_label"]
    for key in (
        "canvas_count", "image_count", "range_count", "missing_image_label_count",
        "choice_alternative_count", "shared_image_id_count", "cyclic_range_count",
        "error_count",
    ):
        assert d["stats"][key] == truth[key], f"统计字段 {key} 不符"

    # ---- Canvas 标识独立、页面顺序保留 ----
    assert [c.canvas_id for c in result.canvases] == truth["canvas_ids"]
    assert [len(c.images) for c in result.canvases] == truth["images_per_canvas"]

    # ---- 图像标识 ≠ Canvas 标识：任何图像 ID 都不等于任何 Canvas ID ----
    all_image_ids = {img.image_id for c in result.canvases for img in c.images if img.image_id}
    all_canvas_ids = {c.canvas_id for c in result.canvases}
    assert all_image_ids.isdisjoint(all_canvas_ids)

    # ---- Choice：c2 上两个候选，首个非 alternative ----
    c2 = result.canvases[1]
    assert len(c2.images) == 2
    assert c2.images[0].is_choice_alternative is False
    assert c2.images[1].is_choice_alternative is True
    assert c2.images[0].label == "蕨类标准彩色照（默认）"
    assert c2.images[1].label == "蕨类历史照片（候选）"

    # ---- 同一图像多处使用不合并页面 ----
    owners = {
        c.canvas_id
        for c in result.canvases
        for img in c.images
        if img.image_id == truth["shared_image_id"]
    }
    assert owners == set(truth["shared_image_canvases"])
    assert len(result.canvases) == truth["canvas_count"]  # 页面数量未因共享而减少

    # ---- 章节复用 Canvas：r1、r2 都引用 c2 ----
    range_by_id = {r.range_id: r for r in result.ranges}
    for rid, refs in truth["range_canvas_refs"].items():
        assert range_by_id[rid].referenced_canvas_ids == refs
    root = range_by_id[[r.range_id for r in result.ranges if r.label == "标本册总目"][0]]
    assert root.child_range_ids == truth["root_range_children"]
    # 章节引用 ID 与 Canvas/图像 ID 分属不同命名
    assert set(root.child_range_ids).isdisjoint(all_canvas_ids)

    # ---- 来源证据路径可追溯 ----
    assert result.canvases[0].source_path == "/items/0"
    assert result.canvases[0].images[0].source_path.startswith("/items/0/items/0/items/")
    assert result.ranges[0].source_path.startswith("/structures/")


def test_demo_boundary_matches_truth(server):
    base_url = server["base_url"]
    truth = render("demo-boundary", base_url)
    result = collect(manifest_url(base_url, "demo-boundary"))
    d = result.to_dict()

    # ---- 部分成功：可用页面仍全部保留 ----
    assert d["status"] == "partial"
    for key in (
        "canvas_count", "image_count", "range_count", "missing_image_label_count",
        "shared_image_id_count", "cyclic_range_count",
    ):
        assert d["stats"][key] == truth[key], f"统计字段 {key} 不符"
    assert [c.canvas_id for c in result.canvases] == truth["usable_canvas_ids"]

    # ---- 缺失图像描述：定位到具体图像与路径 ----
    no_label = [
        img
        for c in result.canvases for img in c.images
        if any(i.code == "MISSING_IMAGE_LABEL" for i in img.issues)
    ]
    assert len(no_label) == 1
    assert no_label[0].image_id == truth["missing_label_image_id"]
    assert no_label[0].label is None
    assert no_label[0].issues[0].path.startswith("/items/")

    # ---- 循环 Range：两个节点标记 cyclic，且解析不会死循环 ----
    cyclic_ids = {r.range_id for r in result.ranges if r.cyclic}
    assert cyclic_ids == set(truth["cyclic_range_ids"])
    cycle_issues = [i for i in result.issues if i.code == "CYCLIC_RANGE_REF"]
    assert cycle_issues and all(i.severity == "error" for i in cycle_issues)
    assert all(i.path.startswith("/structures/") for i in cycle_issues)

    # ---- 悬空引用：错误带具体路径与引用 ID ----
    dangling_canvas = [i for i in result.issues if i.code == "DANGLING_CANVAS_REF"]
    dangling_range = [i for i in result.issues if i.code == "DANGLING_RANGE_REF"]
    assert any(i.ref == truth["dangling_canvas_ref"] for i in dangling_canvas)
    assert any(i.ref == truth["dangling_range_ref"] for i in dangling_range)
    assert all(i.path.startswith("/structures/") for i in dangling_canvas + dangling_range)

    # ---- 非标准 context 与非 painting 标注产生警告 ----
    assert truth["error_code_set"].issubset(_issue_codes(result, "error"))
    assert truth["warning_code_set"].issubset(_issue_codes(result, "warning"))

    # ---- 无图像 Canvas 保留并给出问题 ----
    empty = next(c for c in result.canvases if c.canvas_id == truth["empty_canvas_id"])
    assert empty.images == []
    assert any(i.code == "CANVAS_WITHOUT_IMAGE" for i in empty.issues)

    # ---- 共享图像仍不合并页面 ----
    shared_owners = {
        c.canvas_id for c in result.canvases for img in c.images
        if img.image_id and img.image_id.endswith("shared-scale-card/full/full/0/default.jpg")
    }
    assert len(shared_owners) == 2


def test_full_acceptance_matches_truth(server):
    base_url = server["base_url"]
    truth = render("full-acceptance", base_url)
    result = collect(manifest_url(base_url, "full-acceptance"))
    d = result.to_dict()

    assert d["status"] == "partial"
    for key in (
        "canvas_count", "image_count", "range_count", "missing_image_label_count",
        "choice_alternative_count", "shared_image_id_count", "cyclic_range_count",
    ):
        assert d["stats"][key] == truth[key], f"统计字段 {key} 不符"
    assert [len(c.images) for c in result.canvases] == truth["images_per_canvas"]

    # 自循环 Range
    loop = next(r for r in result.ranges if r.range_id == truth["self_cycle_range_id"])
    assert loop.cyclic is True
    assert any(i.code == "CYCLIC_RANGE_REF" for i in loop.issues)

    # 根章节含 4 个子引用（3 个正常 Range + 1 个循环 Range）
    root = next(r for r in result.ranges if r.range_id == truth["root_range_id"])
    assert len(root.child_range_ids) == truth["root_children_count"]

    # 无图像页保留
    empty = next(c for c in result.canvases if c.canvas_id == truth["empty_canvas_id"])
    assert empty.images == []


def test_fetch_failure_has_specific_message(tmp_path):
    result = collect("http://127.0.0.1:1/no-such-manifest.json")
    assert result.status == "failed"
    assert result.issues[0].code == "FETCH_FAILED"
    assert result.issues[0].message

    missing_file = tmp_path / "not-exist.json"
    result2 = collect(str(missing_file))
    assert result2.status == "failed"
    assert "不存在" in result2.issues[0].message
