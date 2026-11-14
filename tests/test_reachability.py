"""SEC-VULN-004 可达性分析的单元测试。

每个验收点对应独立测试:
- 有循环的插件调用
- 同名函数分属不同依赖实例
- 发布模式不包含测试入口
- 动态目标未解析导致无法判断
- 重复运行结果一致
"""
import json
import os
import unittest

from reachability.analyzer import analyze, analyze_mode
from reachability.loader import load_dataset
from reachability.model import InputError, dataset_from_dicts

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")

INSTANCES = [
    {"id": "acme@1.4.0", "name": "acme", "version": "1.4.0"},
    {"id": "acme@2.1.0", "name": "acme", "version": "2.1.0"},
    {"id": "plugkit@0.9.2", "name": "plugkit", "version": "0.9.2"},
    {"id": "testkit@3.0.1", "name": "testkit", "version": "3.0.1"},
]

AFFECTED_PARSE = {"symbol": "acme@1.4.0:parse_token",
                  "advisory": "SEC-VULN-004", "severity": "high"}
AFFECTED_DESER = {"symbol": "acme@1.4.0:deserialize",
                  "advisory": "SEC-VULN-004", "severity": "critical"}


def make_dataset(edges, modes, affected, instances=INSTANCES):
    return dataset_from_dicts(
        {"instances": instances},
        {"edges": edges},
        {"modes": modes},
        {"affected": affected},
    )


def edge(caller, callee, build_mode="all", resolution="resolved", reason=""):
    e = {"caller": caller, "callee": callee,
         "build_mode": build_mode, "resolution": resolution}
    if reason:
        e["reason"] = reason
    return e


class TestPluginCycle(unittest.TestCase):
    """验收点: 有循环的插件调用。"""

    def setUp(self):
        self.dataset = make_dataset(
            edges=[
                edge("plugin.loader:load", "plugkit@0.9.2:handle"),
                edge("plugkit@0.9.2:handle", "plugkit@0.9.2:emit"),
                # 循环边: emit 回调 handle
                edge("plugkit@0.9.2:emit", "plugkit@0.9.2:handle"),
                edge("plugkit@0.9.2:emit", "acme@1.4.0:parse_token"),
            ],
            modes={"release": ["plugin.loader:load"]},
            affected=[AFFECTED_PARSE],
        )

    def test_cycle_terminates_and_symbol_reachable(self):
        result = analyze_mode(self.dataset, "release")
        info = result["symbols"]["acme@1.4.0:parse_token"]
        self.assertEqual(info["status"], "reachable")

    def test_path_has_no_repeated_nodes(self):
        result = analyze_mode(self.dataset, "release")
        path = result["symbols"]["acme@1.4.0:parse_token"]["path"]
        self.assertEqual(path[0], "plugin.loader:load")
        self.assertEqual(path[-1], "acme@1.4.0:parse_token")
        self.assertEqual(len(path), len(set(path)),
                         "循环节点不应在路径中重复出现")
        self.assertIn("plugkit@0.9.2:emit", path)


class TestSameNameDistinctInstances(unittest.TestCase):
    """验收点: 同名函数分属不同依赖实例。"""

    def test_fixed_instance_not_flagged(self):
        dataset = make_dataset(
            edges=[
                edge("app.main:run", "acme@2.1.0:parse"),
                edge("acme@2.1.0:parse", "acme@2.1.0:parse_token"),
            ],
            modes={"release": ["app.main:run"]},
            affected=[AFFECTED_PARSE],
        )
        result = analyze_mode(dataset, "release")
        info = result["symbols"]["acme@1.4.0:parse_token"]
        # 到达的是 acme@2.1.0:parse_token(已修复实例), 不是受影响实例
        self.assertEqual(info["status"], "unreachable")
        self.assertTrue(result["evidence_complete"])
        self.assertNotIn("acme@2.1.0:parse_token", result["symbols"])

    def test_vulnerable_instance_flagged_when_reached(self):
        dataset = make_dataset(
            edges=[edge("app.main:run", "acme@1.4.0:parse_token")],
            modes={"release": ["app.main:run"]},
            affected=[AFFECTED_PARSE],
        )
        result = analyze_mode(dataset, "release")
        self.assertEqual(
            result["symbols"]["acme@1.4.0:parse_token"]["status"], "reachable")


