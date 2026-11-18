"""同意版本一致性用例。

覆盖题面"同意被更新时整份投影使用一致的同意版本"：
- 存在多个版本时，整份投影只采用最高版本快照；
- 高版本收窄用途后，即使旧版本曾授权，新投影也必须拒绝；
- 高版本撤回后，所有用途失效，必需声明停止签发；
- 无同意记录、重复版本号等异常输入的处理。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claim_projection import (  # noqa: E402
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
    REASON_CONSENT_MISSING,
    REASON_CONSENT_REVOKED,
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


class ConsentVersionConsistencyTest(unittest.TestCase):
    def test_whole_projection_uses_latest_consent_version(self):
        consent_records = [
            ConsentRecord(version=1, granted_purposes=frozenset(ALL_PURPOSES)),
            ConsentRecord(
                version=2,
                granted_purposes=frozenset({PURPOSE_PROFILE, PURPOSE_EMPLOYMENT}),
            ),
        ]
        result = build_projection(
            full_attributes(),
            ALL_PURPOSES,  # 受众仍允许 team，但用户 v2 已收回
            consent_records,
            [
                ClaimRequest("name"),
                ClaimRequest("employment_status"),
                ClaimRequest("team"),
            ],
        )
        self.assertTrue(result.issued)
        self.assertEqual(result.consent_version, 2)
        # 同一份投影内所有授权判断都基于 v2：team 被省略
        self.assertEqual(set(result.claims), {"name", "employment_status"})
        self.assertEqual(len(result.omitted), 1)
        self.assertEqual(result.omitted[0].reason, REASON_PURPOSE_UNAUTHORIZED)
        self.assertEqual(
            result.effective_purposes,
            frozenset({PURPOSE_PROFILE, PURPOSE_EMPLOYMENT}),
        )

    def test_latest_revoked_version_invalidates_all_claims(self):
        consent_records = [
            ConsentRecord(version=1, granted_purposes=frozenset(ALL_PURPOSES)),
            ConsentRecord(
                version=2, granted_purposes=frozenset(), revoked=True
            ),
        ]
        result = build_projection(
            full_attributes(),
            ALL_PURPOSES,
            consent_records,
            [ClaimRequest("name", required=True)],
        )
        self.assertFalse(result.issued)
        self.assertEqual(result.consent_version, 2)
        self.assertEqual(result.blocked.reason, REASON_CONSENT_REVOKED)
        self.assertEqual(dict(result.claims), {})

    def test_revoked_consent_strict_entry_raises(self):
        with self.assertRaises(ClaimProjectionError) as ctx:
            project_claims(
                full_attributes(),
                ALL_PURPOSES,
                [ConsentRecord(version=1, granted_purposes=frozenset(), revoked=True)],
                [ClaimRequest("name", required=True)],
            )
        self.assertEqual(ctx.exception.blocked.reason, REASON_CONSENT_REVOKED)

    def test_no_consent_record_blocks_required_claim(self):
        result = build_projection(
            full_attributes(),
            ALL_PURPOSES,
            None,
            [ClaimRequest("name", required=True)],
        )
        self.assertFalse(result.issued)
        self.assertEqual(result.blocked.reason, REASON_CONSENT_MISSING)
        self.assertIsNone(result.consent_version)
        self.assertEqual(result.effective_purposes, frozenset())

    def test_no_consent_record_omits_optional_claim(self):
        result = build_projection(
            full_attributes(),
            ALL_PURPOSES,
            [],
            [ClaimRequest("name")],
        )
        self.assertTrue(result.issued)
        self.assertEqual(dict(result.claims), {})
        self.assertEqual(result.omitted[0].reason, REASON_CONSENT_MISSING)

    def test_versions_picked_by_number_not_input_order(self):
        # 乱序输入：版本选择取决于版本号而非列表顺序
        consent_records = [
            ConsentRecord(version=3, granted_purposes=frozenset({PURPOSE_PROFILE})),
            ConsentRecord(version=1, granted_purposes=frozenset(ALL_PURPOSES)),
            ConsentRecord(
                version=2,
                granted_purposes=frozenset(
                    {PURPOSE_PROFILE, PURPOSE_EMPLOYMENT, PURPOSE_TEAM}
                ),
            ),
        ]
        result = build_projection(
            full_attributes(),
            ALL_PURPOSES,
            consent_records,
            [ClaimRequest("name"), ClaimRequest("team")],
        )
        self.assertEqual(result.consent_version, 3)
        self.assertEqual(set(result.claims), {"name"})
        self.assertEqual(result.omitted[0].reason, REASON_PURPOSE_UNAUTHORIZED)

    def test_duplicate_consent_versions_are_rejected(self):
        with self.assertRaises(ValueError):
            build_projection(
                full_attributes(),
                ALL_PURPOSES,
                [
                    ConsentRecord(version=1, granted_purposes=frozenset(ALL_PURPOSES)),
                    ConsentRecord(
                        version=1, granted_purposes=frozenset({PURPOSE_PROFILE})
                    ),
                ],
                [ClaimRequest("name")],
            )

    def test_revoked_record_cannot_carry_granted_purposes(self):
        with self.assertRaises(ValueError):
            ConsentRecord(
                version=1,
                granted_purposes=frozenset({PURPOSE_PROFILE}),
                revoked=True,
            )

    def test_re_grant_after_revocation_uses_new_snapshot(self):
        # v2 撤回、v3 重新授予：投影只看最高快照，恢复正常签发
        consent_records = [
            ConsentRecord(version=1, granted_purposes=frozenset(ALL_PURPOSES)),
            ConsentRecord(version=2, granted_purposes=frozenset(), revoked=True),
            ConsentRecord(
                version=3,
                granted_purposes=frozenset(
                    {PURPOSE_PROFILE, PURPOSE_EMPLOYMENT, PURPOSE_TEAM}
                ),
            ),
        ]
        result = build_projection(
            full_attributes(),
            ALL_PURPOSES,
            consent_records,
            [ClaimRequest("name"), ClaimRequest("team")],
        )
        self.assertTrue(result.issued)
        self.assertEqual(result.consent_version, 3)
        self.assertEqual(set(result.claims), {"name", "team"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
