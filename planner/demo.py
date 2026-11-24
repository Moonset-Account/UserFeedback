"""命令行演示：在小地址投影上构造一个多租户迁移问题并输出真实排程。

用法（工作区根目录）：

    python3 -I -m planner.demo

该脚本实时调用 :func:`planner.scheduler.plan` 并经两套验证器校验，
不使用任何预置/硬编码输出。
"""

from __future__ import annotations

from .model import Problem
from .prefix import Prefix
from .render import render_text
from .scheduler import plan
from .verifier import verify_schedule, verify_small_projection

WIDTH = 8


def _sl(addr: int, bits: int) -> Prefix:
    return Prefix.projected(addr, WIDTH, WIDTH - bits)


def build_demo_problem() -> Problem:
    from .model import Tenant, TempPrefix, Upstream

    u1 = Upstream("U1", _sl(0, 6), max_advertisements=8)
    u2 = Upstream("U2", _sl(0, 6), max_advertisements=8)
    t1 = Tenant(
        name="t1",
        prefix_length=WIDTH - 2,
        fixed=(_sl(6, 1),),
        allowed_upstreams=("U2",),
        old_prefix=_sl(0, 3),
        old_upstream="U1",
        hold_rounds=2,
        policy="alpha",
    )
    t2 = Tenant(
        name="t2",
        prefix_length=WIDTH - 2,
        fixed=(_sl(22, 1),),
        allowed_upstreams=("U2",),
        old_prefix=_sl(16, 3),
        old_upstream="U1",
        hold_rounds=1,
        policy="beta",
    )
    tmp = TempPrefix("TMP1", _sl(4, 2), "U1")
    return Problem(upstreams=(u1, u2), tenants=(t1, t2), temp_prefixes=(tmp,))


def main() -> None:
    problem = build_demo_problem()
    schedule = plan(problem)
    symbolic = verify_schedule(problem, schedule)
    projected = verify_small_projection(problem, schedule, WIDTH)
    print(render_text(problem, schedule))
    print(f"符号验证: {'通过' if symbolic.ok else '失败 ' + str(symbolic.errors)}")
    print(f"逐地址投影验证: {'通过' if projected.ok else '失败 ' + str(projected.errors)}")
    if not (symbolic.ok and projected.ok):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
