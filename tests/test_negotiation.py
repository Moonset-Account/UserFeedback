"""专网隧道提案协商的验收测试。

覆盖维度：
- 可接受结果：AEAD 套件、非 AEAD 套件（含完整性选择）；
- 应被阻止/拒绝：AEAD 另配完整性、混用加密类型、未知算法、
  政策淘汰（PolicyRejectionError）、无共同提案（NoCommonProposalError）；
- 边界与交错：强度恰等于门槛、跨提案不可拼接、本端/对端提案顺序、
  组内偏好、先淘汰后成功的交错协商、换钥门槛叠加。
"""

import os
import sys
import unittest

# 允许直接以脚本方式运行（python tests/test_negotiation.py）：
# 把工作区根目录加入导入路径，再导入被测包。
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

from tunnel_negotiation import (
    NoCommonProposalError,
    PolicyRejectionError,
    Proposal,
    UnknownAlgorithmError,
    default_policy,
    negotiate,
    negotiate_rekey,
    validate_proposal,
)


def proposal(pid, enc, integrity, groups):
    return Proposal(
        proposal_id=pid,
        encryption_algorithms=list(enc),
        integrity_algorithms=list(integrity),
        key_exchange_groups=list(groups),
    )


class AcceptableResultTests(unittest.TestCase):
    def test_aead_happy_path_uses_local_ordering(self):
        # 对端把偏好顺序反过来，结果仍按本端顺序选定。
        local = [proposal(1, ["AES128-GCM", "AES256-GCM"], [], ["DH19", "DH20"])]
        remote = [proposal(7, ["AES256-GCM", "AES128-GCM"], [], ["DH20", "DH19"])]

        result = negotiate(local, remote, default_policy())

        self.assertEqual(result.encryption, "AES128-GCM")
        self.assertIsNone(result.integrity)  # AEAD 不携带独立完整性算法
        self.assertEqual(result.group, "DH19")
        self.assertEqual(result.strength, 128)
        self.assertEqual(result.local_proposal_id, 1)
        self.assertEqual(result.remote_proposal_id, 7)

    def test_non_aead_requires_and_selects_integrity(self):
        local = [
            proposal(
                1,
                ["AES256-CBC"],
                ["HMAC-SHA256-128", "HMAC-SHA384-192"],
                ["DH14"],
            )
        ]
        remote = [
            proposal(
                2,
                ["AES256-CBC"],
                ["HMAC-SHA384-192", "HMAC-SHA256-128"],
                ["DH14"],
            )
        ]

        result = negotiate(local, remote, default_policy())

        self.assertEqual(result.encryption, "AES256-CBC")
        # 完整性按本端偏好选 SHA256，尽管对端把 SHA384 排在前面。
        self.assertEqual(result.integrity, "HMAC-SHA256-128")
        self.assertEqual(result.group, "DH14")
        # 套件强度由最弱组件决定：min(256, 128, 112)。
        self.assertEqual(result.strength, 112)

    def test_aead_and_non_aead_coexist_in_separate_proposals(self):
        local = [
            proposal(1, ["3DES-CBC"], ["HMAC-SHA1-96"], ["DH14"]),
            proposal(2, ["AES256-GCM"], [], ["DH21"]),
        ]
        remote = [
            proposal(9, ["AES256-GCM"], [], ["DH21"]),
        ]

        result = negotiate(local, remote, default_policy())

        self.assertEqual(result.encryption, "AES256-GCM")
        self.assertIsNone(result.integrity)
        self.assertEqual(result.local_proposal_id, 2)
        self.assertEqual(result.remote_proposal_id, 9)


