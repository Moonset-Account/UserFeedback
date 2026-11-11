"""核心规划算法测试：可满足性、冲突链、确定性、最少移动下界。"""
import json

import pytest

from core.models import Spec, Step
from core.parser import parse_obj
from core.planner import (
    UnsatisfiableError,
    build_target_order,
    check_satisfiable,
    plan_moves,
    weighted_lcs,
)
from core.verifier import verify_order


def _spec(ids, fixed=None, deps=None, groups=None):
    steps = [Step(id=i, name=f"步骤{i}", group=(groups or {}).get(i)) for i in ids]
    spec = Spec(scenario_id="t", title="t", steps=steps,
                fixed=fixed or {}, depends_on={i: list((deps or {}).get(i, [])) for i in ids})
    # 补全同组边（与 parser 行为一致）
    group_last = {}
    for s in steps:
        if s.group:
            if s.group in group_last:
                spec.depends_on[s.id].append(group_last[s.group])
            group_last[s.group] = s.id
    return spec


# --------------------------------------------------------------------------- #
# 多个合法排序：求解器给出确定的一个，验证器独立确认它合法
# --------------------------------------------------------------------------- #
def test_multiple_valid_orders_choose_deterministic():
    spec = _spec(["a", "b", "c", "d"], deps={"c": ["a"], "d": ["b"]})
    t1 = build_target_order(spec)
    t2 = build_target_order(spec)
    assert t1 == t2, "目标序必须确定（重复运行结果一致）"
    assert set(t1) == {"a", "b", "c", "d"}
    v = verify_order(spec, t1)
    assert v.ok, v.checks
    # 该约束确实存在多个合法拓扑序
    assert t1.index("a") < t1.index("c") and t1.index("b") < t1.index("d")


def test_group_relative_order_enforced():
    spec = _spec(["g1", "x", "g2"], groups={"g1": "g", "g2": "g"})
    target = build_target_order(spec)
    assert target.index("g1") < target.index("g2")
    # 手动破坏同组顺序，验证器必须独立抓到
    bad = ["x", "g2", "g1"]
    assert not verify_order(spec, bad).ok


# --------------------------------------------------------------------------- #
# 循环依赖：必须报错且给出具体冲突链
# --------------------------------------------------------------------------- #
def test_cycle_reported_with_chain(load_spec):
    spec = load_spec("cyclic-deps")
    with pytest.raises(UnsatisfiableError) as ei:
        check_satisfiable(spec)
    chain_text = " ".join(ei.value.chains)
    assert "循环依赖" in chain_text
    for sid in ("X", "Y", "Z"):
        assert sid in chain_text


def test_self_dependency_is_cycle():
    spec = _spec(["a", "b"], deps={"a": ["a"]})
    with pytest.raises(UnsatisfiableError):
        check_satisfiable(spec)


# --------------------------------------------------------------------------- #
# 固定步骤冲突：同槽位抢占 + 依赖方向矛盾
# --------------------------------------------------------------------------- #
def test_fixed_slot_collision(load_spec):
    spec = load_spec("fixed-conflict")
    with pytest.raises(UnsatisfiableError) as ei:
        check_satisfiable(spec)
    text = " ".join(ei.value.chains)
    assert "槽位 2" in text and "B" in text and "C" in text


def test_fixed_order_contradicts_dependency():
    # a 必须先于 b，却把 b 固定在更前的槽位
    spec = _spec(["a", "b", "c"], fixed={"a": 2, "b": 0}, deps={"b": ["a"]})
    with pytest.raises(UnsatisfiableError) as ei:
        check_satisfiable(spec)
    text = " ".join(ei.value.chains)
    assert "固定位置冲突" in text


def test_fixed_too_many_predecessors():
    # x 固定在槽位 0，却有两个前置步骤，槽位前没有位置
    spec = _spec(["p1", "p2", "x"], fixed={"x": 0},
                 deps={"x": ["p1", "p2"]})
    with pytest.raises(UnsatisfiableError):
        check_satisfiable(spec)


