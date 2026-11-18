"""基线引擎验收测试（unittest）。

覆盖题面交叉约束：
* 可接受结果：配置与运行现场一致 -> compliant；
* 应阻止/拒绝结果：配置命中禁止高权限身份 -> noncompliant；
* 待整改：配置符合预期但进程身份/写入现场不符 -> pending_remediation；
* 无运行快照 -> no_snapshot 单列；
* 覆盖层 merge / replace（显式清除上层列表）/ remove 交错语义；
* 启动别名按最终定义判定（含别名链上的覆盖与快照匹配）；
* 写入范围边界：等于允许路径合规、兄弟目录越界；
* 不符合项来源 config / runtime 的归属与分桶优先级；
* 别名成环、未知引用等输入拒绝；
* CLI 真实执行与报告产物。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest

from baseline_engine.cli import main as cli_main
from baseline_engine.engine import BaselineConfigError, BaselineEngine, evaluate
from baseline_engine.models import (
    ListOp,
    Overlay,
    PlatformDefaults,
    ProcessSnapshot,
    ServiceDef,
    SOURCE_CONFIG,
    SOURCE_RUNTIME,
    STATUS_COMPLIANT,
    STATUS_NONCOMPLIANT,
    STATUS_NO_SNAPSHOT,
    STATUS_REMEDIATE,
    STATUS_SKIPPED,
)


def make_platform() -> PlatformDefaults:
    return PlatformDefaults(
        user="svcplat",
        group="svcgrp",
        write_paths=["/var/app/shared"],
        forbidden_identities=["root"],
        low_privilege_required=True,
    )


def find(findings, check, source):
    return [f for f in findings if f.check == check and f.source == source]


class CompliantBaselineTests(unittest.TestCase):
    def test_acceptable_result_is_compliant(self):
        """配置与进程实测身份完全一致 -> 可接受结果。"""
        defs = [
            ServiceDef(
                name="collector",
                user="svc_collector",
                group="svcgrp",
                write_paths=["/var/app/collector"],
            )
        ]
        snapshots = [
            ProcessSnapshot(
                service="collector",
                user="svc_collector",
                group="svcgrp",
                actual_write_paths=["/var/app/collector/data/2026.log"],
                pid=4100,
            )
        ]
        report = BaselineEngine(defs, [], make_platform(), snapshots).evaluate()

        self.assertEqual(report.summary["total"], 1)
        svc = report.services[0]
        self.assertEqual(svc.status, STATUS_COMPLIANT)
        self.assertEqual(svc.findings, [])
        self.assertTrue(svc.has_snapshot)

    def test_skipped_service_not_checked(self):
        """未要求低权限运行的服务跳过核对，不产生不符合项。"""
        defs = [
            ServiceDef(
                name="housekeeping",
                user="root",
                low_privilege_required=False,
            )
        ]
        report = BaselineEngine(defs, [], make_platform(), []).evaluate()
        svc = report.services[0]
        self.assertEqual(svc.status, STATUS_SKIPPED)
        self.assertFalse(svc.low_privilege_required)
        self.assertEqual(svc.findings, [])


class BlockedAndRemediationTests(unittest.TestCase):
    def test_configured_root_is_blocked_as_noncompliant(self):
        """配置本身要求 root 运行 -> 应阻止，不符合项归属 config。"""
        defs = [
            ServiceDef(
                name="badagent",
                user="root",
                write_paths=["/var/app/badagent"],
            )
        ]
        snapshots = [
            ProcessSnapshot(
                service="badagent",
                user="root",
                group="root",
                actual_write_paths=["/var/app/badagent/x"],
            )
        ]
        report = BaselineEngine(defs, [], make_platform(), snapshots).evaluate()
        svc = report.services[0]
        self.assertEqual(svc.status, STATUS_NONCOMPLIANT)
        inheritance = find(svc.findings, "privilege_inheritance", SOURCE_CONFIG)
        self.assertEqual(len(inheritance), 1)
        self.assertEqual(inheritance[0].actual, "root")
        # 运行现场同样命中高权限身份时，runtime 项也要保留
        runtime_inh = find(svc.findings, "privilege_inheritance", SOURCE_RUNTIME)
        self.assertEqual(len(runtime_inh), 1)

    def test_runtime_user_mismatch_goes_to_pending_remediation(self):
        """配置符合预期但进程身份不同 -> 待整改，来源 runtime。"""
        defs = [
            ServiceDef(
                name="collector",
                user="svc_collector",
                group="svcgrp",
                write_paths=["/var/app/collector"],
            )
        ]
        snapshots = [
            ProcessSnapshot(
                service="collector",
                user="daemon",  # 与配置不一致
                group="svcgrp",
                actual_write_paths=["/var/app/collector/run.log"],
            )
        ]
        report = BaselineEngine(defs, [], make_platform(), snapshots).evaluate()
        svc = report.services[0]
        self.assertEqual(svc.status, STATUS_REMEDIATE)
        identity = find(svc.findings, "startup_identity", SOURCE_RUNTIME)
        self.assertEqual(len(identity), 1)
        self.assertEqual(identity[0].expected, "svc_collector")
        self.assertEqual(identity[0].actual, "daemon")
        # 配置侧不应有任何不符合项
        self.assertEqual(find(svc.findings, "startup_identity", SOURCE_CONFIG), [])

    def test_runtime_write_out_of_scope_is_remediation(self):
        """进程写入越界 -> 待整改，并指出违规路径。"""
        defs = [
            ServiceDef(
                name="collector",
                user="svc_collector",
                write_paths=["/var/app/collector"],
            )
        ]
        snapshots = [
            ProcessSnapshot(
                service="collector",
                user="svc_collector",
                actual_write_paths=["/etc/collector/cron.conf"],
            )
        ]
        report = BaselineEngine(defs, [], make_platform(), snapshots).evaluate()
        svc = report.services[0]
        self.assertEqual(svc.status, STATUS_REMEDIATE)
        scope = find(svc.findings, "write_scope", SOURCE_RUNTIME)
        self.assertEqual(len(scope), 1)
        self.assertEqual(scope[0].detail["violating_path"], "/etc/collector/cron.conf")

    def test_no_snapshot_isolated_bucket_keeps_config_findings(self):
        """无运行快照单列；若配置自身有缺陷仍需在报告中指出。"""
        defs = [
            # replace 显式清除上层写入路径 -> 写入范围不受约束（config 缺陷）
            ServiceDef(
                name="ghost",
                user="svc_ghost",
                write_paths=ListOp(ListOp.REPLACE, []),
            ),
            ServiceDef(
                name="healthy",
                user="svc_healthy",
                write_paths=["/var/app/healthy"],
            ),
        ]
        report = BaselineEngine(defs, [], make_platform(), []).evaluate()
        no_snapshot = report.by_status(STATUS_NO_SNAPSHOT)
        self.assertEqual({s.name for s in no_snapshot}, {"ghost", "healthy"})
        ghost = next(s for s in no_snapshot if s.name == "ghost")
        self.assertFalse(ghost.has_snapshot)
        # 平台默认写入路径被空的服务层列表 merge 后仍为空 -> 配置缺陷保留
        scope_cfg = find(ghost.findings, "write_scope", SOURCE_CONFIG)
        self.assertEqual(len(scope_cfg), 1)


class OverlaySemanticsTests(unittest.TestCase):
    def test_replace_op_clears_upper_layer_list(self):
        """覆盖层 replace 显式清除上层写入路径列表。"""
        defs = [
            ServiceDef(
                name="worker",
                user="svc_worker",
                write_paths=["/var/app/worker"],
            )
        ]
        overlays = [
            Overlay(
                target="worker",
                write_paths=ListOp(ListOp.REPLACE, []),
            )
        ]
        report = BaselineEngine(defs, overlays, make_platform(), []).evaluate()
        svc = report.services[0]
        self.assertEqual(svc.resolved["write_paths"], [])
        self.assertEqual(svc.status, STATUS_NO_SNAPSHOT)
        self.assertEqual(
            len(find(svc.findings, "write_scope", SOURCE_CONFIG)), 1
        )

    def test_merge_remove_and_replace_interleaved(self):
        """merge / remove / replace 多覆盖层按输入顺序交错生效。"""
        defs = [
            ServiceDef(
                name="worker",
                user="svc_worker",
                write_paths=["/var/app/worker"],
            )
        ]
        overlays = [
            # 合并去重（重复元素只保留一个）
            Overlay(
                target="worker",
                write_paths=ListOp(
                    ListOp.MERGE, ["/var/app/worker", "/data/worker"]
                ),
            ),
            # 剔除平台/上层中的某个元素
            Overlay(target="worker", write_paths=ListOp(ListOp.REMOVE, ["/data/worker"])),
            # 最终显式清除并替换
            Overlay(
                target="worker",
                write_paths=ListOp(ListOp.REPLACE, ["/srv/worker"]),
            ),
        ]
        report = BaselineEngine(defs, overlays, make_platform(), []).evaluate()
        svc = report.services[0]
        self.assertEqual(svc.resolved["write_paths"], ["/srv/worker"])
        # 禁止身份列表也验证 merge 去重：平台 root + 服务层 root 不重复
        self.assertEqual(svc.resolved["forbidden_identities"], ["root"])

    def test_overlay_dict_form_is_accepted(self):
        """JSON 风格的 {"op": ..., "items": ...} 覆盖项可被解析。"""
        raw = {
            "platform_defaults": {"forbidden_identities": ["root"]},
            "service_defs": [
                {
                    "name": "worker",
                    "user": "svc_worker",
                    "write_paths": {"op": "merge", "items": ["/var/app/worker"]},
                }
            ],
            "overlays": [
                {
                    "target": "worker",
                    "write_paths": {"op": "replace", "items": ["/srv/worker"]},
                }
            ],
            "snapshots": [],
        }
        report = evaluate(raw)
        svc = report.services[0]
        self.assertEqual(svc.resolved["write_paths"], ["/srv/worker"])


class AliasResolutionTests(unittest.TestCase):
    def test_alias_uses_final_definition(self):
        """启动别名引用的服务按最终定义判定，快照可用别名上报。"""
        defs = [
            ServiceDef(
                name="collector",
                user="svc_collector",
                group="svcgrp",
                write_paths=["/var/app/collector"],
                aliases=["collector-prod"],
            ),
            ServiceDef(
                name="collector-shim",
                alias_of="collector",
                # 别名定义层追加自己的写入路径
                write_paths=["/var/app/shim"],
            ),
        ]
        snapshots = [
            ProcessSnapshot(
                service="collector-shim",  # 以别名上报
                user="svc_collector",
                group="svcgrp",
                actual_write_paths=[
                    "/var/app/collector/a.log",
                    "/var/app/shim/b.log",
                ],
            )
        ]
        report = BaselineEngine(defs, [], make_platform(), snapshots).evaluate()
        # 别名不单独成报，只输出一个规范服务
        self.assertEqual(report.summary["total"], 1)
        svc = report.services[0]
        self.assertEqual(svc.canonical_name, "collector")
        self.assertEqual(svc.snapshot_service, "collector-shim")
        self.assertIn("collector-prod", svc.aliases)
        self.assertIn("collector-shim", svc.aliases)
        self.assertEqual(
            sorted(svc.resolved["write_paths"]),
            sorted(
                [
                    "/var/app/shared",  # 平台默认层保留
                    "/var/app/collector",
                    "/var/app/shim",
                ]
            ),
        )
        self.assertEqual(svc.status, STATUS_COMPLIANT)

    def test_overlay_targeting_alias_affects_final_definition(self):
        """定向到别名的覆盖层最终作用于规范服务。"""
        defs = [
            ServiceDef(
                name="collector",
                user="svc_collector",
                write_paths=["/var/app/collector"],
            ),
            ServiceDef(name="collector-legacy", alias_of="collector"),
        ]
        overlays = [
            Overlay(target="collector-legacy", user="svc_new"),
        ]
        snapshots = [
            ProcessSnapshot(
                service="collector",
                user="svc_new",
                actual_write_paths=["/var/app/collector/x"],
            )
        ]
        report = BaselineEngine(defs, overlays, make_platform(), snapshots).evaluate()
        svc = report.services[0]
        self.assertEqual(svc.resolved["user"], "svc_new")
        self.assertEqual(svc.status, STATUS_COMPLIANT)

    def test_alias_cycle_is_rejected(self):
        """别名链成环必须被拒绝。"""
        defs = [
            ServiceDef(name="a", alias_of="b"),
            ServiceDef(name="b", alias_of="a"),
        ]
        engine = BaselineEngine(defs, [], make_platform(), [])
        with self.assertRaises(BaselineConfigError):
            engine.evaluate()

    def test_unknown_overlay_and_snapshot_rejected(self):
        """覆盖层或快照引用未知服务必须被拒绝。"""
        defs = [ServiceDef(name="real", user="svc_real")]
        with self.assertRaises(BaselineConfigError):
            BaselineEngine(
                defs, [Overlay(target="ghost")], make_platform(), []
            ).evaluate()
        with self.assertRaises(BaselineConfigError):
            BaselineEngine(
                defs,
                [],
                make_platform(),
                [ProcessSnapshot(service="ghost", user="x")],
            ).evaluate()


class WriteScopeBoundaryTests(unittest.TestCase):
    def test_equal_path_allowed_sibling_rejected(self):
        """边界：等于允许路径合规；同前缀兄弟目录必须判越界。"""
        defs = [
            ServiceDef(
                name="edge",
                user="svc_edge",
                write_paths=["/var/app/edge"],
            )
        ]
        # 恰好等于允许路径 -> 合规
        ok_snapshot = ProcessSnapshot(
            service="edge",
            user="svc_edge",
            actual_write_paths=["/var/app/edge"],
        )
        ok_report = BaselineEngine(defs, [], make_platform(), [ok_snapshot]).evaluate()
        self.assertEqual(ok_report.services[0].status, STATUS_COMPLIANT)

        # /var/app/edge-extra 是兄弟路径，不得被前缀匹配放行
        bad_snapshot = ProcessSnapshot(
            service="edge",
            user="svc_edge",
            actual_write_paths=["/var/app/edge-extra/escape.log"],
        )
        bad_report = BaselineEngine(
            defs, [], make_platform(), [bad_snapshot]
        ).evaluate()
        self.assertEqual(bad_report.services[0].status, STATUS_REMEDIATE)
        scope = find(bad_report.services[0].findings, "write_scope", SOURCE_RUNTIME)
        self.assertEqual(len(scope), 1)


class SourceAttributionTests(unittest.TestCase):
    def test_config_and_runtime_findings_both_reported_bucket_priority(self):
        """同时存在 config 与 runtime 问题时归入 noncompliant，但两类来源都保留。"""
        defs = [
            ServiceDef(
                name="mixed",
                user="root",  # config 缺陷：禁止身份
                write_paths=["/var/app/mixed"],
            )
        ]
        snapshots = [
            ProcessSnapshot(
                service="mixed",
                user="daemon",  # runtime 缺陷：与配置也不一致
                actual_write_paths=["/tmp/mixed.sock"],  # runtime 越界
            )
        ]
        report = BaselineEngine(defs, [], make_platform(), snapshots).evaluate()
        svc = report.services[0]
        self.assertEqual(svc.status, STATUS_NONCOMPLIANT)
        sources = {f.source for f in svc.findings}
        self.assertEqual(sources, {SOURCE_CONFIG, SOURCE_RUNTIME})
        self.assertTrue(
            any(f.check == "write_scope" and f.source == SOURCE_RUNTIME
                for f in svc.findings)
        )

    def test_missing_configured_user_is_config_finding(self):
        """平台与服务都未声明启动用户且无快照 -> no_snapshot 桶内保留 config 不符合项。"""
        platform = PlatformDefaults(
            user=None,
            group=None,
            write_paths=["/var/app/shared"],
            forbidden_identities=["root"],
        )
        defs = [ServiceDef(name="nobody", write_paths=["/var/app/nobody"])]
        report = BaselineEngine(defs, [], platform, []).evaluate()
        svc = report.services[0]
        self.assertEqual(svc.status, STATUS_NO_SNAPSHOT)
        identity = find(svc.findings, "startup_identity", SOURCE_CONFIG)
        self.assertEqual(len(identity), 1)


class CliEndToEndTests(unittest.TestCase):
    def _write_input(self, directory: str) -> str:
        payload = {
            "platform_defaults": {
                "user": "svcplat",
                "group": "svcgrp",
                "write_paths": ["/var/app/shared"],
                "forbidden_identities": ["root"],
            },
            "service_defs": [
                {
                    "name": "collector",
                    "user": "svc_collector",
                    "write_paths": ["/var/app/collector"],
                    "aliases": ["collector-prod"],
                },
                {
                    "name": "legacy-collector",
                    "alias_of": "collector",
                },
            ],
            "overlays": [
                {
                    "target": "legacy-collector",
                    "write_paths": {
                        "op": "replace",
                        "items": ["/srv/collector"],
                    },
                }
            ],
            "snapshots": [
                {
                    "service": "collector-prod",
                    "user": "svc_collector",
                    "group": "svcgrp",
                    "actual_write_paths": ["/srv/collector/run.log"],
                }
            ],
        }
        path = os.path.join(directory, "input.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        return path

    def test_cli_produces_real_report_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = self._write_input(tmp)
            output_path = os.path.join(tmp, "report.json")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = cli_main(["--input", input_path, "--output", output_path])
            self.assertEqual(code, 0)
            self.assertTrue(os.path.exists(output_path))
            with open(output_path, "r", encoding="utf-8") as fh:
                report = json.load(fh)
            self.assertEqual(report["summary"]["total"], 1)
            svc = report["services"][0]
            self.assertEqual(svc["canonical_name"], "collector")
            self.assertEqual(svc["status"], STATUS_COMPLIANT)
            # replace 已清除上层写入路径
            self.assertEqual(svc["resolved"]["write_paths"], ["/srv/collector"])
            self.assertIn("collector-prod", svc["aliases"])
            self.assertIn("评估完成", stderr.getvalue())

    def test_cli_rejects_bad_input_with_exit_code_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = {
                "service_defs": [{"name": "a", "alias_of": "b"}],
            }
            input_path = os.path.join(tmp, "bad.json")
            with open(input_path, "w", encoding="utf-8") as fh:
                json.dump(bad, fh)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = cli_main(["--input", input_path])
            self.assertEqual(code, 2)
            self.assertIn("输入配置错误", stderr.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