class BlockedResultTests(unittest.TestCase):
    def test_aead_with_integrity_algorithm_is_rejected(self):
        bad = proposal(1, ["AES128-GCM"], ["HMAC-SHA256-128"], ["DH19"])
        with self.assertRaises(ValueError):
            validate_proposal(bad, default_policy())
        with self.assertRaises(ValueError):
            negotiate([bad], [proposal(2, ["AES128-GCM"], [], ["DH19"])],
                      default_policy())

    def test_mixed_aead_and_non_aead_in_one_proposal_is_rejected(self):
        bad = proposal(
            1,
            ["AES128-GCM", "AES128-CBC"],
            ["HMAC-SHA256-128"],
            ["DH19"],
        )
        with self.assertRaises(ValueError):
            validate_proposal(bad, default_policy())

    def test_non_aead_without_integrity_is_rejected(self):
        bad = proposal(1, ["AES128-CBC"], [], ["DH19"])
        with self.assertRaises(ValueError):
            validate_proposal(bad, default_policy())

    def test_unknown_algorithm_raises_named_error(self):
        local = [proposal(1, ["BOGUS-CBC"], ["HMAC-SHA256-128"], ["DH19"])]
        remote = [proposal(2, ["BOGUS-CBC"], ["HMAC-SHA256-128"], ["DH19"])]
        with self.assertRaises(UnknownAlgorithmError):
            negotiate(local, remote, default_policy())

    def test_policy_rejection_distinct_from_no_common_proposal(self):
        # 存在完整共同套件，但强度 80 低于门槛 128：政策淘汰。
        local = [proposal(1, ["AES128-GCM"], [], ["DH2"])]
        remote = [proposal(2, ["AES128-GCM"], [], ["DH2"])]

        with self.assertRaises(PolicyRejectionError) as ctx:
            negotiate(local, remote, default_policy(min_strength=128))

        self.assertEqual(ctx.exception.min_strength, 128)
        self.assertEqual(len(ctx.exception.rejected), 1)
        rejected = ctx.exception.rejected[0]
        self.assertEqual(rejected.strength, 80)
        self.assertEqual(rejected.group, "DH2")
        # 两类失败必须可区分。
        self.assertNotIsInstance(ctx.exception, NoCommonProposalError)

    def test_disjoint_proposals_are_no_common_proposal(self):
        local = [proposal(1, ["AES128-GCM"], [], ["DH19"])]
        remote = [proposal(2, ["AES256-GCM"], [], ["DH19"])]

        with self.assertRaises(NoCommonProposalError) as ctx:
            negotiate(local, remote, default_policy())

        self.assertEqual(len(ctx.exception.diagnostics), 1)
        diag = ctx.exception.diagnostics[0]
        self.assertEqual(diag.common_encryption, ())
        self.assertEqual(diag.common_groups, ("DH19",))
        self.assertNotIsInstance(ctx.exception, PolicyRejectionError)


class BoundaryAndInterleavingTests(unittest.TestCase):
    def test_strength_equal_to_threshold_is_accepted(self):
        local = [proposal(1, ["AES128-GCM"], [], ["DH19"])]  # 强度恰好 128
        remote = [proposal(2, ["AES128-GCM"], [], ["DH19"])]

        result = negotiate(local, remote, default_policy(min_strength=128))
        self.assertEqual(result.strength, 128)

    def test_strength_one_step_below_threshold_is_rejected(self):
        # DH14=112，门槛 128：共同套件存在但全部被淘汰。
        local = [proposal(1, ["AES128-GCM"], [], ["DH14"])]
        remote = [proposal(2, ["AES128-GCM"], [], ["DH14"])]

        with self.assertRaises(PolicyRejectionError) as ctx:
            negotiate(local, remote, default_policy(min_strength=128))
        self.assertEqual(ctx.exception.rejected[0].strength, 112)

    def test_algorithms_cannot_be_stitched_across_proposals(self):
        # 本端 P1 的加密只能配 DH19；对端把 DH20 放在另一份提案里，
        # 即使双方整体算法集合相同也不得跨提案拼套件。
        local = [
            proposal(1, ["AES128-GCM"], [], ["DH19"]),
            proposal(2, ["AES256-GCM"], [], ["DH20"]),
        ]
        remote = [
            proposal(9, ["AES128-GCM"], [], ["DH20"]),
            proposal(10, ["AES256-GCM"], [], ["DH19"]),
        ]

        with self.assertRaises(NoCommonProposalError) as ctx:
            negotiate(local, remote, default_policy())

        # 四对提案逐一诊断，且没有任何一对产生完整共同套件。
        self.assertEqual(len(ctx.exception.diagnostics), 4)
        pair_keys = {
            (d.local_proposal_id, d.remote_proposal_id)
            for d in ctx.exception.diagnostics
        }
        self.assertEqual(
            pair_keys, {(1, 9), (1, 10), (2, 9), (2, 10)}
        )

    def test_local_proposal_order_outranks_suite_strength(self):
        local = [
            proposal(1, ["3DES-CBC"], ["HMAC-SHA1-96"], ["DH14"]),  # 弱但在前
            proposal(2, ["AES256-GCM"], [], ["DH21"]),
        ]
        remote = [
            proposal(5, ["3DES-CBC"], ["HMAC-SHA1-96"], ["DH14"]),
            proposal(6, ["AES256-GCM"], [], ["DH21"]),
        ]

        result = negotiate(local, remote, default_policy())

        self.assertEqual(result.encryption, "3DES-CBC")
        self.assertEqual(result.local_proposal_id, 1)
        self.assertEqual(result.remote_proposal_id, 5)

    def test_remote_proposal_order_breaks_ties_within_one_local_proposal(self):
        local = [
            proposal(
                1,
                ["AES128-CBC", "AES256-CBC"],
                ["HMAC-SHA256-128"],
                ["DH19"],
            )
        ]
        remote = [
            proposal(5, ["AES128-CBC"], ["HMAC-SHA256-128"], ["DH19"]),
            proposal(6, ["AES256-CBC"], ["HMAC-SHA256-128"], ["DH19"]),
        ]

        result = negotiate(local, remote, default_policy())

        # 本端同一份提案可同时匹配对端两份提案，取对端顺序靠前者。
        self.assertEqual(result.encryption, "AES128-CBC")
        self.assertEqual(result.remote_proposal_id, 5)

    def test_intra_group_preference_follows_local_lists(self):
        local = [
            proposal(2, ["AES256-GCM", "AES128-GCM"], [], ["DH21", "DH19"])
        ]
        remote = [
            proposal(8, ["AES128-GCM", "AES256-GCM"], [], ["DH19", "DH21"])
        ]

        result = negotiate(local, remote, default_policy())

        self.assertEqual(result.encryption, "AES256-GCM")
        self.assertEqual(result.group, "DH21")
        self.assertEqual(result.strength, 256)

    def test_interleaved_rejection_then_success_continues_search(self):
        local = [
            proposal(1, ["AES128-GCM"], [], ["DH2"]),   # 强度 80，被淘汰
            proposal(2, ["AES128-GCM"], [], ["DH19"]),  # 强度 128，达标
        ]
        remote = [proposal(9, ["AES128-GCM"], [], ["DH2", "DH19"])]

        result = negotiate(local, remote, default_policy(min_strength=128))

        self.assertEqual(result.group, "DH19")
        self.assertEqual(result.local_proposal_id, 2)
        self.assertEqual(result.strength, 128)

    def test_all_pairs_rejected_collects_every_candidate(self):
        local = [
            proposal(1, ["AES128-GCM"], [], ["DH2"]),
            proposal(2, ["AES256-GCM"], [], ["DH2"]),
        ]
        remote = [
            proposal(9, ["AES128-GCM", "AES256-GCM"], [], ["DH2"])
        ]

        with self.assertRaises(PolicyRejectionError) as ctx:
            negotiate(local, remote, default_policy(min_strength=128))

        # 两对提案各产生一个被淘汰候选，全部留档。
        self.assertEqual(len(ctx.exception.rejected), 2)
        self.assertEqual(
            {r.local_proposal_id for r in ctx.exception.rejected}, {1, 2}
        )