def test_fixed_steps_land_on_slots():
    spec = _spec(["a", "b", "c", "d"], fixed={"a": 0, "d": 3},
                 deps={"c": ["b"]})
    target = build_target_order(spec)
    assert target[0] == "a" and target[3] == "d"
    assert verify_order(spec, target).ok


# --------------------------------------------------------------------------- #
# 最少移动：加权 LCS 与计划最优性
# --------------------------------------------------------------------------- #
def test_weighted_lcs_basic():
    cur = ["c", "a", "b", "d"]
    tgt = ["a", "b", "c", "d"]
    keep = weighted_lcs(cur, tgt, {x: 1 for x in cur})
    assert keep == ["a", "b", "d"]


def test_weighted_lcs_prefers_fixed():
    # 固定步骤 b 权重更高，应被优先保留
    cur = ["b", "a", "c"]
    tgt = ["a", "b", "c"]
    w = {"a": 1, "b": 100, "c": 1}
    keep = weighted_lcs(cur, tgt, w)
    assert "b" in keep and "c" in keep


def test_plan_reaches_lower_bound_and_simulation_matches_target():
    spec = _spec(["a", "b", "c", "d", "e"], fixed={"a": 0},
                 deps={"d": ["b"], "e": ["c", "d"]})
    current = ["e", "c", "a", "b", "d"]
    plan = plan_moves(spec, current)
    assert plan.optimal, "计划移动次数应达到 n-LCS 下界"
    # 用计划中的移动逐步模拟，结果必须等于目标
    sim = list(current)
    for mv in plan.moves:
        sim.remove(mv.step_id)
        if mv.anchor_id is None:
            sim.insert(0, mv.step_id)
        else:
            sim.insert(sim.index(mv.anchor_id) + 1, mv.step_id)
    assert sim == plan.target
    assert verify_order(spec, sim).ok


def test_replan_after_user_reorder_uses_fresh_indices(load_spec):
    """用户改变顺序后必须重新规划，旧计划不能继续用。"""
    spec = load_spec("normal-titration")
    order_a = ["S8", "S4", "S1", "S7", "S2", "S6", "S5", "S3"]
    plan_a = plan_moves(spec, order_a)
    # 用户手动换成另一种排列（过期下标场景）
    order_b = ["S3", "S7", "S8", "S1", "S5", "S2", "S4", "S6"]
    plan_b = plan_moves(spec, order_b)
    assert plan_a.moves != plan_b.moves or plan_a.target == plan_b.target
    # 两个计划各自模拟都必须收敛到同一目标
    for order, plan in ((order_a, plan_a), (order_b, plan_b)):
        sim = list(order)
        for mv in plan.moves:
            sim.remove(mv.step_id)
            pos = 0 if mv.anchor_id is None else sim.index(mv.anchor_id) + 1
            sim.insert(pos, mv.step_id)
        assert sim == plan.target


def test_already_sorted_needs_zero_moves():
    spec = _spec(["a", "b", "c"], deps={"c": ["b"], "b": ["a"]})
    plan = plan_moves(spec, ["a", "b", "c"])
    assert plan.moves == [] and plan.lower_bound == 0


# --------------------------------------------------------------------------- #
# 独立真值断言：truth 文件仅供本测试使用
# --------------------------------------------------------------------------- #
def test_normal_sample_matches_independent_truth(load_spec, truth_dir):
    spec = load_spec("normal-titration")
    truth = json.loads((truth_dir / "normal-titration.expected.json").read_text("utf-8"))
    target = build_target_order(spec)
    assert truth["satisfiable"] is True
    assert target == truth["expected_target"]
    for sid, slot in truth["fixed"].items():
        assert target[slot] == sid
    assert verify_order(spec, target).ok


def test_full_acceptance_target_satisfies_all(load_spec):
    spec = load_spec("full-acceptance")
    target = build_target_order(spec)
    v = verify_order(spec, target)
    assert v.ok, v.checks
    assert target[0] == "F01" and target[9] == "F10"
