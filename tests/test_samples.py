"""样例用例测试：samples/ 下每个 case 的输入必须产出 expected.json 声明的结论。

每个 case 对应需求中的一条明确要求：
- case-01-closable: 部署到位 + 修复前后证据配对通过 => 可关闭
- case-02-outdated-deployment: 在役实例仍跑旧产物 => 待部署
- case-03-candidate-only: 测试通过但只覆盖候选产物 => 待部署
- case-04-unpaired-checks: 修复前后结果无法配对 => 待验证并列出缺项
- case-05-decommissioned-and-new-instance: 下线实例单独统计；新上线实例缺证据 => 待验证
"""

import json
import os
import unittest

from aggregator.loader import load_bundle
from aggregator.evaluate import evaluate

SAMPLES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")


def iter_cases():
    for name in sorted(os.listdir(SAMPLES_DIR)):
        case_dir = os.path.join(SAMPLES_DIR, name)
        if os.path.isdir(case_dir):
            yield name, case_dir


class TestSamples(unittest.TestCase):

    def test_at_least_one_sample_case_exists(self):
        self.assertGreaterEqual(len(list(iter_cases())), 1)

    def test_sample_cases_match_expected(self):
        for name, case_dir in iter_cases():
            with self.subTest(case_id=name):
                with open(os.path.join(case_dir, "expected.json"),
                          encoding="utf-8") as fh:
                    expected = json.load(fh)
                report = evaluate(load_bundle(os.path.join(case_dir, "input.json")))
                actual = {(d["component"], d["environment"]): d
                          for d in report["decisions"]}
                for exp in expected["decisions"]:
                    key = (exp["component"], exp["environment"])
                    self.assertIn(key, actual, "缺少判定: %s" % (key,))
                    decision = actual[key]
                    self.assertEqual(decision["status"], exp["status"],
                                     "状态不符: %s 期望 %s 实际 %s"
                                     % (key, exp["status"], decision["status"]))
                    self.assertEqual(len(decision["missing"]), exp["missing_count"])
                    self.assertEqual(decision["summary"]["decommissioned_instances"],
                                     exp["decommissioned_count"])
                    self.assertEqual(decision["summary"]["new_instances"],
                                     exp["new_instance_count"])
                    self.assertTrue(decision["evidence_chain"],
                                    "证据链不能为空: %s" % (key,))


if __name__ == "__main__":
    unittest.main()
