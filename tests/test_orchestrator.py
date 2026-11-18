"""RebootOrchestrator 验收用例。

测试矩阵（交叉约束）
--------------------
可接受路径：
    test_happy_path_accept          回执 -> 下发 -> 全进程上报 -> 生效
    test_reserve_then_dispatch      先预留名额再下发，预留占容量
    test_quorum_exact_boundary      健康副本恰好等于要求值时允许下发
    test_timeout_frees_slot         超时释放名额后同主机/他机可重新下发
被阻止/拒绝路径：
    test_dispatch_blocked_without_receipt   无回执不得下发
    test_dispatch_blocked_by_capacity       容量满载必须阻止
    test_dispatch_blocked_by_quorum         健康副本不足必须阻止
    test_stale_receipt_rejected             旧批次迟到回执不能进入新批次
    test_wrong_batch_report_rejected        旧批次迟到上报不能完成新批次
    test_wrong_version_keeps_pending        版本不匹配不能生效
    test_unknown_host_rejected              未知主机操作必须拒绝
边界/交错：
    test_interleaved_capacity_quorum_timeout 多主机交错：容量、副本、超时联动
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import (  # noqa: E402
    HostState,
    InstallReceipt,
    Outcome,
    RebootOrchestrator,
    RejectReason,
    ServiceSpec,
    VersionReport,
)


class FakeClock:
    """可控时钟，便于确定性地构造超时场景。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def build_cluster():
    """构建标准 4 主机 / 2 服务集群。

    * api 服务（最低 2 健康副本）：h1, h2, h3 各一个进程
    * db  服务（最低 2 健康副本）：h2, h3, h4 各一个进程
    重启容量为 2。
    """
    clock = FakeClock()
    orch = RebootOrchestrator(
        reboot_capacity=2,
        services=[ServiceSpec("api", 2), ServiceSpec("db", 2)],
        now_func=clock,
    )
    layout = {
        "h1": [("api", "api-proc-h1")],
        "h2": [("api", "api-proc-h2"), ("db", "db-proc-h2")],
        "h3": [("api", "api-proc-h3"), ("db", "db-proc-h3")],
        "h4": [("db", "db-proc-h4")],
    }
    for host_id, replicas in layout.items():
        for service_name, process_name in replicas:
            orch.add_replica(host_id, service_name, process_name)
    return orch, clock


def receipt(host_id, batch_id, version="v2"):
    return InstallReceipt(host_id, batch_id, version, installed_at=0.0)


def report(host_id, batch_id, process_name, version="v2"):
    return VersionReport(host_id, batch_id, process_name, version, reported_at=0.0)


