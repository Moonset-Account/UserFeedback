"""可接受结果用例：授权齐备时投影应输出允许的声明。

覆盖题面正向约束：
- 姓名、在岗证明、团队信息在用途授权齐备时正常签发；
- 能用已验证布尔证明满足请求时只发布尔证明（最小披露）；
- 显式要求明细时展开衍生字段，并满足其全部用途授权；
- 整份投影基于同一个（最高版本）同意版本。
"""

import sys
import unittest
from pathlib import Path

# python -I 下 sys.path 不含工作目录，显式加入仓库根以保证可导入
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claim_projection import (  # noqa: E402
    Attribute,
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


def consent_v2():
    return [
        ConsentRecord(version=1, granted_purposes=frozenset({PURPOSE_PROFILE})),
        ConsentRecord(version=2, granted_purposes=frozenset(ALL_PURPOSES)),
    ]


class AcceptableProjectionTest(unittest.TestCase):
    def test_full_authorized_projection_emits_all_claims(self):
        result = build_projection(
            full_attributes(),
            ALL_PURPOSES,
            consent_v2(),
            [
                ClaimRequest("name", required=True),
                ClaimRequest("employment_status"),
                ClaimRequest("team"),
            ],
        )
        self.assertTrue(result.issued)
        self.assertIsNone(result.blocked)
        self.assertEqual(result.consent_version, 2)
        self.assertEqual(
            dict(result.claims),
            {
                "name": "Ada Lovelace",
                "employment_status": True,
                "team": True,
            },
        )
        self.assertEqual(result.omitted, ())
        self.assertEqual(result.effective_purposes, frozenset(ALL_PURPOSES))

    def test_boolean_proof_minimization_hides_detail_attributes(self):
        # 同一身份数据下，默认请求只拿到布尔证明，工号/团队标识不泄露
        result = build_projection(
            full_attributes(),
            ALL_PURPOSES,
            consent_v2(),
            [ClaimRequest("employment_status"), ClaimRequest("team")],
        )
        self.assertEqual(result.claims["employment_status"], True)
        self.assertEqual(result.claims["team"], True)
        joined = repr(dict(result.claims))
        self.assertNotIn("E-1024", joined)
        self.assertNotIn("T-7", joined)
        self.assertNotIn("Analytics", joined)

    def test_explicit_detail_request_expands_derived_claims(self):
        result = build_projection(
            full_attributes(),
            ALL_PURPOSES,
            consent_v2(),
            [
                ClaimRequest("employment_status", require_detail=True),
                ClaimRequest("team", require_detail=True),
            ],
        )
        self.assertTrue(result.issued)
        self.assertEqual(
            result.claims["employment_status"], {"employee_id": "E-1024"}
        )
        self.assertEqual(
            result.claims["team"],
            {"team_id": "T-7", "team_name": "Analytics"},
        )

    def test_strict_entry_returns_claims_dict(self):
        claims = project_claims(
            full_attributes(),
            ALL_PURPOSES,
            consent_v2(),
            [ClaimRequest("name", required=True)],
        )
        self.assertEqual(claims, {"name": "Ada Lovelace"})

    def test_false_boolean_proof_is_still_emitted(self):
        # 已验证的 false 证明也是合法证明，应原样输出而非视为缺失
        attrs = full_attributes()
        attrs["is_active_employee"] = False
        result = build_projection(
            attrs,
            ALL_PURPOSES,
            consent_v2(),
            [ClaimRequest("employment_status")],
        )
        self.assertTrue(result.issued)
        self.assertEqual(result.claims, {"employment_status": False})

    def test_attribute_objects_and_dict_consent_accepted(self):
        # 同时验证 Attribute 对象输入与字典式同意快照输入
        attrs = [
            Attribute("given_name", "Ada"),
            Attribute("family_name", "Lovelace"),
        ]
        consent = [{"version": 1, "granted_purposes": [PURPOSE_PROFILE]}]
        result = build_projection(
            attrs,
            [PURPOSE_PROFILE],
            consent,
            [ClaimRequest("name")],
        )
        self.assertEqual(result.claims, {"name": "Ada Lovelace"})
        self.assertEqual(result.consent_version, 1)

    def test_cross_purpose_derived_claim_with_both_purposes(self):
        result = build_projection(
            full_attributes(),
            ALL_PURPOSES,
            consent_v2(),
            [ClaimRequest("employment_verification", require_detail=True)],
        )
        self.assertTrue(result.issued)
        self.assertEqual(
            result.claims["employment_verification"],
            {
                "active_employee": True,
                "employee_id": "E-1024",
                "team_member": True,
                "team_id": "T-7",
            },
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
