"""lock_review 验收测试：验证题面交叉约束。

覆盖：
* 可接受结果 —— 互斥运维模式里的逆序获取不报；同一入口读锁共存不报。
* 应被拒绝结果 —— 同一配置里可并发且相互等待的写锁环必须报出。
* 边界/交错 —— 取消路径释放锁后继续检查其他路径；无共同配置组合
  时不报；环上某锁读-读共存时不报；报告须含处置阶段与持有关系。
"""

import json
import os
import tempfile
import unittest

from lock_review import load_spec, review
from lock_review.__main__ import main as cli_main


def make_spec(configs=None, exclusive_groups=None, streams=()):
    return load_spec({
        "configs": configs or [{"id": "cfg-all", "enables": ["realtime", "manual"]}],
        "exclusive_groups": exclusive_groups or [],
        "streams": list(streams),
    })


def stream(sid, phase, mode, conditions, steps):
    return {
        "id": sid,
        "phase": phase,
        "mode": mode,
        "conditions": conditions,
        "steps": steps,
    }


def acq(lock, mode="write"):
    return {"op": "acquire", "lock": lock, "mode": mode}


def rel(lock):
    return {"op": "release", "lock": lock}


def cancel(*locks):
    return {"op": "cancel", "releases": list(locks)}


class TestBlockingCycleReported(unittest.TestCase):
    """应被阻止/拒绝的结果：同配置并发 + 相互等待 + 写冲突。"""

    def test_quarantine_recovery_cycle_is_reported(self):
        spec = make_spec(streams=[
            stream("stop-object", "quarantine", "stop-mode", ["realtime"], [
                acq("obj-table"), acq("quarantine-log"),
                rel("quarantine-log"), rel("obj-table"),
            ]),
            stream("recover-object", "recovery", "recover-mode", ["realtime"], [
                acq("quarantine-log"), acq("obj-table"),
                rel("obj-table"), rel("quarantine-log"),
            ]),
        ])
        report = review(spec)
        self.assertEqual(len(report["findings"]), 1)
        finding = report["findings"][0]
        # 输出涉及的处置阶段
        self.assertEqual(finding["phases"], ["quarantine", "recovery"])
        self.assertEqual(sorted(finding["locks"]), ["obj-table", "quarantine-log"])
        self.assertEqual(finding["configs"], ["cfg-all"])
        # 输出持有关系：谁持有什么、等待什么
        holds = {(h["stream"], h["holds"], h["waits_for"]) for h in finding["holds"]}
        self.assertIn(("stop-object", "obj-table", "quarantine-log"), holds)
        self.assertIn(("recover-object", "quarantine-log", "obj-table"), holds)

    def test_three_lock_cycle_is_reported(self):
        steps_ab = [acq("a"), acq("b"), rel("b"), rel("a")]
        steps_bc = [acq("b"), acq("c"), rel("c"), rel("b")]
        steps_ca = [acq("c"), acq("a"), rel("a"), rel("c")]
        spec = make_spec(streams=[
            stream("s1", "quarantine", "m1", ["realtime"], steps_ab),
            stream("s2", "recovery", "m2", ["realtime"], steps_bc),
            stream("s3", "cleanup", "m3", ["realtime"], steps_ca),
        ])
        report = review(spec)
        self.assertEqual(len(report["findings"]), 1)
        self.assertEqual(report["findings"][0]["phases"],
                         ["cleanup", "quarantine", "recovery"])


class TestAcceptableResultsNotReported(unittest.TestCase):
    """可接受结果：互斥模式逆序、读锁共存均不误报。"""

    def test_reverse_order_in_mutually_exclusive_modes_not_reported(self):
        spec = make_spec(
            exclusive_groups=[["scan-mode", "restore-mode"]],
            streams=[
                stream("scan-stop", "quarantine", "scan-mode", ["realtime"], [
                    acq("obj-table"), acq("quarantine-log"),
                    rel("quarantine-log"), rel("obj-table"),
                ]),
                stream("restore-stop", "recovery", "restore-mode", ["realtime"], [
                    acq("quarantine-log"), acq("obj-table"),
                    rel("obj-table"), rel("quarantine-log"),
                ]),
            ],
        )
        report = review(spec)
        self.assertEqual(report["findings"], [])
        # 环确实存在但被互斥约束过滤
        self.assertEqual(report["stats"]["cycles_checked"], 1)

    def test_read_locks_coexisting_at_same_entry_not_reported(self):
        spec = make_spec(streams=[
            stream("enum-objects", "quarantine", "query-mode", ["realtime"], [
                acq("obj-table", "read"), acq("quarantine-log", "read"),
                rel("quarantine-log"), rel("obj-table"),
            ]),
            stream("audit-log", "quarantine", "query-mode", ["realtime"], [
                acq("quarantine-log", "read"), acq("obj-table", "read"),
                rel("obj-table"), rel("quarantine-log"),
            ]),
        ])
        report = review(spec)
        self.assertEqual(report["findings"], [])

    def test_no_cycle_no_finding(self):
        spec = make_spec(streams=[
            stream("s1", "quarantine", "m1", ["realtime"], [
                acq("a"), acq("b"), rel("b"), rel("a"),
            ]),
            stream("s2", "recovery", "m2", ["realtime"], [
                acq("a"), acq("b"), rel("b"), rel("a"),
            ]),
        ])
        self.assertEqual(review(spec)["findings"], [])