class RebootOrchestratorTests(unittest.TestCase):
    # ------------------------------------------------------------------ #
    # 可接受结果
    # ------------------------------------------------------------------ #
    def test_happy_path_accept(self):
        orch, clock = build_cluster()
        batch = orch.begin_batch("B1")

        # 非当前批次的回执按迟到回执拒绝
        self.assertFalse(orch.submit_receipt(receipt("h1", "B0")).accepted)
        self.assertTrue(orch.submit_receipt(receipt("h1", batch)).accepted)
        self.assertEqual(orch.get_state("h1"), HostState.AWAITING_DISPATCH.value)

        out = orch.dispatch("h1", timeout_seconds=60)
        self.assertTrue(out.accepted, out)
        self.assertEqual(orch.get_state("h1"), HostState.REBOOTING.value)
        self.assertEqual(orch.held_slots(), 1)

        # 进程上报加载新版本 -> 待确认；全部进程上报 -> 生效并释放名额
        self.assertTrue(orch.report_version(
            report("h1", batch, "api-proc-h1")).accepted)
        self.assertEqual(orch.get_state("h1"), HostState.EFFECTIVE.value)
        self.assertEqual(orch.effective_hosts(), ["h1"])
        self.assertEqual(orch.held_slots(), 0)

    def test_reserve_then_dispatch(self):
        orch, clock = build_cluster()
        batch = orch.begin_batch("B1")
        for h in ("h1", "h4"):
            self.assertTrue(orch.submit_receipt(receipt(h, batch)).accepted)

        self.assertTrue(orch.reserve_slot("h1").accepted)
        self.assertEqual(orch.get_state("h1"), HostState.RESERVED.value)
        self.assertEqual(orch.held_slots(), 1)

        # 第二个名额被 h4 占满，新预留必须拒绝
        self.assertTrue(orch.reserve_slot("h4").accepted)
        h2 = orch.submit_receipt(receipt("h2", batch))
        self.assertTrue(h2.accepted)
        self.assertEqual(
            orch.reserve_slot("h2").reason, RejectReason.CAPACITY_FULL.value)

        # 预留转下发，不再额外占名额
        out = orch.dispatch("h1", timeout_seconds=60)
        self.assertTrue(out.accepted)
        self.assertEqual(orch.held_slots(), 2)

    def test_quorum_exact_boundary(self):
        orch, clock = build_cluster()
        batch = orch.begin_batch("B1")
        # h1 已在重启（仅承载 api），api 剩余健康 = 2，恰好等于要求值
        orch.submit_receipt(receipt("h1", batch))
        self.assertTrue(orch.dispatch("h1", 60).accepted)

        # h2 同时承载 api/db：重启后 api 剩余 h3 一份 < 2，必须阻止
        orch.submit_receipt(receipt("h2", batch))
        blocked = orch.dispatch("h2", 60)
        self.assertFalse(blocked.accepted)
        self.assertEqual(blocked.reason, RejectReason.QUORUM_BREACH.value)
        self.assertEqual(orch.get_state("h2"), HostState.AWAITING_DISPATCH.value)

        # h4 仅承载 db，db 当前 3 健康、扣除 h4 后为 2，恰好达标 -> 允许
        orch.submit_receipt(receipt("h4", batch))
        boundary = orch.dispatch("h4", 60)
        self.assertTrue(boundary.accepted, boundary)
        self.assertEqual(orch.healthy_replica_count("db"), 2)

    def test_timeout_frees_slot(self):
        orch, clock = build_cluster()
        batch = orch.begin_batch("B1")
        orch.submit_receipt(receipt("h1", batch))
        self.assertTrue(orch.dispatch("h1", timeout_seconds=60).accepted)
        self.assertEqual(orch.held_slots(), 1)

        clock.advance(61)
        expired = orch.expire_timeouts()
        self.assertEqual(expired, ["h1"])
        self.assertEqual(orch.get_state("h1"),
                         HostState.PENDING_CONFIRMATION.value)
        self.assertEqual(orch.held_slots(), 0)

        # 名额释放后可对同一主机重新下发
        again = orch.dispatch("h1", timeout_seconds=60)
        self.assertTrue(again.accepted)
        self.assertEqual(orch.get_state("h1"), HostState.REBOOTING.value)

    # ------------------------------------------------------------------ #
    # 被阻止 / 拒绝
    # ------------------------------------------------------------------ #
    def test_dispatch_blocked_without_receipt(self):
        orch, _ = build_cluster()
        orch.begin_batch("B1")
        out = orch.dispatch("h1", timeout_seconds=60)
        self.assertFalse(out.accepted)
        self.assertEqual(out.reason, RejectReason.NO_RECEIPT.value)
        self.assertEqual(orch.held_slots(), 0)

    def test_dispatch_blocked_by_capacity(self):
        orch, clock = build_cluster()
        batch = orch.begin_batch("B1")
        # 选不冲突副本下限的两台先占满容量：h1(api) + h4(db)
        orch.submit_receipt(receipt("h1", batch))
        orch.submit_receipt(receipt("h4", batch))
        self.assertTrue(orch.dispatch("h1", 60).accepted)
        self.assertTrue(orch.dispatch("h4", 60).accepted)
        self.assertEqual(orch.held_slots(), 2)

        orch.submit_receipt(receipt("h2", batch))
        out = orch.dispatch("h2", 60)
        self.assertFalse(out.accepted)
        self.assertEqual(out.reason, RejectReason.CAPACITY_FULL.value)
        self.assertEqual(orch.get_state("h2"), HostState.AWAITING_DISPATCH.value)

    def test_dispatch_blocked_by_quorum(self):
        orch, clock = build_cluster()
        batch = orch.begin_batch("B1")
        # h2、h3 同时承载 api/db；h3 重启后两服务都只剩 2 健康（含 h2），
        # 此时再下发 h2 会使两服务都只剩 1 健康 < 2。
        orch.submit_receipt(receipt("h3", batch))
        self.assertTrue(orch.dispatch("h3", 60).accepted)

        orch.submit_receipt(receipt("h2", batch))
        out = orch.dispatch("h2", 60)
        self.assertFalse(out.accepted)
        self.assertEqual(out.reason, RejectReason.QUORUM_BREACH.value)
        self.assertEqual(orch.held_slots(), 1)  # 未下发不得占名额

    def test_stale_receipt_rejected(self):
        orch, clock = build_cluster()
        b1 = orch.begin_batch("B1")
        orch.submit_receipt(receipt("h1", b1))
        orch.dispatch("h1", 60)
        orch.report_version(report("h1", b1, "api-proc-h1"))
        self.assertEqual(orch.effective_hosts(), ["h1"])

        # 新批次开始：旧批次 B1 的迟到回执不能完成 B2
        b2 = orch.begin_batch("B2")
        self.assertEqual(orch.get_state("h1"), HostState.NO_RECEIPT.value)
        stale = orch.submit_receipt(receipt("h1", b1, "v2"))
        self.assertFalse(stale.accepted)
        self.assertEqual(stale.reason, RejectReason.STALE_RECEIPT.value)
        self.assertEqual(orch.get_state("h1"), HostState.NO_RECEIPT.value)

        # 当前批次回执仍可正常进入
        fresh = orch.submit_receipt(receipt("h1", b2, "v3"))
        self.assertTrue(fresh.accepted)
        self.assertEqual(orch.get_state("h1"),
                         HostState.AWAITING_DISPATCH.value)

    def test_wrong_batch_report_rejected(self):
        orch, clock = build_cluster()
        b1 = orch.begin_batch("B1")
        orch.submit_receipt(receipt("h1", b1, "v2"))
        orch.dispatch("h1", 60)

        # 进程侧仍带着旧批次号上报 -> 拒绝，不能完成（新）批次
        b2 = orch.begin_batch("B2")
        orch.submit_receipt(receipt("h1", b2, "v3"))
        self.assertTrue(orch.dispatch("h1", 60).accepted)

        stale_report = orch.report_version(
            report("h1", b1, "api-proc-h1", "v3"))
        self.assertFalse(stale_report.accepted)
        self.assertEqual(stale_report.reason, RejectReason.WRONG_BATCH.value)
        self.assertNotEqual(orch.get_state("h1"),
                            HostState.EFFECTIVE.value)

        # 带当前批次号与目标版本的上报才能生效
        ok = orch.report_version(
            report("h1", b2, "api-proc-h1", "v3"))
        self.assertTrue(ok.accepted)
        self.assertEqual(orch.get_state("h1"), HostState.EFFECTIVE.value)

    def test_wrong_version_keeps_pending(self):
        orch, clock = build_cluster()
        batch = orch.begin_batch("B1")
        orch.submit_receipt(receipt("h2", batch, "v2"))
        self.assertTrue(orch.dispatch("h2", 60).accepted)

        bad = orch.report_version(
            report("h2", batch, "api-proc-h2", "v1-old"))
        self.assertFalse(bad.accepted)
        self.assertEqual(bad.reason, RejectReason.VERSION_MISMATCH.value)
        self.assertEqual(orch.get_state("h2"), HostState.REBOOTING.value)

        # h2 有两个进程，只上报一个也不能生效
        orch.report_version(report("h2", batch, "api-proc-h2", "v2"))
        self.assertEqual(orch.get_state("h2"),
                         HostState.PENDING_CONFIRMATION.value)
        orch.report_version(report("h2", batch, "db-proc-h2", "v2"))
        self.assertEqual(orch.get_state("h2"), HostState.EFFECTIVE.value)

    def test_unknown_host_rejected(self):
        orch, _ = build_cluster()
        orch.begin_batch("B1")
        r = orch.submit_receipt(receipt("ghost", "B1"))
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, RejectReason.UNKNOWN_HOST.value)
        self.assertFalse(orch.dispatch("ghost", 60).accepted)
        self.assertFalse(orch.reserve_slot("ghost").accepted)

    # ------------------------------------------------------------------ #
    # 边界 / 交错场景
    # ------------------------------------------------------------------ #
    def test_interleaved_capacity_quorum_timeout(self):
        orch, clock = build_cluster()
        batch = orch.begin_batch("B1")
        for h in ("h1", "h2", "h4"):
            orch.submit_receipt(receipt(h, batch))

        # 1) h1 下发占 1 名额；api 健康 3->2（恰达标）
        self.assertTrue(orch.dispatch("h1", 30).accepted)

        # 2) h2 下发同时撞两条约束：容量还剩 1（够），但 api 会降到 1（不足）
        blocked = orch.dispatch("h2", 30)
        self.assertEqual(blocked.reason, RejectReason.QUORUM_BREACH.value)

        # 3) h4 仅承载 db，下发后 db 健康 3->2（恰达标），名额占满；
        #    给 h4 更长超时，使后续只有 h1 到期，构造交错回收
        self.assertTrue(orch.dispatch("h4", 90).accepted)
        self.assertEqual(orch.held_slots(), 2)

        # 4) h1 超时：名额释放、状态保持待确认
        clock.advance(31)
        self.assertEqual(orch.expire_timeouts(), ["h1"])
        self.assertEqual(orch.get_state("h1"),
                         HostState.PENDING_CONFIRMATION.value)
        self.assertEqual(orch.held_slots(), 1)

        # h4 已完成版本确认并释放名额，集群副本恢复后 h2 才具备下发条件
        self.assertTrue(orch.report_version(
            report("h4", batch, "db-proc-h4")).accepted)
        self.assertEqual(orch.held_slots(), 0)

        ok = orch.dispatch("h2", 30)
        self.assertTrue(ok.accepted, ok)
        self.assertEqual(orch.get_state("h2"), HostState.REBOOTING.value)
        self.assertEqual(orch.held_slots(), 1)

        # 5) h1 迟到的版本上报仍可在待确认阶段生效
        self.assertTrue(orch.report_version(
            report("h1", batch, "api-proc-h1")).accepted)
        self.assertEqual(orch.effective_hosts(), ["h1", "h4"])

        # 6) h2 需要两个进程全部上报才生效，交错中先单进程不会误判
        self.assertTrue(orch.report_version(
            report("h2", batch, "api-proc-h2")).accepted)
        self.assertEqual(orch.effective_hosts(), ["h1", "h4"])
        self.assertTrue(orch.report_version(
            report("h2", batch, "db-proc-h2")).accepted)
        self.assertEqual(orch.effective_hosts(), ["h1", "h2", "h4"])

    def test_event_log_records_decisions(self):
        orch, clock = build_cluster()
        batch = orch.begin_batch("B1")
        orch.submit_receipt(receipt("h1", batch))
        orch.dispatch("h1", 60)
        kinds = [e["event"] for e in orch.events]
        self.assertIn("BATCH_BEGIN", kinds)
        self.assertIn("REBOOT_DISPATCHED", kinds)

        # 被阻止的下发也要留痕，便于审计：h1 重启后再下发 h2 会击穿 api 副本下限
        orch.submit_receipt(receipt("h2", batch))
        blocked = orch.dispatch("h2", 60)
        self.assertFalse(blocked.accepted)
        self.assertIn("DISPATCH_REJECTED",
                      [e["event"] for e in orch.events])


if __name__ == "__main__":
    unittest.main(verbosity=2)
