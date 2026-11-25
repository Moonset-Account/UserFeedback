"""测试辅助：独立复核、暴力枚举对照与合成数据生成（仅测试用）。"""

from __future__ import annotations

import random

from mail_gateway import (
    DeliveryPlan,
    Key,
    Recipient,
    evaluate_package,
)


# ---------------------------------------------------------------------------
# 独立复核：不调用 planner 的结论，直接从输入重新核对每个包
# ---------------------------------------------------------------------------

def assert_plan_invariants(
    testcase,
    plan: DeliveryPlan,
    recipients: dict[str, Recipient],
    keys: list[Key],
) -> None:
    all_ids = set(recipients)
    seen: list[str] = []
    for pkg in plan.packages:
        seen.extend(sorted(pkg.members))
        # 每名收件人恰好进入一个包
        testcase.assertTrue(pkg.members <= all_ids)
        testcase.assertTrue(pkg.members)

        # 与 evaluate_package 的结论一致
        option = evaluate_package(pkg.members, recipients, keys)
        testcase.assertTrue(
            option.feasible, msg=f"package {set(pkg.members)} should be feasible"
        )
        testcase.assertEqual(option.suite, pkg.suite)
        testcase.assertEqual(option.envelope_key_ids, pkg.envelope_key_ids)

        key_by_id = {k.key_id: k for k in keys}
        envelope_keys = [key_by_id[kid] for kid in pkg.envelope_key_ids]

        # 套件一致：一个包只能用一种大家都支持的套件
        for key in envelope_keys:
            testcase.assertEqual(key.suite, pkg.suite)
        for rid in pkg.members:
            testcase.assertIn(pkg.suite, recipients[rid].suites)

        # 每名成员都能解开至少一个封套
        for rid in pkg.members:
            testcase.assertTrue(
                any(rid in key.holders for key in envelope_keys),
                msg=f"{rid} cannot open any envelope in {set(pkg.members)}",
            )

        # 独立复核可见性约束
        for key in envelope_keys:
            for viewer in key.resolvers & pkg.members:
                owner = recipients.get(key.identity)
                allowed = owner.can_see if owner is not None else frozenset()
                testcase.assertIn(viewer, allowed)

        # 独立复核解密范围（含组外持钥人）
        for key in envelope_keys:
            for rid in pkg.members:
                testcase.assertTrue(
                    key.holders <= recipients[rid].decrypt_scope,
                    msg=(
                        f"holder {sorted(key.holders)} outside scope of {rid} "
                        f"for key {key.key_id}"
                    ),
                )

    testcase.assertEqual(sorted(seen), sorted(all_ids))


# ---------------------------------------------------------------------------
# 暴力枚举：穷举全部集合划分，独立求最优，与 DP 结果对照
# ---------------------------------------------------------------------------

def _all_partitions(ids: list[str]) -> list[list[frozenset[str]]]:
    if not ids:
        return [[]]
    first, rest = ids[0], ids[1:]
    result: list[list[frozenset[str]]] = []
    for smaller in _all_partitions(rest):
        # 放进已有某个包
        for i in range(len(smaller)):
            grown = smaller.copy()
            grown[i] = smaller[i] | frozenset([first])
            result.append(grown)
        # 单开一个包
        result.append(smaller + [frozenset([first])])
    return result


def brute_force_optimum(
    recipients: dict[str, Recipient], keys: list[Key]
) -> tuple[int, int] | None:
    """返回 (最少包数, 该包数下最少封套数)；无解返回 None。"""
    ids = sorted(recipients)
    best: tuple[int, int] | None = None
    for partition in _all_partitions(ids):
        total_envelopes = 0
        feasible = True
        for block in partition:
            option = evaluate_package(block, recipients, keys)
            if not option.feasible:
                feasible = False
                break
            total_envelopes += len(option.envelope_key_ids)
        if feasible:
            cost = (len(partition), total_envelopes)
            if best is None or cost < best:
                best = cost
    return best


# ---------------------------------------------------------------------------
# 合成随机实例：刻意制造交错的可见性/范围/套件约束
# ---------------------------------------------------------------------------

def random_instance(
    n: int, rng: random.Random
) -> tuple[dict[str, Recipient], list[Key]]:
    """生成带交错约束的随机实例。

    每名收件人必有一把只由自己持有、无人可反查的个人钥，且其
    ``decrypt_scope`` 至少包含自己——因此“全部单包”始终可行；
    更紧的 scope 与组钥仍会让很多合包不可行，从而产生拆投压力。
    """
    ids = [f"r{i:02d}" for i in range(n)]
    outsiders = [f"x{i}" for i in range(rng.randint(0, 2))]
    everyone = ids + outsiders
    suites_pool = ["S1", "S2", "S3"]

    recipients: dict[str, Recipient] = {}
    for rid in ids:
        scope = set(rng.sample(everyone, rng.randint(1, len(everyone))))
        scope.add(rid)  # 自己永远在自身授权范围内
        recipients[rid] = Recipient(
            recipient_id=rid,
            suites=frozenset(
                rng.sample(suites_pool, rng.randint(1, len(suites_pool)))
            ),
            can_see=frozenset(rng.sample(ids, rng.randint(0, n))),
            decrypt_scope=frozenset(scope),
        )

    keys: list[Key] = []
    for i, rid in enumerate(ids):
        # 保底个人钥：只由本人持有、匿名不可反查，套件取本人支持的第一个
        personal_suite = sorted(recipients[rid].suites)[0]
        keys.append(
            Key(
                key_id=f"k{i:02d}-self",
                suite=personal_suite,
                holders=frozenset([rid]),
                identity=f"anon-{rid}",
                resolvers=frozenset(),
            )
        )
        # 额外的共享/组密钥，刻意制造交错约束
        for _ in range(rng.randint(1, 2)):
            holders = frozenset(
                set(
                    rng.sample(
                        everyone, rng.randint(1, max(1, len(everyone) // 2 + 1))
                    )
                )
                | {rid}
            )
            identity = rid if rng.random() < 0.8 else f"anon-{rid}"
            resolvers = frozenset(rng.sample(ids, rng.randint(0, n)))
            keys.append(
                Key(
                    key_id=f"k{i:02d}-{rng.randrange(10_000):04d}",
                    suite=rng.choice(sorted(recipients[rid].suites)),
                    holders=holders,
                    identity=identity,
                    resolvers=resolvers,
                )
            )
    return recipients, keys
