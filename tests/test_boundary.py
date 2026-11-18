"""边界与交错用例：同时影响可接受与被拒路径的临界条件。

覆盖：
- 布尔证明缺失时在同一请求上回退明细路径（证明路径边界）；
- 跨用途衍生字段恰好缺一个用途时的授权边界（必需 vs 可选交错）；
- 可选失败后必需成功、必需失败立即短路等交错顺序；
- 重复请求、空请求、仅部分属性等边界输入。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claim_projection import (  # noqa: E402
    ClaimRequest,
    ConsentRecord,
    build_projection,
)
from claim_projection.models import (  # noqa: E402
    PURPOSE_EMPLOYMENT,
    PURPOSE_PROFILE,
    PURPOSE_TEAM,
)
from claim_projection.projector import (  # noqa: E402
    REASON_ATTRIBUTE_MISSING,
    REASON_PURPOSE_UNAUTHORIZED,
)

ALL_PURPOSES = {PURPOSE_PROFILE, PURPOSE_EMPLOYMENT, PURPOSE_TEAM}


def full_attributes():
    return {
        "given_name": "Ada",
        "family_name": "Lovelace",
        "is_active_employee": True,
        "employee_id": "E-1024",
        "is_team_member": True,
        "team_id": "T-7",
        "team_name": "Analytics",
    }


def consent(purposes, version=1):
    return [ConsentRecord(version=version, granted_purposes=frozenset(purposes))]


class ProofFallbackBoundaryTest(unittest.TestCase):
    def test_missing_proof_falls_back_to_authorized_detail(self):
        # 临界条件：布尔证明属性不存在，但明细属性齐备且用途已授权
        attrs = full_attributes()
        del attrs["is_team_member"]
        result = build_projection(
            attrs,
            ALL_PURPOSES,
            consent(ALL_PURPOSES),
            [ClaimRequest("team")],
        )
        self.assertTrue(result.issued)
        self.assertEqual(
            result.claims["team"],
            {"team_id": "T-7", "team_name": "Analytics"},
        )

    def test_missing_proof_and_missing_detail_omits_optional(self):
        # 同一临界条件的拒绝侧：证明与明细属性都不存在 → 省略可选声明
        attrs = full_attributes()
        del attrs["is_team_member"]
        del attrs["team_id"]
        del attrs["team_name"]
        result = build_projection(
            attrs,
            ALL_PURPOSES,
            consent(ALL_PURPOSES),
            [ClaimRequest("team")],
        )
        self.assertTrue(result.issued)
        self.assertEqual(dict(result.claims), {})
        self.assertEqual(result.omitted[0].reason, REASON_ATTRIBUTE_MISSING)

    def test_missing_proof_fallback_still_requires_purpose(self):
        # 回退到明细路径后仍受用途门控：无 team 用途时不得借明细泄露
        attrs = full_attributes()
        del attrs["is_team_member"]
        result = build_projection(
            attrs,
            [PURPOSE_PROFILE, PURPOSE_EMPLOYMENT],
            consent({PURPOSE_PROFILE, PURPOSE_EMPLOYMENT}),
            [ClaimRequest("team")],
        )
        self.assertEqual(dict(result.claims), {})
        self.assertEqual(result.omitted[0].reason, REASON_PURPOSE_UNAUTHORIZED)


class CrossPurposeBoundaryTest(unittest.TestCase):
    def test_derived_claim_needs_every_extra_purpose(self):
        # employment_verification 同时依赖 employment 与 team：
        # 只授 employment 时即使雇佣属性齐备也必须拒绝
        result = build_projection(
            full_attributes(),
            [PURPOSE_PROFILE, PURPOSE_EMPLOYMENT],
            consent({PURPOSE_PROFILE, PURPOSE_EMPLOYMENT}),
            [ClaimRequest("employment_verification", require_detail=True)],
        )
        self.assertEqual(dict(result.claims), {})
        self.assertEqual(result.omitted[0].reason, REASON_PURPOSE_UNAUTHORIZED)
        self.assertIn(PURPOSE_TEAM, result.omitted[0].detail)

    def test_derived_claim_optional_blocked_does_not_stop_other_claims(self):
        # 交错场景：跨用途衍生字段（可选）被阻止，但同批的必需 name 正常签发
        result = build_projection(
            full_attributes(),
            [PURPOSE_PROFILE, PURPOSE_EMPLOYMENT],
            consent({PURPOSE_PROFILE, PURPOSE_EMPLOYMENT}),
            [
                ClaimRequest("employment_verification", require_detail=True),
                ClaimRequest("name", required=True),
            ],
        )
        self.assertTrue(result.issued)
        self.assertEqual(dict(result.claims), {"name": "Ada Lovelace"})
        self.assertEqual(len(result.omitted), 1)
        self.assertEqual(
            result.omitted[0].claim_name, "employment_verification"
        )

    def test_derived_claim_required_blocked_short_circuits(self):
        # 同一授权边界的拒绝侧：衍生字段为必需时整次签发停止
        result = build_projection(
            full_attributes(),
            [PURPOSE_PROFILE, PURPOSE_EMPLOYMENT],
            consent({PURPOSE_PROFILE, PURPOSE_EMPLOYMENT}),
            [
                ClaimRequest("name"),
                ClaimRequest(
                    "employment_verification", required=True, require_detail=True
                ),
            ],
        )
        self.assertFalse(result.issued)
        self.assertEqual(
            result.blocked.claim_name, "employment_verification"
        )
        self.assertEqual(dict(result.claims), {})

    def test_exactly_all_purposes_authorized_is_accepted(self):
        # 恰好满足全部用途的临界接受侧：子集判断必须是 <= 而非严格小于
        result = build_projection(
            full_attributes(),
            ALL_PURPOSES,
            consent(ALL_PURPOSES),
            [ClaimRequest("employment_verification", require_detail=True)],
        )
        self.assertTrue(result.issued)
        self.assertIn("employment_verification", result.claims)


class InterleavingAndEdgeInputTest(unittest.TestCase):
    def test_optional_failure_then_required_success_continues(self):
        result = build_projection(
            full_attributes(),
            [PURPOSE_PROFILE],
            consent({PURPOSE_PROFILE}),
            [
                ClaimRequest("team"),  # 可选，无用途，省略
                ClaimRequest("name", required=True),  # 必需，成功
            ],
        )
        self.assertTrue(result.issued)
        self.assertEqual(dict(result.claims), {"name": "Ada Lovelace"})
        self.assertEqual([item.claim_name for item in result.omitted], ["team"])

    def test_required_failure_short_circuits_later_requests(self):
        result = build_projection(
            full_attributes(),
            [PURPOSE_PROFILE],
            consent({PURPOSE_PROFILE}),
            [
                ClaimRequest("employment_status", required=True),  # 先失败
                ClaimRequest("name"),  # 不会被处理
            ],
        )
        self.assertFalse(result.issued)
        self.assertEqual(result.blocked.claim_name, "employment_status")
        self.assertEqual(dict(result.claims), {})

    def test_duplicate_claim_requests_resolved_once(self):
        result = build_projection(
            full_attributes(),
            ALL_PURPOSES,
            consent(ALL_PURPOSES),
            [
                ClaimRequest("name"),
                ClaimRequest("name", required=True),
                ClaimRequest("name", require_detail=True),
            ],
        )
        self.assertTrue(result.issued)
        self.assertEqual(dict(result.claims), {"name": "Ada Lovelace"})

    def test_empty_requests_produces_empty_projection(self):
        result = build_projection(
            full_attributes(), ALL_PURPOSES, consent(ALL_PURPOSES), []
        )
        self.assertTrue(result.issued)
        self.assertEqual(dict(result.claims), {})
        self.assertEqual(result.omitted, ())

    def test_partial_name_attributes_still_emits_available_part(self):
        attrs = {"given_name": "Ada"}  # 缺 family_name
        result = build_projection(
            attrs,
            [PURPOSE_PROFILE],
            consent({PURPOSE_PROFILE}),
            [ClaimRequest("name")],
        )
        self.assertEqual(result.claims, {"name": "Ada"})

    def test_none_value_treated_as_missing(self):
        attrs = {"given_name": None, "family_name": "Lovelace"}
        result = build_projection(
            attrs,
            [PURPOSE_PROFILE],
            consent({PURPOSE_PROFILE}),
            [ClaimRequest("name")],
        )
        self.assertEqual(result.claims, {"name": "Lovelace"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
