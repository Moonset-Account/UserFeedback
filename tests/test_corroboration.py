"""佐证判定库验收测试。

覆盖题面交叉约束：
- 可接受结果：两条同向通告、各自一套依据、原始采集源完全不交叠 -> 已佐证。
- 应被阻止结果：两套依据共享采集源 / 只有一条通告 / 纯互引循环 -> 不予佐证。
- 边界与交错：转引链独立性、接地循环、争议（双向达标）、组内“缺一不可”、
  同一通告的两套组不算两条结论、共享源临界对比、CLI 端到端。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from corroboration import (
    Advisory,
    CorroborationEngine,
    Observation,
    STATUS_CORROBORATED,
    STATUS_DISPUTED,
    STATUS_UNCORROBORATED,
)
from corroboration.cli import evaluate_file, main as cli_main


def obs(oid, indicator, source):
    return Observation(id=oid, indicator=indicator, source=source)


def adv(aid, indicator, direction, groups):
    return Advisory(
        id=aid,
        indicator=indicator,
        direction=direction,
        evidence_groups=tuple(tuple(g) for g in groups),
    )


def evaluate(observations, advisories):
    return CorroborationEngine(observations, advisories).evaluate()


class TestCorroboratedAccepted(unittest.TestCase):
    """可接受结果：两个同向结论 + 各自一套依据 + 采集源完全不交叠。"""

    def test_two_independent_same_direction_advisories(self):
        verdicts = evaluate(
            observations=[
                obs("obs-1", "ind-1", "sensor-a"),
                obs("obs-2", "ind-1", "sensor-b"),
            ],
            advisories=[
                adv("adv-1", "ind-1", "malicious", [["obs-1"]]),
                adv("adv-2", "ind-1", "malicious", [["obs-2"]]),
            ],
        )
        verdict = verdicts["ind-1"]
        self.assertEqual(verdict.status, STATUS_CORROBORATED)
        self.assertIn("malicious", verdict.support)
        side_a, side_b = verdict.support["malicious"]
        # 见证必须来自两条不同通告
        self.assertNotEqual(side_a.advisory_id, side_b.advisory_id)
        self.assertEqual({side_a.advisory_id, side_b.advisory_id}, {"adv-1", "adv-2"})
        # 两套来源完全不交叠，审核者可据此核对独立性
        self.assertTrue(set(side_a.sources).isdisjoint(side_b.sources))
        self.assertEqual(set(side_a.sources) | set(side_b.sources), {"sensor-a", "sensor-b"})

    def test_witness_reports_underlying_observations(self):
        verdicts = evaluate(
            observations=[
                obs("obs-1", "ind-1", "sensor-a"),
                obs("obs-2", "ind-1", "sensor-b"),
            ],
            advisories=[
                adv("adv-1", "ind-1", "malicious", [["obs-1"]]),
                adv("adv-2", "ind-1", "malicious", [["obs-2"]]),
            ],
        )
        side_a, side_b = verdicts["ind-1"].support["malicious"]
        self.assertEqual(side_a.observation_ids, ("obs-1",))
        self.assertEqual(side_b.observation_ids, ("obs-2",))
        self.assertEqual(side_a.evidence_group, ("obs-1",))


class TestCorroborationRejected(unittest.TestCase):
    """应被阻止的结果。"""

    def test_shared_source_is_not_independent(self):
        # 两条通告看似独立，回溯后都依赖 sensor-a 的观测 -> 不予佐证
        verdicts = evaluate(
            observations=[
                obs("obs-1", "ind-1", "sensor-a"),
                obs("obs-2", "ind-1", "sensor-a"),
            ],
            advisories=[
                adv("adv-1", "ind-1", "malicious", [["obs-1"]]),
                adv("adv-2", "ind-1", "malicious", [["obs-2"]]),
            ],
        )
        self.assertEqual(verdicts["ind-1"].status, STATUS_UNCORROBORATED)
        self.assertEqual(verdicts["ind-1"].support, {})

    def test_single_advisory_is_not_enough(self):
        verdicts = evaluate(
            observations=[obs("obs-1", "ind-1", "sensor-a")],
            advisories=[adv("adv-1", "ind-1", "malicious", [["obs-1"]])],
        )
        self.assertEqual(verdicts["ind-1"].status, STATUS_UNCORROBORATED)

    def test_same_advisory_two_groups_does_not_count(self):
        # 边界：同一通告的两套独立证据组不等于“两个结论”
        verdicts = evaluate(
            observations=[
                obs("obs-1", "ind-1", "sensor-a"),
                obs("obs-2", "ind-1", "sensor-b"),
            ],
            advisories=[
                adv("adv-1", "ind-1", "malicious", [["obs-1"], ["obs-2"]]),
            ],
        )
        self.assertEqual(verdicts["ind-1"].status, STATUS_UNCORROBORATED)

    def test_pure_citation_cycle_is_not_observation(self):
        # 纯粹互引的循环：没有任何观测落地，两组通告互相转引不算数
        verdicts = evaluate(
            observations=[],
            advisories=[
                adv("adv-1", "ind-1", "malicious", [["adv-2"]]),
                adv("adv-2", "ind-1", "malicious", [["adv-1"]]),
            ],
        )
        self.assertEqual(verdicts["ind-1"].status, STATUS_UNCORROBORATED)

    def test_self_citation_does_not_ground(self):
        verdicts = evaluate(
            observations=[],
            advisories=[adv("adv-1", "ind-1", "malicious", [["adv-1"]])],
        )
        self.assertEqual(verdicts["ind-1"].status, STATUS_UNCORROBORATED)


class TestBoundaryAndInterleaved(unittest.TestCase):
    """边界与交错场景。"""

    def test_citation_chain_independence(self):
        # adv-2 经转引链落地到 sensor-b；与 adv-1 的 sensor-a 独立 -> 佐证。
        # 审核者可从见证中看到 adv-2 实际走的是 adv-3 这条转引路线。
        verdicts = evaluate(
            observations=[
                obs("obs-1", "ind-1", "sensor-a"),
                obs("obs-2", "ind-1", "sensor-b"),
            ],
            advisories=[
                adv("adv-1", "ind-1", "malicious", [["obs-1"]]),
                adv("adv-2", "ind-1", "malicious", [["adv-3"]]),
                adv("adv-3", "ind-1", "malicious", [["obs-2"]]),
            ],
        )
        verdict = verdicts["ind-1"]
        self.assertEqual(verdict.status, STATUS_CORROBORATED)
        sides = {s.advisory_id: s for s in verdict.support["malicious"]}
        self.assertEqual(sides["adv-2"].sources, ("sensor-b",))
        self.assertEqual(sides["adv-2"].observation_ids, ("obs-2",))
        self.assertEqual(sides["adv-2"].evidence_group, ("adv-3",))

    def test_rerouted_citation_breaks_independence(self):
        # 交错边界：换一条转引路线后，两条结论回溯到同一采集源 -> 不再独立。
        # adv-1 与 adv-2 表面不同，实际都只吃 sensor-a。
        verdicts = evaluate(
            observations=[obs("obs-1", "ind-1", "sensor-a")],
            advisories=[
                adv("adv-1", "ind-1", "malicious", [["obs-1"]]),
                adv("adv-2", "ind-1", "malicious", [["adv-3"]]),
                adv("adv-3", "ind-1", "malicious", [["obs-1"]]),
            ],
        )
        self.assertEqual(verdicts["ind-1"].status, STATUS_UNCORROBORATED)

    def test_grounded_cycle_shares_source_then_rescued(self):
        # 接地循环：adv-1 <-> adv-2 互引，adv-2 另有观测组落地。
        # adv-1 经循环回溯到的仍是 obs-1，与 adv-2 不独立 -> 不予佐证。
        observations = [
            obs("obs-1", "ind-1", "sensor-a"),
            obs("obs-2", "ind-1", "sensor-b"),
        ]
        advisories = [
            adv("adv-1", "ind-1", "malicious", [["adv-2"]]),
            adv("adv-2", "ind-1", "malicious", [["adv-1"], ["obs-1"]]),
        ]
        self.assertEqual(evaluate(observations, advisories)["ind-1"].status,
                         STATUS_UNCORROBORATED)
        # 加入第三条独立通告后达到门槛
        advisories.append(adv("adv-3", "ind-1", "malicious", [["obs-2"]]))
        verdicts = evaluate(observations, advisories)
        self.assertEqual(verdicts["ind-1"].status, STATUS_CORROBORATED)
        side_a, side_b = verdicts["ind-1"].support["malicious"]
        self.assertTrue(set(side_a.sources).isdisjoint(side_b.sources))

    def test_disputed_when_both_directions_meet_threshold(self):
        observations = [
            obs("obs-1", "ind-1", "sensor-a"),
            obs("obs-2", "ind-1", "sensor-b"),
            obs("obs-3", "ind-1", "sensor-c"),
            obs("obs-4", "ind-1", "sensor-d"),
        ]
        advisories = [
            adv("adv-1", "ind-1", "malicious", [["obs-1"]]),
            adv("adv-2", "ind-1", "malicious", [["obs-2"]]),
            adv("adv-3", "ind-1", "benign", [["obs-3"]]),
            adv("adv-4", "ind-1", "benign", [["obs-4"]]),
        ]
        verdict = evaluate(observations, advisories)["ind-1"]
        self.assertEqual(verdict.status, STATUS_DISPUTED)
        self.assertEqual(set(verdict.support), {"malicious", "benign"})

    def test_one_direction_only_is_corroborated_not_disputed(self):
        observations = [
            obs("obs-1", "ind-1", "sensor-a"),
            obs("obs-2", "ind-1", "sensor-b"),
            obs("obs-3", "ind-1", "sensor-c"),
        ]
        advisories = [
            adv("adv-1", "ind-1", "malicious", [["obs-1"]]),
            adv("adv-2", "ind-1", "malicious", [["obs-2"]]),
            # 反方向只有一条结论，未达门槛 -> 仍判佐证而非争议
            adv("adv-3", "ind-1", "benign", [["obs-3"]]),
        ]
        verdict = evaluate(observations, advisories)["ind-1"]
        self.assertEqual(verdict.status, STATUS_CORROBORATED)
        self.assertEqual(set(verdict.support), {"malicious"})

    def test_group_requires_all_members(self):
        # 组内缺一不可：adv-2 的首选组缺 obs-x 整组失败，替代组顶上后达标
        verdicts = evaluate(
            observations=[
                obs("obs-1", "ind-1", "sensor-a"),
                obs("obs-2", "ind-1", "sensor-b"),
            ],
            advisories=[
                adv("adv-1", "ind-1", "malicious", [["obs-1"]]),
                adv("adv-2", "ind-1", "malicious", [["obs-2", "adv-missing-ok"], ["obs-2"]]),
                # adv-missing-ok 是纯循环，无法落地，使首选组失败
                adv("adv-missing-ok", "ind-1", "malicious", [["adv-missing-ok"]]),
            ],
        )
        verdict = verdicts["ind-1"]
        self.assertEqual(verdict.status, STATUS_CORROBORATED)
        sides = {s.advisory_id: s for s in verdict.support["malicious"]}
        # adv-2 实际采用的是只含 obs-2 的替代组
        self.assertEqual(sides["adv-2"].evidence_group, ("obs-2",))

    def test_group_and_semantics_merge_sources(self):
        # 组内两条材料缺一不可，且来源合并后参与交叠判定：
        # adv-1 的组同时吃 sensor-a 与 sensor-b，与 adv-2 的 sensor-b 交叠 -> 不予佐证
        verdicts = evaluate(
            observations=[
                obs("obs-1", "ind-1", "sensor-a"),
                obs("obs-2", "ind-1", "sensor-b"),
                obs("obs-3", "ind-1", "sensor-b"),
            ],
            advisories=[
                adv("adv-1", "ind-1", "malicious", [["obs-1", "obs-2"]]),
                adv("adv-2", "ind-1", "malicious", [["obs-3"]]),
            ],
        )
        self.assertEqual(verdicts["ind-1"].status, STATUS_UNCORROBORATED)

    def test_shared_source_boundary_flip(self):
        # 临界对比：唯一共享源被替换为独立源后，裁决从拒绝翻转为接受
        base_obs = [
            obs("obs-1", "ind-1", "sensor-a"),
            obs("obs-2", "ind-1", "sensor-shared"),
        ]
        base_adv = [
            adv("adv-1", "ind-1", "malicious", [["obs-1"]]),
            adv("adv-2", "ind-1", "malicious", [["obs-1", "obs-2"]]),
        ]
        self.assertEqual(evaluate(base_obs, base_adv)["ind-1"].status,
                         STATUS_UNCORROBORATED)
        fixed_obs = [
            obs("obs-1", "ind-1", "sensor-a"),
            obs("obs-2", "ind-1", "sensor-b"),
        ]
        fixed_adv = [
            adv("adv-1", "ind-1", "malicious", [["obs-1"]]),
            adv("adv-2", "ind-1", "malicious", [["obs-2"]]),
        ]
        self.assertEqual(evaluate(fixed_obs, fixed_adv)["ind-1"].status,
                         STATUS_CORROBORATED)

    def test_indicators_evaluated_independently(self):
        verdicts = evaluate(
            observations=[
                obs("obs-1", "ind-1", "sensor-a"),
                obs("obs-2", "ind-1", "sensor-b"),
                obs("obs-3", "ind-2", "sensor-a"),
            ],
            advisories=[
                adv("adv-1", "ind-1", "malicious", [["obs-1"]]),
                adv("adv-2", "ind-1", "malicious", [["obs-2"]]),
                adv("adv-3", "ind-2", "malicious", [["obs-3"]]),
            ],
        )
        self.assertEqual(verdicts["ind-1"].status, STATUS_CORROBORATED)
        self.assertEqual(verdicts["ind-2"].status, STATUS_UNCORROBORATED)


class TestInputValidation(unittest.TestCase):
    def test_unknown_reference_rejected(self):
        with self.assertRaises(ValueError):
            evaluate([], [adv("adv-1", "ind-1", "malicious", [["obs-ghost"]])])

    def test_empty_group_rejected(self):
        with self.assertRaises(ValueError):
            evaluate([], [adv("adv-1", "ind-1", "malicious", [[]])])

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(ValueError):
            evaluate(
                [obs("obs-1", "ind-1", "sensor-a"), obs("obs-1", "ind-1", "sensor-b")],
                [],
            )


class TestCliEndToEnd(unittest.TestCase):
    """CLI 端到端：真实执行入口并断言 JSON 输出。"""

    PAYLOAD = {
        "observations": [
            {"id": "obs-1", "indicator": "ind-1", "source": "sensor-a"},
            {"id": "obs-2", "indicator": "ind-1", "source": "sensor-b"},
            {"id": "obs-3", "indicator": "ind-2", "source": "sensor-a"},
        ],
        "advisories": [
            {"id": "adv-1", "indicator": "ind-1", "direction": "malicious",
             "evidence_groups": [["obs-1"]]},
            {"id": "adv-2", "indicator": "ind-1", "direction": "malicious",
             "evidence_groups": [["adv-3"]]},
            {"id": "adv-3", "indicator": "ind-1", "direction": "malicious",
             "evidence_groups": [["obs-2"]]},
            {"id": "adv-4", "indicator": "ind-2", "direction": "malicious",
             "evidence_groups": [["obs-3"]]},
        ],
    }

    def test_evaluate_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "input.json")
            with open(src, "w", encoding="utf-8") as fh:
                json.dump(self.PAYLOAD, fh)
            result = evaluate_file(src)
        verdicts = {v["indicator"]: v for v in result["verdicts"]}
        self.assertEqual(verdicts["ind-1"]["status"], STATUS_CORROBORATED)
        self.assertEqual(verdicts["ind-2"]["status"], STATUS_UNCORROBORATED)
        support = verdicts["ind-1"]["support"]["malicious"]
        source_sets = [set(side["sources"]) for side in support]
        self.assertTrue(source_sets[0].isdisjoint(source_sets[1]))

    def test_module_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "input.json")
            out = os.path.join(tmp, "out.json")
            with open(src, "w", encoding="utf-8") as fh:
                json.dump(self.PAYLOAD, fh)
            repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            proc = subprocess.run(
                [sys.executable, "-m", "corroboration", src, "-o", out],
                cwd=repo_root, capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(out, "r", encoding="utf-8") as fh:
                result = json.load(fh)
        statuses = {v["indicator"]: v["status"] for v in result["verdicts"]}
        self.assertEqual(statuses["ind-1"], STATUS_CORROBORATED)

    def test_cli_rejects_invalid_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "bad.json")
            with open(src, "w", encoding="utf-8") as fh:
                json.dump({"advisories": [
                    {"id": "adv-1", "indicator": "ind-1", "direction": "malicious",
                     "evidence_groups": [["obs-ghost"]]},
                ]}, fh)
            self.assertEqual(cli_main([src]), 2)


if __name__ == "__main__":
    unittest.main()
