"""端到端：加载 JSON 夹具，跑引擎，校验结构化与文本报告、CLI 退出码。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

try:
    from tests._helpers import base_config, node
except ImportError:  # python -I 下 cwd 不在 sys.path
    from _helpers import base_config, node

from region_preflight import PreflightEngine, load_config
from region_preflight.cli import main as cli_main
from region_preflight.report import render_text, to_dict

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample_pipeline.json"


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.config = load_config(FIXTURE)
        self.result = PreflightEngine(self.config).run()

    def test_sample_pipeline_fails_due_to_unapproved_audit_and_overseas(self):
        # order: cn-north 已核准（warning），eu-central 未核准（error）。
        # audit: 两个异地落地都未核准（error）。
        self.assertFalse(self.result.passed)
        error_regions = {
            (f.data_type, f.node_id)
            for f in self.result.findings
            if f.severity == "error"
        }
        self.assertIn(("order", "dr_replica_overseas"), error_regions)
        self.assertIn(("audit", "cache_dr"), error_regions)
        self.assertIn(("audit", "dr_replica_overseas"), error_regions)

    def test_order_report_region_classification(self):
        order = self.result.reports["order"]
        self.assertEqual(
            order.approved_regions, ["cn-east", "cn-north"]
        )
        self.assertEqual(
            order.persistable_regions,
            ["cn-east", "cn-north", "eu-central"],
        )
        # us-west 只出现转发节点 dr_relay。
        self.assertEqual(order.transit_only_regions, ["us-west"])
        self.assertIn("us-west", order.transitable_regions)

    def test_cross_region_landing_paths_and_conditions_for_order(self):
        order = self.result.reports["order"]
        by_node = {landing.node_id: landing for landing in order.cross_region_landings}
        self.assertEqual(set(by_node), {"cache_dr", "dr_replica_overseas"})

        cache_dr = by_node["cache_dr"]
        self.assertEqual(
            cache_dr.path, ["entry", "cache", "cache_dr"]
        )
        self.assertEqual(cache_dr.conditions, ["cache[cache_down]"])
        self.assertTrue(cache_dr.approved)

        overseas = by_node["dr_replica_overseas"]
        self.assertEqual(
            overseas.path,
            ["entry", "cache", "async_logger", "dr_replica", "dr_replica_overseas"],
        )
        self.assertEqual(
            overseas.conditions, ["dr_replica[regional_outage]"]
        )
        self.assertFalse(overseas.approved)

    def test_transit_records_keep_failover_flag_and_path(self):
        order = self.result.reports["order"]
        relay = next(
            r for r in order.transit_records if r.node_id == "dr_relay"
        )
        self.assertTrue(relay.on_failover)
        self.assertEqual(relay.region, "us-west")
        self.assertEqual(relay.path, ["entry", "cache", "cache_dr", "dr_relay"])
        entry = next(
            r for r in order.transit_records if r.node_id == "entry"
        )
        self.assertFalse(entry.on_failover)

    def test_to_dict_is_json_serializable(self):
        payload = to_dict(self.result)
        encoded = json.dumps(payload, ensure_ascii=False)
        decoded = json.loads(encoded)
        self.assertFalse(decoded["passed"])
        landing = decoded["reports"]["order"]["cross_region_landings"][0]
        self.assertIn("trigger_conditions", landing)
        self.assertIn("path", landing)

    def test_render_text_contains_key_sections(self):
        text = render_text(self.result)
        self.assertIn("发布地域预检报告：fixture-pipeline", text)
        self.assertIn("[数据类型] order", text)
        self.assertIn("[数据类型] audit", text)
        self.assertIn("异地回退落地", text)
        self.assertIn("cache[cache_down]", text)
        self.assertIn("dr_replica[regional_outage]", text)
        self.assertIn("仅转发节点", text)
        self.assertIn("不通过", text)

    def test_cli_json_exit_code_one_on_error(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main([str(FIXTURE), "--json"])
        self.assertEqual(code, 1)
        payload = json.loads(buf.getvalue())
        self.assertFalse(payload["passed"])

    def test_cli_text_exit_code_zero_when_passing(self):
        # 构造一份全部本区、全部核准的临时配置，CLI 应返回 0。
        passing = base_config(
            node(
                "entry", "forward", region="cn-east",
                next=node("cache", "cache_write", region="cn-east"),
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "passing.json"
            path.write_text(json.dumps(passing), encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cli_main([str(path)])
            self.assertEqual(code, 0)
            self.assertIn("通过", buf.getvalue())

    def test_cli_returns_two_on_bad_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps({"name": "x"}), encoding="utf-8")
            code = cli_main([str(path)])
            self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