class TestReleaseExcludesTestEntries(unittest.TestCase):
    """验收点: 发布模式不包含测试入口。"""

    def setUp(self):
        self.dataset = make_dataset(
            edges=[
                edge("tests.harness:main", "testkit@3.0.1:run_case",
                     build_mode="test"),
                edge("testkit@3.0.1:run_case", "acme@1.4.0:deserialize",
                     build_mode="test"),
            ],
            modes={"release": ["app.main:run"],
                   "test": ["tests.harness:main"]},
            affected=[AFFECTED_DESER],
        )

    def test_reachable_in_test_mode(self):
        result = analyze_mode(self.dataset, "test")
        info = result["symbols"]["acme@1.4.0:deserialize"]
        self.assertEqual(info["status"], "reachable")
        self.assertEqual(info["path"][0], "tests.harness:main")

    def test_unreachable_in_release_mode(self):
        result = analyze_mode(self.dataset, "release")
        info = result["symbols"]["acme@1.4.0:deserialize"]
        self.assertEqual(info["status"], "unreachable")
        self.assertTrue(result["evidence_complete"])


class TestUnresolvedDynamicTarget(unittest.TestCase):
    """验收点: 动态目标未解析导致无法判断。"""

    def setUp(self):
        self.dataset = make_dataset(
            edges=[
                edge("tools.repl:main", "plugins:dynamic_dispatch",
                     build_mode="dev", resolution="unresolved",
                     reason="getattr 动态分发"),
            ],
            modes={"dev": ["tools.repl:main"],
                   "release": ["app.main:run"]},
            affected=[AFFECTED_DESER],
        )

    def test_insufficient_evidence_in_dev(self):
        result = analyze_mode(self.dataset, "dev")
        info = result["symbols"]["acme@1.4.0:deserialize"]
        self.assertEqual(info["status"], "insufficient_evidence")
        self.assertFalse(result["evidence_complete"])
        self.assertEqual(len(info["missing_evidence"]), 1)
        self.assertIn("tools.repl:main", info["missing_evidence"][0])
        self.assertIn("getattr", info["missing_evidence"][0])

    def test_same_symbol_unreachable_where_edge_not_applicable(self):
        # 未解析边只属于 dev 模式; release 证据完整, 可判定不可达
        result = analyze_mode(self.dataset, "release")
        info = result["symbols"]["acme@1.4.0:deserialize"]
        self.assertEqual(info["status"], "unreachable")

    def test_unreachable_unresolved_edge_does_not_weaken_verdict(self):
        # 未解析边的调用方从入口不可达时, 证据仍算完整
        dataset = make_dataset(
            edges=[
                edge("other.mod:x", "plugins:dynamic_dispatch",
                     build_mode="all", resolution="unresolved",
                     reason="动态加载"),
            ],
            modes={"release": ["app.main:run"]},
            affected=[AFFECTED_DESER],
        )
        result = analyze_mode(dataset, "release")
        self.assertTrue(result["evidence_complete"])
        self.assertEqual(
            result["symbols"]["acme@1.4.0:deserialize"]["status"],
            "unreachable")


class TestDeterminism(unittest.TestCase):
    """验收点: 同一输入重复运行得到一致结果。"""

    def setUp(self):
        self.edges = [
            edge("app.main:run", "acme@2.1.0:parse"),
            edge("plugin.loader:load", "plugkit@0.9.2:handle"),
            edge("plugkit@0.9.2:handle", "plugkit@0.9.2:emit"),
            edge("plugkit@0.9.2:emit", "plugkit@0.9.2:handle"),
            edge("plugkit@0.9.2:emit", "acme@1.4.0:parse_token"),
            edge("tools.repl:main", "plugins:dynamic_dispatch",
                 build_mode="dev", resolution="unresolved", reason="动态分发"),
        ]
        self.modes = {"release": ["app.main:run", "plugin.loader:load"],
                      "dev": ["tools.repl:main"]}
        self.affected = [AFFECTED_PARSE, AFFECTED_DESER]

    def _report_json(self, edges):
        dataset = make_dataset(edges=list(edges), modes=self.modes,
                               affected=self.affected)
        return json.dumps(analyze(dataset), ensure_ascii=False, sort_keys=True)

    def test_repeat_runs_identical(self):
        self.assertEqual(self._report_json(self.edges),
                         self._report_json(self.edges))

    def test_edge_order_irrelevant(self):
        self.assertEqual(self._report_json(self.edges),
                         self._report_json(list(reversed(self.edges))))


