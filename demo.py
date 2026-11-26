"""端到端演示：构造一组报表请求，打印共享计划、阻塞定位与改写前后对照。

运行：python3 demo.py
仅使用 tests/synth_data.py 中的本地合成数据，无网络、无第三方依赖。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests"))

from sharedscan.model import (  # noqa: E402
    Agg,
    AggFn,
    Cmp,
    EmptyGroupPolicy,
    Request,
    Semantics,
)
from sharedscan.planner import BlockingReasons, build_shared_plan  # noqa: E402
from sharedscan.interpreter import execute_request, execute_shared_plan  # noqa: E402
from sharedscan.verify import verify_plan  # noqa: E402
from synth_data import build_catalog  # noqa: E402


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(result) -> None:
    print(f"  列: {result.columns}")
    for row in result.rows:
        print(f"    {row}")
    print(f"  类型族: { {c: sorted(v, key=lambda x: '~' if x is None else x) for c, v in result.column_families.items()} }")


def main() -> None:
    catalog = build_catalog(exclusive_cust=False)

    # ---- 场景一：可共享的三个请求 ---------------------------------------- #
    section("场景一：三个请求合成一次扫描（AVG 重用 sum/count，条件聚合）")
    r1 = Request(
        name="每区平均金额",
        table="sales",
        partitions=("p1", "p2", "p3"),
        group_keys=("region",),
        aggs=(Agg("avg_amount", AggFn.AVG, "amount"),
              Agg("n", AggFn.COUNT_STAR, None)),
    )
    r2 = Request(
        name="每区金额合计与计数",
        table="sales",
        partitions=("p1", "p2", "p3"),
        group_keys=("region",),
        aggs=(Agg("sum_amount", AggFn.SUM, "amount"),
              Agg("cnt_amount", AggFn.COUNT, "amount")),
    )
    r3 = Request(
        name="高金额行计数",
        table="sales",
        partitions=("p1", "p2", "p3"),
        group_keys=("region",),
        aggs=(Agg("n", AggFn.COUNT_STAR, None),),
        where=Cmp("amount", ">", 10),
    )
    plan = build_shared_plan([r1, r2, r3], catalog)
    print(plan.describe())

    section("场景一：改写前后逐行逐类型对照")
    report = verify_plan([r1, r2, r3], plan, catalog)
    print(f"  对照请求数: {report.compared}，结果: {'全部一致' if report.ok else '存在差异'}")

    section("场景一：共享计划投影出的具体行（以请求 r1 为例）")
    shared = execute_shared_plan(plan, catalog)
    show(shared["每区平均金额"])
    print("  -- 参考执行（改写前）同请求的行：")
    show(execute_request(r1, catalog))

    # ---- 场景二：空组/缺失值语义差异由投影承载 --------------------------- #
    section("场景二：DROP 与 KEEP_DOMAIN 两种空组策略共存于一个共享计划")
    d1 = Request(
        name="标准分组",
        table="sales", partitions=("p1",), group_keys=("region",),
        aggs=(Agg("n", AggFn.COUNT_STAR, None),),
        semantics=Semantics(empty_group=EmptyGroupPolicy.DROP),
    )
    d2 = Request(
        name="按域补齐",
        table="sales", partitions=("p1",), group_keys=("region",),
        aggs=(Agg("n", AggFn.COUNT_STAR, None),),
        semantics=Semantics(empty_group=EmptyGroupPolicy.KEEP_DOMAIN),
        key_domain=(("east",), ("west",), ("north",), (None,)),
    )
    plan2 = build_shared_plan([d1, d2], catalog)
    verify_plan([d1, d2], plan2, catalog).raise_if_failed()
    shared2 = execute_shared_plan(plan2, catalog)
    print("  标准分组:")
    show(shared2["标准分组"])
    print("  按域补齐（north 缺席，补 0；NULL 是真实组，不补）:")
    show(shared2["按域补齐"])

    # ---- 场景三：非互斥去重计数 -> 定位无法共享的条件 -------------------- #
    section("场景三：跨分区去重计数且未标示互斥 -> 阻塞并定位")
    bad = Request(
        name="跨区去重客户",
        table="sales", partitions=("p1", "p3"), group_keys=("region",),
        aggs=(Agg("distinct_cust", AggFn.COUNT_DISTINCT, "cust_id"),),
    )
    try:
        build_shared_plan([bad], catalog)
    except BlockingReasons as exc:
        print(str(exc))
        print(f"  原因代码: {[r.code for r in exc.reasons]}")
    print(
        "  说明: p1 与 p3 都出现 cust_id='c1'，部分计数相加会把 c1 计两次；\n"
        "        计划器在没有互斥标示时拒绝合并，而不是静默产出错误结果。"
    )

    section("场景三对照：互斥前提成立时（p1+p2 的 cust_id 不相交）计划可构建")
    exclusive_catalog = build_catalog(exclusive_cust=True)
    safe = Request(
        name="互斥区去重客户",
        table="sales", partitions=("p1", "p2"), group_keys=("region",),
        aggs=(Agg("distinct_cust", AggFn.COUNT_DISTINCT, "cust_id"),),
    )
    ok_plan = build_shared_plan([safe], exclusive_catalog)
    print(f"  状态种类: {sorted({s.kind for s in ok_plan.state_specs.values()})}")
    verify_plan([safe], ok_plan, exclusive_catalog).raise_if_failed()
    print("  改写前后对照: 一致（部分计数相加 == 集合并集基数，因为前提成立）")

    section("反面实证：在违反互斥前提的数据上标示互斥，对照机制如何抓出错误")
    unsafe_plan = build_shared_plan([bad], exclusive_catalog)  # 计划器只校验“有无标示”
    bad_report = verify_plan([bad], unsafe_plan, exclusive_catalog)
    print(f"  对照结果 ok={bad_report.ok}（预期 False）")
    for mismatch in bad_report.mismatches:
        print(f"  [{mismatch.kind}] {mismatch.detail}")
    print("  -> 解释器逐行对照把“标示与数据不符”的错误显式定位出来。")

    print("\n演示完成。")


if __name__ == "__main__":
    main()
