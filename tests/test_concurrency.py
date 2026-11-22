"""多线程交错场景：贡献并发登记、回放去重与故障并发。"""

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tensor_coord import (  # noqa: E402
    InvalidViewError,
    PhysicalStorage,
    TrainingCoordinator,
    View,
)


class ConcurrentContributionTest(unittest.TestCase):
    def setUp(self):
        # 12 个物理位置，初值取 0，便于直接按动量核对
        self.storage = PhysicalStorage([0.0] * 12)
        self.coord = TrainingCoordinator(self.storage, momentum=0.5)
        # 两个重叠切片视图
        self.view_a = View(self.storage, shape=(2, 3), strides=(3, 1), offset=0)
        self.view_b = View(self.storage, shape=(2, 3), strides=(3, 1), offset=4)

    def test_concurrent_distinct_contributions_aggregate_exactly_once(self):
        n_threads = 6
        barrier = threading.Barrier(n_threads)
        errors = []

        def worker(tid):
            try:
                barrier.wait()  # 尽量让所有线程同时进入登记阶段
                # 每个线程写两份贡献（两个重叠视图各一份），ID 全局唯一
                grad = [[tid * 6 + 1, tid * 6 + 2, tid * 6 + 3],
                        [tid * 6 + 4, tid * 6 + 5, tid * 6 + 6]]
                self.coord.add_contribution(
                    self.view_a, grad, f"A-{tid}"
                )
                self.coord.add_contribution(
                    self.view_b, grad, f"B-{tid}"
                )
            except BaseException as exc:  # pragma: no cover - 测试失败路径
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,))
                   for t in range(n_threads)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])

        # 手算期望：位置 p 的聚合梯度
        # A 覆盖 0..5，B 覆盖 4..9，梯度元素 = tid*6 + (元素序号+1)
        expected = {}
        for tid in range(n_threads):
            grad_flat = [tid * 6 + k for k in range(1, 7)]
            for element, physical in enumerate(self.view_a.resolve()):
                expected[physical] = expected.get(physical, 0.0) + grad_flat[element]
            for element, physical in enumerate(self.view_b.resolve()):
                expected[physical] = expected.get(physical, 0.0) + grad_flat[element]

        self.assertEqual(self.coord.pending_aggregation(), expected)
        self.assertEqual(len(expected), 10)  # 0..9，位置 10/11 不参与

        result = self.coord.commit_step(0.25)
        self.assertFalse(result.aborted)
        self.assertEqual(len(result.accepted_contribution_ids), 2 * n_threads)
        self.assertEqual(result.touched_positions, tuple(range(10)))

        # momentum=0.5、初动量 0 -> 首步动量等于聚合梯度
        for position, value in expected.items():
            self.assertAlmostEqual(result.momentum_after[position], float(value))
        # 未参与位置动量保持 0、权重保持 0
        self.assertEqual(self.coord.momentum[10:], [0.0, 0.0])
        self.assertEqual(self.storage.buffer[10:], [0.0, 0.0])

    def test_concurrent_replay_of_same_ids_never_double_counts(self):
        n_threads = 8
        barrier = threading.Barrier(n_threads)
        accepted = []
        duplicates = []
        lock = threading.Lock()

        def worker(tid):
            barrier.wait()
            # 所有线程抢同一组 4 个 ID，且各自给出不同 payload：
            # 贡献 xk 的最后一个逻辑元素额外加 k（0..3），其余元素为 tid
            for index, cid in enumerate(("x1", "x2", "x3", "x4")):
                ok = self.coord.add_contribution(
                    self.view_a,
                    [[tid, tid, tid], [tid, tid, tid + index]],
                    cid,
                )
                with lock:
                    (accepted if ok else duplicates).append(cid)

        threads = [threading.Thread(target=worker, args=(t,))
                   for t in range(n_threads)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(accepted), ["x1", "x2", "x3", "x4"])
        self.assertEqual(len(duplicates), 4 * (n_threads - 1))

        result = self.coord.commit_step(0.5)
        self.assertFalse(result.aborted)
        self.assertEqual(
            sorted(result.accepted_contribution_ids),
            ["x1", "x2", "x3", "x4"],
        )
        self.assertEqual(len(result.duplicate_contribution_ids),
                         4 * (n_threads - 1))

        # 每个物理位置只被四个胜出贡献各更新一次。胜出线程可以重复
        # （同一线程可能赢得多个 ID），因此用与胜出线程无关的不变量核对：
        #   贡献 xk 第 e 个元素 = tid_k + (e == 5 时 k 否则 0)
        # 聚合到位置 e（A 视图为恒等映射），记 S = sum(tid_k)：
        #   g[0..4] = S，g[5] = S + (0+1+2+3) = S + 6
        gradients = result.aggregated_gradients
        self.assertEqual(sorted(gradients), list(range(6)))
        s_value = gradients[0]
        for position in range(5):
            self.assertEqual(gradients[position], s_value)
        self.assertEqual(gradients[5] - gradients[0], 6.0)
        # S 是 4 个可重复线程号之和，范围 [0, 4*(n_threads-1)]
        self.assertGreaterEqual(s_value, 0.0)
        self.assertLessEqual(s_value, 4.0 * (n_threads - 1))
        self.assertEqual(s_value % 1.0, 0.0)

    def test_concurrent_invalid_contribution_aborts_and_recovers(self):
        n_threads = 4
        barrier = threading.Barrier(n_threads)
        bad_view = View(self.storage, shape=(2, 3), strides=(99, 1))
        saw_error = []

        def worker(tid):
            barrier.wait()
            try:
                if tid == 0:
                    self.coord.add_contribution(
                        bad_view, [[0] * 3, [0] * 3], "bad"
                    )
                else:
                    grad = [[1, 1, 1], [1, 1, 1]]
                    self.coord.add_contribution(
                        self.view_a, grad, f"good-{tid}"
                    )
            except InvalidViewError:
                saw_error.append(tid)

        threads = [threading.Thread(target=worker, args=(t,))
                   for t in range(n_threads)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(saw_error, [0])
        result = self.coord.commit_step(0.5)
        # 无论合法贡献先登记还是非法贡献先登记，整步都必须作废
        self.assertTrue(result.aborted)
        self.assertEqual(self.storage.buffer, [0.0] * 12)
        self.assertEqual(self.coord.momentum, [0.0] * 12)

        # 恢复：下一步全合法输入正常落盘
        self.assertTrue(
            self.coord.add_contribution(
                self.view_a, [[2, 2, 2], [2, 2, 2]], "recover"
            )
        )
        ok = self.coord.commit_step(0.5)
        self.assertFalse(ok.aborted)
        self.assertEqual(
            self.storage.buffer[:6], [-1.0] * 6
        )
        self.assertEqual(self.storage.buffer[6:], [0.0] * 6)

    def test_repeated_concurrent_runs_are_stable(self):
        # 同样的并发压力重复多轮，捕捉低概率交错缺陷
        for run in range(20):
            storage = PhysicalStorage([0.0] * 6)
            coord = TrainingCoordinator(storage, momentum=0.0)
            view = View(storage, shape=(2, 3), strides=(3, 1))
            n_threads = 5
            barrier = threading.Barrier(n_threads)

            def worker(tid):
                barrier.wait()
                coord.add_contribution(
                    view,
                    [[1, 1, 1], [1, 1, 1]],
                    f"run{run}-t{tid}",
                )

            threads = [threading.Thread(target=worker, args=(t,))
                       for t in range(n_threads)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            result = coord.commit_step(1.0)
            self.assertFalse(result.aborted)
            # 5 份全 1 贡献在 6 个位置各聚合为 5；momentum=0 首步
            self.assertEqual(
                result.aggregated_gradients, {p: 5.0 for p in range(6)}
            )
            self.assertEqual(storage.buffer, [-5.0] * 6)


if __name__ == "__main__":
    unittest.main()
