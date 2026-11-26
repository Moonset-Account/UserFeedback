"""随机化差分测试：自动生成请求组合，对照改写前后两条执行路径。

覆盖正常共享场景的随机交错（不同 WHERE/HAVING/分区子集/空组策略/聚合集合），
以及保证会被阻塞的非法组合（断言计划器拒绝而非静默产出错误结果）。
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sharedscan.model import (  # noqa: E402
    Agg,
    AggFn,
    And,
    Cmp,
    EmptyGroupPolicy,
    EmptyInputPolicy,
    IsNotNull,
    IsNull,
    Or,
    Request,
    Semantics,
)
from sharedscan.planner import BlockingReasons, build_shared_plan  # noqa: E402
from sharedscan.verify import verify_plan  # noqa: E402
from synth_data import build_catalog  # noqa: E402

ALL_PARTS = ("p1", "p2", "p3", "p4")
REGIONS = ("east", "west", "north", None)

WHERE_CHOICES = [
    None,
    Cmp("amount", ">", 0),
    Cmp("amount", ">", 10),
    Cmp("amount", "=", 10),
    IsNull("amount"),
    IsNotNull("cust_id"),
    And(Cmp("amount", ">", 0), Cmp("qty", "<", 5.0)),
    Or(Cmp("region", "=", "east"), Cmp("region", "=", "north")),
    Cmp("flag", "=", True),
]

AGG_COLUMNS = {
    AggFn.COUNT: "amount",
    AggFn.SUM: "amount",
    AggFn.AVG: "qty",
    AggFn.MIN: "amount",
    AggFn.MAX: "amount",
}


def _random_partition_subset(rng):
    n = rng.randint(1, len(ALL_PARTS))
    return tuple(rng.sample(ALL_PARTS, n))


def _random_aggs(rng):
    choices = [
        Agg("n", AggFn.COUNT_STAR, None),
        Agg("c", AggFn.COUNT, "amount"),
        Agg("s", AggFn.SUM, "amount"),
        Agg("a", AggFn.AVG, "qty"),
        Agg("mn", AggFn.MIN, "amount"),
        Agg("mx", AggFn.MAX, "amount"),
    ]
    k = rng.randint(1, len(choices))
    picked = rng.sample(choices, k)
    # 别名唯一（sample 已保证不重复）
    return tuple(picked)


def _random_grouped_request(rng, index):
    aggs = _random_aggs(rng)
    semantics = Semantics(
        empty_group=rng.choice([EmptyGroupPolicy.DROP, EmptyGroupPolicy.KEEP_DOMAIN])
    )
    domain = tuple((r,) for r in REGIONS) if (
        semantics.empty_group is EmptyGroupPolicy.KEEP_DOMAIN
    ) else ()
    # HAVING 只能引用真实存在的聚合别名 n
    has_n = any(a.alias == "n" for a in aggs)
    having = Cmp("n", ">", 0) if has_n else None
    return Request(
        name=f"r{index}",
        table="sales",
        partitions=_random_partition_subset(rng),
        group_keys=("region",),
        aggs=aggs,
        where=rng.choice(WHERE_CHOICES),
        having=having,
        semantics=semantics,
        key_domain=domain,
    )


def _random_grand_total_request(rng, index):
    return Request(
        name=f"g{index}",
        table="sales",
        partitions=_random_partition_subset(rng),
        group_keys=(),
        aggs=_random_aggs(rng),
        where=rng.choice(WHERE_CHOICES),
        semantics=Semantics(
            empty_input=rng.choice(
                [EmptyInputPolicy.ONE_NULL_ROW, EmptyInputPolicy.ZERO_ROWS]
            )
        ),
    )


class TestDifferentialFuzz(unittest.TestCase):
    def test_many_random_grouped_batches_are_equivalent(self):
        rng = random.Random(20261127)
        catalog = build_catalog(exclusive_cust=False)
        for trial in range(60):
            requests = [_random_grouped_request(rng, i) for i in range(rng.randint(1, 5))]
            plan = build_shared_plan(requests, catalog)
            report = verify_plan(requests, plan, catalog)
            self.assertTrue(
                report.ok,
                msg=f"试验 {trial} 失败: "
                + "; ".join(f"{m.kind}:{m.detail}" for m in report.mismatches),
            )

    def test_many_random_grand_total_batches_are_equivalent(self):
        rng = random.Random(424242)
        catalog = build_catalog(exclusive_cust=False)
        for trial in range(40):
            requests = [_random_grand_total_request(rng, i)
                        for i in range(rng.randint(1, 4))]
            plan = build_shared_plan(requests, catalog)
            report = verify_plan(requests, plan, catalog)
            self.assertTrue(
                report.ok,
                msg=f"grand-total 试验 {trial} 失败: "
                + "; ".join(f"{m.kind}:{m.detail}" for m in report.mismatches),
            )

    def test_random_distinct_requests_single_partition_equivalent(self):
        rng = random.Random(99)
        catalog = build_catalog(exclusive_cust=False)
        for trial in range(30):
            requests = []
            for i in range(rng.randint(1, 3)):
                requests.append(Request(
                    name=f"d{i}",
                    table="sales",
                    partitions=(rng.choice(ALL_PARTS),),  # 始终单分区
                    group_keys=("region",),
                    aggs=(Agg("d", AggFn.COUNT_DISTINCT, "cust_id",
                              include_null=rng.choice([True, False])),),
                    where=rng.choice(WHERE_CHOICES),
                ))
            plan = build_shared_plan(requests, catalog)
            report = verify_plan(requests, plan, catalog)
            self.assertTrue(report.ok, msg=str(report.mismatches))

    def test_random_exclusive_catalog_distinct_batches_equivalent(self):
        rng = random.Random(7)
        catalog = build_catalog(exclusive_cust=True)
        # 互斥标示是正确性前提。当前合成数据只有 p2 的 cust_id（仅 c3）与其它
        # 分区天然不相交；多分区互斥合并（DISTINCT_PARTIAL 计数相加）的正确性
        # 在端到端测试 test_distinct_count_exclusive_multi_partition_equivalence
        # （p1+p2，含手工期望值）中断言，这里的随机批次在单分区子集上采样，
        # 覆盖互斥目录下单分区去重与其它聚合混合的等价性。
        disjoint_subsets = [("p2",), ("p1",), ("p3",)]
        for trial in range(25):
            requests = [
                Request(
                    name=f"x{i}",
                    table="sales",
                    partitions=rng.choice(disjoint_subsets),
                    group_keys=("region",),
                    aggs=(Agg("d", AggFn.COUNT_DISTINCT, "cust_id"),
                          Agg("n", AggFn.COUNT_STAR, None)),
                    where=rng.choice(WHERE_CHOICES),
                )
                for i in range(rng.randint(1, 3))
            ]
            plan = build_shared_plan(requests, catalog)
            report = verify_plan(requests, plan, catalog)
            self.assertTrue(report.ok, msg=str(report.mismatches))

    def test_random_invalid_requests_are_always_rejected(self):
        rng = random.Random(31337)
        catalog = build_catalog(exclusive_cust=False)
        rejected = 0
        for _ in range(40):
            # 跨分区去重但目录未标示互斥：计划器必须拒绝
            req = Request(
                name="bad",
                table="sales",
                partitions=("p1", "p2"),
                group_keys=("region",),
                aggs=(Agg("d", AggFn.COUNT_DISTINCT, "cust_id"),),
            )
            with self.assertRaises(BlockingReasons):
                build_shared_plan([req], catalog)
            rejected += 1
        self.assertEqual(rejected, 40)


if __name__ == "__main__":
    unittest.main()
