"""replica_selection 的本地合成数据测试（unittest，纯标准库）。

注意：按本轮约束，这些测试仅作为源码提交，未实际运行。
"""

import unittest

from replica_selection import (
    ReplicaInfo,
    SelectionStatus,
    TimelineFork,
    TimelineRegistry,
    WriteCredential,
    missing_credentials,
    select_replica,
)


def build_registry() -> TimelineRegistry:
    """构造如下拓扑：

    root ──(fork@50)──> child ──(fork@200)──> grandchild
      └──(fork@80)──> sibling
    """
    return TimelineRegistry(
        [
            TimelineFork("root", None, 0),
            TimelineFork("child", "root", 50),
            TimelineFork("grandchild", "child", 200),
            TimelineFork("sibling", "root", 80),
        ]
    )


class CoverageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = build_registry()

    def test_same_timeline_requires_replay_progress(self) -> None:
        replica = ReplicaInfo(1, "root", replayed_position=100, latency_ms=1.0)
        self.assertEqual(
            missing_credentials(
                self.registry, replica, [WriteCredential("root", 100)]
            ),
            (),
        )
        self.assertEqual(
            missing_credentials(
                self.registry, replica, [WriteCredential("root", 101)]
            ),
            (WriteCredential("root", 101),),
        )

    def test_child_sees_parent_history_up_to_fork_point(self) -> None:
        # 子线在父线 50 处分叉，已重放到 200：
        # 父线 50 处的提交可读。
        replica = ReplicaInfo(1, "child", replayed_position=200, latency_ms=1.0)
        self.assertEqual(
            missing_credentials(
                self.registry, replica, [WriteCredential("root", 50)]
            ),
            (),
        )

    def test_child_cannot_promise_parent_commit_beyond_fork(self) -> None:
        # 同一副本不能承诺父线 51 处的提交。
        replica = ReplicaInfo(1, "child", replayed_position=200, latency_ms=1.0)
        self.assertEqual(
            missing_credentials(
                self.registry, replica, [WriteCredential("root", 51)]
            ),
            (WriteCredential("root", 51),),
        )

    def test_child_replay_lagging_behind_fork_point(self) -> None:
        # 分叉点 50 但只重放到 30：父线 40 处的提交仍不可见。
        replica = ReplicaInfo(1, "child", replayed_position=30, latency_ms=1.0)
        self.assertEqual(
            missing_credentials(
                self.registry, replica, [WriteCredential("root", 40)]
            ),
            (WriteCredential("root", 40),),
        )

    def test_grandchild_inherits_through_two_levels(self) -> None:
        # grandchild 在 child 200 处分叉，child 在 root 50 处分叉：
        # 可见 root<=50、child<=200。
        replica = ReplicaInfo(1, "grandchild", replayed_position=300, latency_ms=1.0)
        self.assertEqual(
            missing_credentials(
                self.registry,
                replica,
                [WriteCredential("root", 50), WriteCredential("child", 200)],
            ),
            (),
        )
        self.assertEqual(
            missing_credentials(
                self.registry,
                replica,
                [WriteCredential("root", 51), WriteCredential("child", 201)],
            ),
            (WriteCredential("root", 51), WriteCredential("child", 201)),
        )

    def test_unrelated_branch_not_visible(self) -> None:
        # sibling 与 child 互不包含：child 的副本看不到 sibling 的提交。
        replica = ReplicaInfo(1, "child", replayed_position=500, latency_ms=1.0)
        self.assertEqual(
            missing_credentials(
                self.registry, replica, [WriteCredential("sibling", 10)]
            ),
            (WriteCredential("sibling", 10),),
        )


class SelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = build_registry()

    def test_prefers_lowest_latency(self) -> None:
        replicas = [
            ReplicaInfo(1, "root", replayed_position=100, latency_ms=5.0),
            ReplicaInfo(2, "root", replayed_position=100, latency_ms=1.0),
        ]
        result = select_replica(
            self.registry, replicas, [WriteCredential("root", 100)]
        )
        self.assertEqual(result.status, SelectionStatus.OK)
        self.assertEqual(result.replica_id, 2)

    def test_tie_break_by_replica_id(self) -> None:
        replicas = [
            ReplicaInfo(7, "root", replayed_position=100, latency_ms=1.0),
            ReplicaInfo(3, "root", replayed_position=100, latency_ms=1.0),
        ]
        result = select_replica(
            self.registry, replicas, [WriteCredential("root", 100)]
        )
        self.assertEqual(result.status, SelectionStatus.OK)
        self.assertEqual(result.replica_id, 3)

    def test_lagging_replica_excluded_from_candidates(self) -> None:
        replicas = [
            ReplicaInfo(1, "root", replayed_position=90, latency_ms=0.1),
            ReplicaInfo(2, "root", replayed_position=100, latency_ms=9.9),
        ]
        result = select_replica(
            self.registry, replicas, [WriteCredential("root", 100)]
        )
        self.assertEqual(result.status, SelectionStatus.OK)
        self.assertEqual(result.replica_id, 2)

    def test_divergent_branches_return_wait_with_per_replica_missing(self) -> None:
        # 凭据分属互不包含的 child / sibling 分支：任何单个副本
        # 都无法同时覆盖，返回等待并列明各副本缺少的凭据。
        replicas = [
            ReplicaInfo(2, "sibling", replayed_position=500, latency_ms=1.0),
            ReplicaInfo(1, "child", replayed_position=500, latency_ms=1.0),
        ]
        creds = [WriteCredential("child", 100), WriteCredential("sibling", 100)]
        result = select_replica(self.registry, replicas, creds)
        self.assertEqual(result.status, SelectionStatus.WAIT)
        self.assertIsNone(result.replica_id)
        self.assertEqual(
            result.missing,
            {
                1: (WriteCredential("sibling", 100),),
                2: (WriteCredential("child", 100),),
            },
        )

    def test_wait_when_replay_not_caught_up(self) -> None:
        replicas = [
            ReplicaInfo(1, "root", replayed_position=40, latency_ms=1.0),
        ]
        result = select_replica(
            self.registry, replicas, [WriteCredential("root", 41)]
        )
        self.assertEqual(result.status, SelectionStatus.WAIT)
        self.assertEqual(
            result.missing, {1: (WriteCredential("root", 41),)}
        )

    def test_no_online_replicas_waits(self) -> None:
        result = select_replica(
            self.registry, [], [WriteCredential("root", 1)]
        )
        self.assertEqual(result.status, SelectionStatus.WAIT)
        self.assertEqual(result.missing, {})

    def test_child_replica_serves_parent_commit_at_fork_point(self) -> None:
        # 题面场景：父线 50 处分叉，子线重放到 200，
        # 父线 50 的读取应路由到子线副本。
        replicas = [
            ReplicaInfo(1, "child", replayed_position=200, latency_ms=2.0),
        ]
        result = select_replica(
            self.registry, replicas, [WriteCredential("root", 50)]
        )
        self.assertEqual(result.status, SelectionStatus.OK)
        self.assertEqual(result.replica_id, 1)

    def test_child_replica_rejects_parent_commit_after_fork_point(self) -> None:
        replicas = [
            ReplicaInfo(1, "child", replayed_position=200, latency_ms=2.0),
        ]
        result = select_replica(
            self.registry, replicas, [WriteCredential("root", 51)]
        )
        self.assertEqual(result.status, SelectionStatus.WAIT)
        self.assertEqual(
            result.missing, {1: (WriteCredential("root", 51),)}
        )


if __name__ == "__main__":
    unittest.main()