class RekeyTests(unittest.TestCase):
    def test_rekey_floor_raises_bar_above_base_policy(self):
        local = [proposal(1, ["AES256-GCM"], [], ["DH19", "DH20"])]
        remote = [proposal(2, ["AES256-GCM"], [], ["DH19", "DH20"])]
        policy = default_policy(min_strength=0)

        # 普通协商：本端偏好 DH19（128）即可。
        base = negotiate(local, remote, policy)
        self.assertEqual(base.group, "DH19")

        # 换钥：上一代连接最低等级 192，DH19 被跳过，选 DH20。
        rekeyed = negotiate_rekey(local, remote, policy, prior_suite_strength=192)
        self.assertEqual(rekeyed.group, "DH20")
        self.assertEqual(rekeyed.strength, 192)

    def test_rekey_with_unreachable_floor_is_policy_rejection(self):
        local = [proposal(1, ["AES256-GCM"], [], ["DH20"])]  # 最高 192
        remote = [proposal(2, ["AES256-GCM"], [], ["DH20"])]

        with self.assertRaises(PolicyRejectionError) as ctx:
            negotiate_rekey(
                local, remote, default_policy(min_strength=0),
                prior_suite_strength=256,
            )
        self.assertEqual(ctx.exception.min_strength, 256)

    def test_rekey_floor_keeps_base_policy_when_it_is_higher(self):
        local = [proposal(1, ["AES256-GCM"], [], ["DH19", "DH20"])]
        remote = [proposal(2, ["AES256-GCM"], [], ["DH19", "DH20"])]

        # 政策门槛 192 高于上一代等级 128，以政策门槛为准。
        result = negotiate_rekey(
            local, remote, default_policy(min_strength=192),
            prior_suite_strength=128,
        )
        self.assertEqual(result.group, "DH20")


class ConstructionValidationTests(unittest.TestCase):
    def test_duplicate_algorithm_in_proposal_is_rejected(self):
        with self.assertRaises(ValueError):
            proposal(1, ["AES128-GCM", "AES128-GCM"], [], ["DH19"])

    def test_empty_encryption_or_groups_is_rejected(self):
        with self.assertRaises(ValueError):
            proposal(1, [], [], ["DH19"])
        with self.assertRaises(ValueError):
            proposal(1, ["AES128-GCM"], [], [])

    def test_duplicate_proposal_ids_are_rejected(self):
        local = [
            proposal(1, ["AES128-GCM"], [], ["DH19"]),
            proposal(1, ["AES256-GCM"], [], ["DH21"]),
        ]
        remote = [proposal(2, ["AES128-GCM"], [], ["DH19"])]
        with self.assertRaises(ValueError):
            negotiate(local, remote, default_policy())


if __name__ == "__main__":
    unittest.main(verbosity=2)