class TestPriorityFixes(unittest.TestCase):
    """发布入口修复优先级清单。"""

    def test_only_release_entries_reaching_affected_listed(self):
        dataset = make_dataset(
            edges=[
                edge("app.main:run", "acme@2.1.0:parse"),
                edge("acme@2.1.0:parse", "acme@2.1.0:parse_token"),
                edge("plugin.loader:load", "plugkit@0.9.2:handle"),
                edge("plugkit@0.9.2:handle", "acme@1.4.0:parse_token"),
            ],
            modes={"release": ["app.main:run", "plugin.loader:load"]},
            affected=[AFFECTED_PARSE],
        )
        report = analyze(dataset)
        fixes = [(f["entry"], f["symbol"]) for f in report["priority_fixes"]]
        self.assertEqual(fixes,
                         [("plugin.loader:load", "acme@1.4.0:parse_token")])
        path = report["priority_fixes"][0]["path"]
        self.assertEqual(path[0], "plugin.loader:load")
        self.assertEqual(path[-1], "acme@1.4.0:parse_token")


class TestValidation(unittest.TestCase):
    """输入校验。"""

    def test_unknown_affected_instance_rejected(self):
        with self.assertRaises(InputError):
            make_dataset(edges=[], modes={"release": ["app.main:run"]},
                         affected=[{"symbol": "ghost@9.9.9:fn",
                                    "advisory": "SEC-VULN-004",
                                    "severity": "low"}])

    def test_duplicate_instance_id_rejected(self):
        with self.assertRaises(InputError):
            make_dataset(
                edges=[], modes={"release": ["app.main:run"]},
                affected=[AFFECTED_PARSE],
                instances=INSTANCES + [INSTANCES[0]],
            )

    def test_unknown_resolution_rejected(self):
        with self.assertRaises(InputError):
            make_dataset(
                edges=[{"caller": "a:b", "callee": "c:d",
                        "build_mode": "all", "resolution": "maybe"}],
                modes={"release": ["app.main:run"]},
                affected=[AFFECTED_PARSE],
            )

    def test_unresolved_edge_requires_reason(self):
        with self.assertRaises(InputError):
            make_dataset(
                edges=[{"caller": "a:b", "callee": "c:d",
                        "build_mode": "all", "resolution": "unresolved"}],
                modes={"release": ["app.main:run"]},
                affected=[AFFECTED_PARSE],
            )

    def test_unknown_build_mode_rejected(self):
        with self.assertRaises(InputError):
            make_dataset(
                edges=[edge("a:b", "c:d", build_mode="staging")],
                modes={"release": ["app.main:run"]},
                affected=[AFFECTED_PARSE],
            )


class TestShippedSampleData(unittest.TestCase):
    """随附 data/ 样例的整体结论矩阵。"""

    @classmethod
    def setUpClass(cls):
        cls.report = analyze(load_dataset(DATA_DIR))

    def test_release_matrix(self):
        symbols = self.report["modes"]["release"]["symbols"]
        self.assertEqual(symbols["acme@1.4.0:parse_token"]["status"],
                         "reachable")
        self.assertEqual(symbols["acme@1.4.0:deserialize"]["status"],
                         "unreachable")
        self.assertTrue(self.report["modes"]["release"]["evidence_complete"])

    def test_release_path_goes_through_plugin_cycle(self):
        path = self.report["modes"]["release"]["symbols"][
            "acme@1.4.0:parse_token"]["path"]
        self.assertEqual(path, ["plugin.loader:load",
                                "plugkit@0.9.2:handle",
                                "plugkit@0.9.2:emit",
                                "acme@1.4.0:parse_token"])

    def test_test_mode_matrix(self):
        symbols = self.report["modes"]["test"]["symbols"]
        self.assertEqual(symbols["acme@1.4.0:parse_token"]["status"],
                         "reachable")
        self.assertEqual(symbols["acme@1.4.0:deserialize"]["status"],
                         "reachable")

    def test_dev_mode_matrix(self):
        symbols = self.report["modes"]["dev"]["symbols"]
        for symbol in ("acme@1.4.0:parse_token", "acme@1.4.0:deserialize"):
            self.assertEqual(symbols[symbol]["status"],
                             "insufficient_evidence")
            self.assertTrue(symbols[symbol]["missing_evidence"])

    def test_priority_fixes(self):
        fixes = [(f["entry"], f["symbol"])
                 for f in self.report["priority_fixes"]]
        self.assertEqual(fixes,
                         [("plugin.loader:load", "acme@1.4.0:parse_token")])


if __name__ == "__main__":
    unittest.main()
