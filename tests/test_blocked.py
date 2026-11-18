"""阻止与拒绝用例：授权不成立时必须停止签发或省略声明。

覆盖题面反向约束：
- 必需字段无法授权 → 停止本次签发，不输出任何声明，严格入口抛异常；
- 可选字段无法授权 → 省略并记录原因，其余声明照常签发；
- 受众允许但用户未同意、用户同意但受众未允许，两种交集缺失均拒绝；
- 未验证/缺失的原始属性不得进入投影；
- 未知声明、非布尔证明等异常输入被拒绝。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claim_projection import (  # noqa: E402
    Attribute,
    ClaimProjectionError,
    ClaimRequest,
    ConsentRecord,
    build_projection,
    project_claims,
)
from claim_projection.models import (  # noqa: E402
    PURPOSE_EMPLOYMENT,
    PURPOSE_PROFILE,
    PURPOSE_TEAM,
)
from claim_projection.projector import (  # noqa: E402
    REASON_ATTRIBUTE_UNVERIFIED,
    REASON_PURPOSE_UNAUTHORIZED,
    REASON_UNKNOWN_CLAIM,
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


class RequiredClaimBlockedTest(unittest.TestCase):
    def test_required_claim_without_purpose_stops_issuance(self):
        # 受众只要 profile 用途，name 所需属性未获 employment/team 之外授权：
        # 这里让必需的 employment_status 缺少 employment 用途
        consent = [
            ConsentRecord(version=1, granted_purposes=frozenset({PURPOSE_PROFILE}))
        ]
        result = build_projection(
            full_attributes(),
            [PURPOSE_PROFILE],
            consent,
            [
                ClaimRequest("name"),
                ClaimRequest("employment_status", required=True),
            ],
        )
        self.assertFalse(result.issued)
        self.assertTrue(result.stopped)
        self.assertIsNotNone(result.blocked)
        self.assertEqual(result.blocked.claim_name, "employment_status")
        self.assertTrue(result.blocked.required)
        self.assertEqual(result.blocked.reason, REASON_PURPOSE_UNAUTHORIZED)
        # 停止签发：此前已收集的声明也不得输出
        self.assertEqual(dict(result.claims), {})

    def test_strict_entry_raises_on_blocked_required_claim(self):
        consent = [
            ConsentRecord(version=1, granted_purposes=frozenset({PURPOSE_PROFILE}))
        ]
        with self.assertRaises(ClaimProjectionError) as ctx:
            project_claims(
                full_attributes(),
                [PURPOSE_PROFILE],
                consent,
                [ClaimRequest("employment_status", required=True)],
            )
        self.assertEqual(ctx.exception.blocked.claim_name, "employment_status")
        self.assertEqual(ctx.exception.consent_version, 1)

    def test_optional_claim_without_purpose_is_omitted(self):
        consent = [
            ConsentRecord(
                version=1,
                granted_purposes=frozenset(
                    {PURPOSE_PROFILE, PURPOSE_EMPLOYMENT}
                ),
            )
        ]
        result = build_projection(
            full_attributes(),
            [PURPOSE_PROFILE, PURPOSE_EMPLOYMENT],
            consent,
            [
                ClaimRequest("name"),
                ClaimRequest("employment_status"),
                ClaimRequest("team"),  # 可选且无 team 用途
            ],
        )
        self.assertTrue(result.issued)
        self.assertNotIn("team", result.claims)
        self.assertEqual(set(result.claims), {"name", "employment_status"})
        self.assertEqual(len(result.omitted), 1)
        self.assertEqual(result.omitted[0].claim_name, "team")
        self.assertFalse(result.omitted[0].required)
        self.assertEqual(result.omitted[0].reason, REASON_PURPOSE_UNAUTHORIZED)

    def test_audience_allowance_and_user_consent_intersection(self):
        attrs = full_attributes()
        consent = [
            ConsentRecord(
                version=1,
                granted_purposes=frozenset(
                    {PURPOSE_PROFILE, PURPOSE_TEAM}
                ),
            )
        ]
        # 受众允许 employment 但用户未同意 team → team 证明不可发
        result = build_projection(
            attrs,
            [PURPOSE_PROFILE, PURPOSE_EMPLOYMENT],
            consent,
            [ClaimRequest("team")],
        )
        self.assertTrue(result.issued)
        self.assertEqual(dict(result.claims), {})
        self.assertEqual(result.omitted[0].reason, REASON_PURPOSE_UNAUTHORIZED)
        self.assertIn("team", result.omitted[0].detail)

    def test_unverified_attribute_blocks_required_claim(self):
        attrs = dict(full_attributes())
        attrs["family_name"] = Attribute(
            name="family_name", value="Lovelace", verified=False
        )
        consent = [ConsentRecord(version=1, granted_purposes=frozenset(ALL_PURPOSES))]
        result = build_projection(
            attrs,
            ALL_PURPOSES,
            consent,
            [ClaimRequest("name", required=True)],
        )
        self.assertFalse(result.issued)
        self.assertEqual(result.blocked.reason, REASON_ATTRIBUTE_UNVERIFIED)
        self.assertIn("family_name", result.blocked.detail)

    def test_missing_attribute_omits_optional_claim(self):
        attrs = full_attributes()
        del attrs["team_id"]
        del attrs["team_name"]
        consent = [ConsentRecord(version=1, granted_purposes=frozenset(ALL_PURPOSES))]
        result = build_projection(
            attrs,
            ALL_PURPOSES,
            consent,
            [
                ClaimRequest("team", require_detail=True),
                ClaimRequest("name"),
            ],
        )
        self.assertTrue(result.issued)
        self.assertNotIn("team", result.claims)
        self.assertEqual([item.claim_name for item in result.omitted], ["team"])

    def test_unknown_claim_required_stops_issuance(self):
        consent = [ConsentRecord(version=1, granted_purposes=frozenset(ALL_PURPOSES))]
        result = build_projection(
            full_attributes(),
            ALL_PURPOSES,
            consent,
            [ClaimRequest("nonexistent", required=True)],
        )
        self.assertFalse(result.issued)
        self.assertEqual(result.blocked.reason, REASON_UNKNOWN_CLAIM)

    def test_unknown_optional_claim_is_omitted(self):
        consent = [ConsentRecord(version=1, granted_purposes=frozenset(ALL_PURPOSES))]
        result = build_projection(
            full_attributes(),
            ALL_PURPOSES,
            consent,
            [ClaimRequest("ghost"), ClaimRequest("name")],
        )
        self.assertTrue(result.issued)
        self.assertEqual(dict(result.claims), {"name": "Ada Lovelace"})
        self.assertEqual(result.omitted[0].reason, REASON_UNKNOWN_CLAIM)

    def test_non_boolean_proof_value_is_rejected(self):
        attrs = full_attributes()
        attrs["is_active_employee"] = "yes"  # 非布尔证明值
        consent = [ConsentRecord(version=1, granted_purposes=frozenset(ALL_PURPOSES))]
        result = build_projection(
            attrs,
            ALL_PURPOSES,
            consent,
            [ClaimRequest("employment_status", required=True)],
        )
        self.assertFalse(result.issued)
        self.assertEqual(result.blocked.reason, "proof_not_boolean")


if __name__ == "__main__":
    unittest.main(verbosity=2)
