"""独立验证器：只依据 spec 约束与页面回读顺序做独立复核。

不读取 planner 的 Plan，也不读取样例生成器的 expected 真值文件；
真值文件仅供 pytest 断言使用。验证维度：
1. 顺序是步骤 id 的一个合法排列（无缺失/重复/外来项）；
2. 每条依赖边（含同组展开边）方向正确；
3. 每个固定锚点落在指定槽位。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Spec


@dataclass
class Verification:
    ok: bool
    checks: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "checks": self.checks}


def verify_order(spec: Spec, order: list[str]) -> Verification:
    checks: list[dict] = []
    ok = True

    # 1) 排列合法性
    expect_set = set(spec.step_ids)
    actual_set = set(order)
    missing = sorted(expect_set - actual_set)
    extra = sorted(actual_set - expect_set)
    dupes = sorted({x for x in order if order.count(x) > 1})
    perm_ok = not missing and not extra and not dupes and len(order) == len(spec.steps)
    ok &= perm_ok
    detail = []
    if missing:
        detail.append(f"缺失步骤：{'、'.join(missing)}")
    if extra:
        detail.append(f"外来步骤：{'、'.join(extra)}")
    if dupes:
        detail.append(f"重复步骤：{'、'.join(dupes)}")
    checks.append({
        "name": "排列完整性",
        "ok": perm_ok,
        "detail": "回读顺序包含且仅包含全部步骤各一次。"
        if perm_ok else "；".join(detail),
    })

    index = {sid: i for i, sid in enumerate(order)}

    # 2) 依赖方向（含同组约束展开后的边）
    dep_violations: list[str] = []
    for vid, prereqs in spec.depends_on.items():
        for uid in prereqs:
            if uid in index and vid in index and not index[uid] < index[vid]:
                dep_violations.append(
                    f"{uid}（槽位{index[uid]}）必须先于 {vid}（槽位{index[vid]}）"
                )
    dep_ok = not dep_violations
    ok &= dep_ok
    total_edges = sum(len(v) for v in spec.depends_on.values())
    checks.append({
        "name": "依赖与同组相对顺序",
        "ok": dep_ok,
        "detail": f"全部 {total_edges} 条先后约束方向正确。"
        if dep_ok else "违反约束：" + "；".join(dep_violations),
    })

    # 3) 固定锚点
    fixed_violations: list[str] = []
    for sid, pos in spec.fixed.items():
        actual = index.get(sid)
        if actual != pos:
            fixed_violations.append(f"{sid} 应在槽位 {pos}，实际 {actual}")
    fixed_ok = not fixed_violations
    ok &= fixed_ok
    checks.append({
        "name": "固定锚点位置",
        "ok": fixed_ok,
        "detail": f"全部 {len(spec.fixed)} 个固定步骤位于指定槽位。"
        if fixed_ok else "；".join(fixed_violations),
    })

    return Verification(ok=ok, checks=checks)
