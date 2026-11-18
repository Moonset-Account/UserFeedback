"""离线预检模块的验收测试（unittest）。

覆盖：
- 可接受结果：前缀整体公开、经批准分发入口可访问；
- 应被阻止结果：组织显式拒绝覆盖资源放行、继承默认阻止公开、
  未批准分发入口不生效、服务身份放行不开放直连；
- 边界/交错场景：前缀部分公开的范围与依据、条件依赖快照外属性时标记待确认。
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path

from precheck import evaluate_snapshot, load_snapshot
from precheck.__main__ import main as cli_main
from precheck.models import Defaults, Principal, Snapshot, Statement

EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "examples" / "snapshot"


def make_statement(
    sid,
    effect,
    principal,
    resources,
    actions=("s3:GetObject",),
    condition=(),
    source="resource_policy:bucket",
):
    return Statement(
        sid=sid,
        effect=effect,
        principal=principal,
        actions=tuple(actions),
        resources=tuple(resources),
        condition_attributes=tuple(condition),
        source=source,
    )


def make_snapshot(
    prefixes,
    resource_statements=(),
    org_statements=(),
    defaults=None,
):
    return Snapshot(
        defaults=defaults
        or Defaults(
            block_public_access=True,
            overrides={},
            approved_distribution_endpoints=("cdn-dist-1",),
            known_attributes=("aws:PrincipalOrgID",),
        ),
        org_boundary=tuple(org_statements),
        resource_policies=tuple(resource_statements),
        prefixes=tuple(prefixes),
    )


def result_for(report, prefix):
    for entry in report["prefixes"]:
        if entry["prefix"] == prefix:
            return entry
    raise AssertionError(f"报告中缺少前缀 {prefix}")


ANON = Principal(kind="anonymous")
CDN = Principal(kind="service", service="cdn-distribution", endpoint="cdn-dist-1")
CDN_ROGUE = Principal(kind="service", service="cdn-distribution", endpoint="cdn-evil")


class AcceptableResultTests(unittest.TestCase):
    """可接受结果：明确放行且无拒绝时，前缀应对匿名公开。"""

    def test_fully_public_prefix(self):
        defaults = Defaults(
            block_public_access=True,
            overrides={"media-bucket": {"block_public_access": False}},
            approved_distribution_endpoints=("cdn-dist-1",),
            known_attributes=(),
        )
        snap = make_snapshot(
            prefixes=["media-bucket/assets/"],
            resource_statements=[
                make_statement(
                    "anon-read", "Allow", ANON, ["media-bucket/assets/*"]
                )
            ],
            defaults=defaults,
        )
        report = evaluate_snapshot(snap)
        direct = result_for(report, "media-bucket/assets/")["direct"]
        self.assertEqual(direct["status"], "public")
        self.assertTrue(
            any(
                s["scope"] == "media-bucket/assets/" and s["status"] == "allowed"
                for s in direct["scopes"]
            )
        )

    def test_approved_distribution_endpoint_access(self):
        snap = make_snapshot(
            prefixes=["media-bucket/assets/"],
            resource_statements=[
                make_statement(
                    "cdn-read", "Allow", CDN, ["media-bucket/*"]
                )
            ],
        )
        report = evaluate_snapshot(snap)
        dist = result_for(report, "media-bucket/assets/")["distribution"]
        self.assertEqual(dist["status"], "public")


class BlockedResultTests(unittest.TestCase):
    """应被阻止或拒绝的结果。"""

    def test_org_explicit_deny_overrides_resource_allow(self):
        snap = make_snapshot(
            prefixes=["logs-bucket/"],
            org_statements=[
                make_statement(
                    "org-deny",
                    "Deny",
                    ANON,
                    ["logs-bucket/*"],
                    source="org_boundary",
                )
            ],
            resource_statements=[
                make_statement("anon-read", "Allow", ANON, ["logs-bucket/*"])
            ],
            defaults=Defaults(
                block_public_access=False,
                approved_distribution_endpoints=("cdn-dist-1",),
            ),
        )
        report = evaluate_snapshot(snap)
        direct = result_for(report, "logs-bucket/")["direct"]
        self.assertEqual(direct["status"], "denied")
        self.assertTrue(
            all(s["status"] == "denied" for s in direct["scopes"]),
            "组织显式拒绝必须覆盖资源放行",
        )
        self.assertIn("org-deny", direct["scopes"][0]["rationale"])

    def test_service_identity_allow_does_not_open_direct(self):
        snap = make_snapshot(
            prefixes=["media-bucket/assets/"],
            resource_statements=[
                make_statement("cdn-read", "Allow", CDN, ["media-bucket/*"])
            ],
            defaults=Defaults(
                block_public_access=False,
                approved_distribution_endpoints=("cdn-dist-1",),
            ),
        )
        report = evaluate_snapshot(snap)
        entry = result_for(report, "media-bucket/assets/")
        self.assertEqual(
            entry["direct"]["status"],
            "denied",
            "分发服务身份放行不得开放匿名直连",
        )
        self.assertEqual(entry["distribution"]["status"], "public")

    def test_unapproved_distribution_endpoint_ignored(self):
        snap = make_snapshot(
            prefixes=["media-bucket/assets/"],
            resource_statements=[
                make_statement("cdn-rogue", "Allow", CDN_ROGUE, ["media-bucket/*"])
            ],
        )
        report = evaluate_snapshot(snap)
        dist = result_for(report, "media-bucket/assets/")["distribution"]
        self.assertEqual(dist["status"], "denied")
        self.assertIn("不在已批准清单内", dist["summary"])

    def test_inherited_default_blocks_public_access(self):
        snap = make_snapshot(
            prefixes=["data-bucket/pub/"],
            resource_statements=[
                make_statement("anon-read", "Allow", ANON, ["data-bucket/pub/*"])
            ],
            defaults=Defaults(block_public_access=True),
        )
        report = evaluate_snapshot(snap)
        direct = result_for(report, "data-bucket/pub/")["direct"]
        self.assertEqual(direct["status"], "denied")
        self.assertIn("继承设置", direct["scopes"][0]["rationale"])


class BoundaryAndInterleavedTests(unittest.TestCase):
    """边界与交错场景：部分公开、条件待确认、默认覆盖的交错。"""

    def test_partial_public_scope_with_rationale(self):
        defaults = Defaults(
            block_public_access=True,
            overrides={"media-bucket": {"block_public_access": False}},
            approved_distribution_endpoints=(),
            known_attributes=(),
        )
        snap = make_snapshot(
            prefixes=["media-bucket/"],
            resource_statements=[
                make_statement(
                    "anon-read-assets",
                    "Allow",
                    ANON,
                    ["media-bucket/assets/*"],
                )
            ],
            defaults=defaults,
        )
        report = evaluate_snapshot(snap)
        direct = result_for(report, "media-bucket/")["direct"]
        self.assertEqual(direct["status"], "partially_public")
        public_scopes = [s for s in direct["scopes"] if s["status"] == "allowed"]
        self.assertEqual(
            [s["scope"] for s in public_scopes], ["media-bucket/assets/"]
        )
        self.assertIn("anon-read-assets", public_scopes[0]["rationale"])

    def test_condition_on_unknown_attribute_marks_pending(self):
        defaults = Defaults(
            block_public_access=False,
            approved_distribution_endpoints=(),
            known_attributes=("aws:PrincipalOrgID",),
        )
        snap = make_snapshot(
            prefixes=["media-bucket/partner/"],
            resource_statements=[
                make_statement(
                    "ip-allow",
                    "Allow",
                    ANON,
                    ["media-bucket/partner/*"],
                    condition=("aws:SourceIp",),
                )
            ],
            defaults=defaults,
        )
        report = evaluate_snapshot(snap)
        direct = result_for(report, "media-bucket/partner/")["direct"]
        self.assertEqual(direct["status"], "pending")
        self.assertEqual(direct["scopes"][0]["status"], "pending")
        self.assertIn("aws:SourceIp", direct["scopes"][0]["rationale"])

    def test_pending_deny_condition_also_marks_pending(self):
        defaults = Defaults(
            block_public_access=False,
            approved_distribution_endpoints=(),
            known_attributes=(),
        )
        snap = make_snapshot(
            prefixes=["media-bucket/assets/"],
            resource_statements=[
                make_statement(
                    "anon-read", "Allow", ANON, ["media-bucket/assets/*"]
                ),
                make_statement(
                    "maybe-deny",
                    "Deny",
                    ANON,
                    ["media-bucket/assets/*"],
                    condition=("aws:SourceVpce",),
                ),
            ],
            defaults=defaults,
        )
        report = evaluate_snapshot(snap)
        direct = result_for(report, "media-bucket/assets/")["direct"]
        self.assertEqual(direct["status"], "pending")

    def test_org_deny_narrows_partial_public_scope(self):
        """交错场景：资源放行整个前缀，组织拒绝其中一块，其余部分仍公开。"""
        defaults = Defaults(
            block_public_access=False,
            approved_distribution_endpoints=(),
            known_attributes=(),
        )
        snap = make_snapshot(
            prefixes=["media-bucket/assets/"],
            org_statements=[
                make_statement(
                    "org-deny-internal",
                    "Deny",
                    ANON,
                    ["media-bucket/assets/internal/*"],
                    source="org_boundary",
                )
            ],
            resource_statements=[
                make_statement(
                    "anon-read", "Allow", ANON, ["media-bucket/assets/*"]
                )
            ],
            defaults=defaults,
        )
        report = evaluate_snapshot(snap)
        direct = result_for(report, "media-bucket/assets/")["direct"]
        self.assertEqual(direct["status"], "partially_public")
        scope = direct["scopes"][0]
        self.assertEqual(scope["status"], "partially_public")
        self.assertIn("org-deny-internal", scope["rationale"])


class ExampleSnapshotCliTests(unittest.TestCase):
    """以示例快照验证真实入口 python -m precheck 的端到端行为。"""

    def test_example_snapshot_report(self):
        snapshot = load_snapshot(EXAMPLE_DIR)
        report = evaluate_snapshot(snapshot)

        assets = result_for(report, "media-bucket/assets/")
        # 匿名放行仅覆盖 public 子范围 → 部分公开；
        # partner 子范围条件依赖快照外属性 aws:SourceIp → 该范围待确认。
        self.assertEqual(assets["direct"]["status"], "partially_public")
        statuses = {s["scope"]: s["status"] for s in assets["direct"]["scopes"]}
        self.assertEqual(statuses["media-bucket/assets/public/"], "allowed")
        self.assertEqual(statuses["media-bucket/assets/partner/"], "pending")
        # 分发入口已批准 → 可经分发访问。
        self.assertEqual(assets["distribution"]["status"], "public")

        private = result_for(report, "media-bucket/private/")
        # 仅有服务身份放行 → 直连拒绝，分发可访问。
        self.assertEqual(private["direct"]["status"], "denied")
        self.assertEqual(private["distribution"]["status"], "public")

        logs = result_for(report, "logs-bucket/")
        # 组织显式拒绝覆盖资源放行，且继承默认也阻止。
        self.assertEqual(logs["direct"]["status"], "denied")

    def test_cli_outputs_json_report(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli_main([str(EXAMPLE_DIR)])
        self.assertEqual(code, 0)
        report = json.loads(buf.getvalue())
        self.assertEqual(len(report["prefixes"]), 3)


if __name__ == "__main__":
    unittest.main()