class TestBoundaryAndInterleaved(unittest.TestCase):
    """边界/交错场景：取消路径、配置交集、读-读边界。"""

    def test_cancel_path_releases_locks_other_paths_still_checked(self):
        # stop-object 在取消路径上释放全部锁，但未取消路径仍持有并等待，
        # 与 recover-object 构成环 —— 取消路径不得抑制该发现。
        spec = make_spec(streams=[
            stream("stop-object", "quarantine", "stop-mode", ["realtime"], [
                acq("obj-table"), acq("quarantine-log"),
                cancel("obj-table", "quarantine-log"),
                rel("quarantine-log"), rel("obj-table"),
            ]),
            stream("recover-object", "recovery", "recover-mode", ["realtime"], [
                acq("quarantine-log"), acq("obj-table"),
                rel("obj-table"), rel("quarantine-log"),
            ]),
        ])
        report = review(spec)
        self.assertEqual(len(report["findings"]), 1)
        self.assertEqual(report["findings"][0]["phases"],
                         ["quarantine", "recovery"])

    def test_cancel_before_second_acquire_breaks_only_that_path(self):
        # 取消路径释放 obj-table 后该路径终止；未取消路径不再申请新锁，
        # 因此不存在 obj-table -> quarantine-log 的持有-等待边，不报。
        spec = make_spec(streams=[
            stream("stop-object", "quarantine", "stop-mode", ["realtime"], [
                acq("obj-table"),
                cancel("obj-table"),
                rel("obj-table"),
            ]),
            stream("recover-object", "recovery", "recover-mode", ["realtime"], [
                acq("quarantine-log"), acq("obj-table"),
                rel("obj-table"), rel("quarantine-log"),
            ]),
        ])
        self.assertEqual(review(spec)["findings"], [])

    def test_no_common_config_combination_not_reported(self):
        # 两条流各自只在不同配置组合中启用，无共同配置 -> 不并发，不报。
        spec = make_spec(
            configs=[
                {"id": "cfg-realtime", "enables": ["realtime"]},
                {"id": "cfg-manual", "enables": ["manual"]},
            ],
            streams=[
                stream("stop-object", "quarantine", "stop-mode", ["realtime"], [
                    acq("obj-table"), acq("quarantine-log"),
                    rel("quarantine-log"), rel("obj-table"),
                ]),
                stream("recover-object", "recovery", "recover-mode", ["manual"], [
                    acq("quarantine-log"), acq("obj-table"),
                    rel("obj-table"), rel("quarantine-log"),
                ]),
            ],
        )
        report = review(spec)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["stats"]["cycles_checked"], 1)

    def test_read_read_boundary_on_one_lock_clears_cycle(self):
        # obj-table 两侧均为读（读-读共存），即使另一把锁有写，环也不成立。
        spec = make_spec(streams=[
            stream("stop-object", "quarantine", "stop-mode", ["realtime"], [
                acq("obj-table", "read"), acq("quarantine-log", "write"),
                rel("quarantine-log"), rel("obj-table"),
            ]),
            stream("recover-object", "recovery", "recover-mode", ["realtime"], [
                acq("quarantine-log", "write"), acq("obj-table", "read"),
                rel("obj-table"), rel("quarantine-log"),
            ]),
        ])
        self.assertEqual(review(spec)["findings"], [])

    def test_write_on_one_side_of_each_lock_is_enough(self):
        # 边界另一侧：obj-table 一写一读、quarantine-log 一写一读 -> 仍冲突。
        spec = make_spec(streams=[
            stream("stop-object", "quarantine", "stop-mode", ["realtime"], [
                acq("obj-table", "write"), acq("quarantine-log", "read"),
                rel("quarantine-log"), rel("obj-table"),
            ]),
            stream("recover-object", "recovery", "recover-mode", ["realtime"], [
                acq("quarantine-log", "write"), acq("obj-table", "read"),
                rel("obj-table"), rel("quarantine-log"),
            ]),
        ])
        report = review(spec)
        self.assertEqual(len(report["findings"]), 1)


class TestCliEntry(unittest.TestCase):
    """实际入口：python -m lock_review <spec.json>。"""

    def _run_cli(self, doc):
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            return cli_main([path])
        finally:
            os.unlink(path)

    def test_cli_exit_code_reflects_findings(self):
        blocking = {
            "configs": [{"id": "cfg-all", "enables": ["realtime"]}],
            "streams": [
                stream("stop-object", "quarantine", "stop-mode", ["realtime"], [
                    acq("obj-table"), acq("quarantine-log"),
                    rel("quarantine-log"), rel("obj-table"),
                ]),
                stream("recover-object", "recovery", "recover-mode", ["realtime"], [
                    acq("quarantine-log"), acq("obj-table"),
                    rel("obj-table"), rel("quarantine-log"),
                ]),
            ],
        }
        clean = {
            "configs": [{"id": "cfg-all", "enables": ["realtime"]}],
            "streams": [
                stream("stop-object", "quarantine", "stop-mode", ["realtime"], [
                    acq("obj-table"), rel("obj-table"),
                ]),
            ],
        }
        self.assertEqual(self._run_cli(blocking), 1)
        self.assertEqual(self._run_cli(clean), 0)


if __name__ == "__main__":
    unittest.main()
