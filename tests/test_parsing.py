"""字典解析助手与端到端（dict -> Service -> plan_recovery）测试。"""

import unittest

from dr_planner import (
    REASON_BUDGET,
    Backup,
    Service,
    plan_recovery,
    service_from_dict,
)


SERVICE_PAYLOADS = [
    {
        "service_id": "orders",
        "weight": 3,
        "backups": [
            {
                "backup_id": "o1",
                "position": 100,
                "bytes_required": 200,
                "requirements": {"inventory": 80},
            },
            {
                "backup_id": "o2",
                "position": 120,
                "bytes_required": 600,
                "requirements": {"inventory": 110},
            },
        ],
    },
    {
        "service_id": "inventory",
        "weight": 2,
        "backups": [
            {"backup_id": "i1", "position": 80, "bytes_required": 150},
            {"backup_id": "i2", "position": 110, "bytes_required": 500},
        ],
    },
]


class ParsingTest(unittest.TestCase):
    def test_service_from_dict_roundtrip(self):
        services = [service_from_dict(item) for item in SERVICE_PAYLOADS]
        self.assertEqual([s.service_id for s in services], ["orders", "inventory"])
        self.assertEqual(services[0].weight, 3)
        self.assertEqual(
            dict(services[0].backups[0].requirements), {"inventory": 80}
        )
        self.assertEqual(services[1].latest_position, 110)
        self.assertIsInstance(services[0].backups[0], Backup)

    def test_default_weight_is_one(self):
        svc = service_from_dict(
            {"service_id": "s", "backups": [{"backup_id": "b", "position": 1,
                                              "bytes_required": 2}]}
        )
        self.assertEqual(svc.weight, 1)

    def test_missing_field_raises_value_error(self):
        with self.assertRaises(ValueError):
            service_from_dict({"service_id": "s"})
        with self.assertRaises(ValueError):
            service_from_dict(
                {"service_id": "s", "backups": [{"position": 1}]}
            )

    def test_bad_requirement_type_raises(self):
        with self.assertRaises(ValueError):
            service_from_dict(
                {
                    "service_id": "s",
                    "backups": [
                        {
                            "backup_id": "b",
                            "position": 1,
                            "bytes_required": 2,
                            "requirements": [("x", 1)],
                        }
                    ],
                }
            )

    def test_non_mapping_input_raises(self):
        with self.assertRaises(ValueError):
            service_from_dict("not-a-dict")


class EndToEndDictTest(unittest.TestCase):
    """用 JSON 风格数据完整走一遍选择流程，数值手工核对。"""

    def setUp(self):
        self.services = [service_from_dict(item) for item in SERVICE_PAYLOADS]

    def test_tight_budget_chooses_joint_old_level(self):
        # 依赖可行组合：
        #   (o1,i1)=350，损失 (120-100)*3 + (110-80)*2 = 60+60 = 120
        #   (o1,i2)=700，损失 60
        #   (o2,i2)=1100，损失 0（o2 要求 inventory>=110）
        result = plan_recovery(self.services, 500)
        self.assertTrue(result.feasible)
        self.assertEqual(
            [x.backup_id for x in result.selected], ["o1", "i1"]
        )
        self.assertEqual(result.total_bytes, 350)
        self.assertEqual(result.loss, 120)

    def test_mid_budget_picks_mixed_level(self):
        result = plan_recovery(self.services, 700)
        self.assertTrue(result.feasible)
        self.assertEqual(
            [x.backup_id for x in result.selected], ["o1", "i2"]
        )
        self.assertEqual(result.total_bytes, 700)
        self.assertEqual(result.loss, 60)

    def test_generous_budget_reaches_zero_loss(self):
        result = plan_recovery(self.services, 1100)
        self.assertTrue(result.feasible)
        self.assertEqual(
            [x.backup_id for x in result.selected], ["o2", "i2"]
        )
        self.assertEqual(result.loss, 0)

    def test_below_minimum_budget_reports_fix(self):
        result = plan_recovery(self.services, 349)
        self.assertFalse(result.feasible)
        self.assertEqual(result.reason, REASON_BUDGET)
        self.assertTrue(result.budget_can_fix)
        self.assertEqual(result.required_budget, 350)
        self.assertEqual(
            [x.backup_id for x in result.selected], ["o1", "i1"]
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
