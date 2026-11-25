"""权重/健康/离池/请求事件交错执行的场景测试，以及端到端确定性重放。"""

from __future__ import annotations

import random
import unittest

from selector import (
    EndpointSelector,
    Lease,
    NoEndpointAvailableError,
    RandomByteStream,
)


def u64(value: int) -> bytes:
    return value.to_bytes(8, "big", signed=False)


class InterleavingEventsTests(unittest.TestCase):
    def test_mixed_start_end_health_weight_events(self) -> None:
        selector = EndpointSelector(
            RandomByteStream(b"".join(u64(v) for v in range(64)))
        )
        selector.upsert_endpoint("a", 1)
        selector.upsert_endpoint("b", 1)
        leases: list[Lease] = []

        l1 = selector.start_request()  # 平局按编号 -> a
        self.assertEqual(l1.endpoint, "a")
        leases.append(l1)

        selector.set_weight("a", 4)  # a 在途 1/4=0.25
        l2 = selector.start_request()  # b 在途 0 -> b 更轻
        self.assertEqual(l2.endpoint, "b")
        leases.append(l2)

        selector.set_health("b", False)
        l3 = selector.start_request()  # 仅 a 健康，直选（不耗随机）
        self.assertEqual(l3.endpoint, "a")
        # 前两次双端点选择各消耗 2 个随机数；单端点直选不消耗。
        self.assertEqual(selector.random_stream.offset, 32)
        leases.append(l3)

        selector.set_health("b", True)
        selector.end_request(l2)  # b 在途归零
        l4 = selector.start_request()  # a 在途 2/4=0.5，b 0 -> b
        self.assertEqual(l4.endpoint, "b")
        leases.append(l4)

        self.assertEqual(selector.in_flight_of("a"), 2)
        self.assertEqual(selector.in_flight_of("b"), 1)

        selector.remove_endpoint("b")
        l5 = selector.start_request()  # 仅 a
        self.assertEqual(l5.endpoint, "a")
        leases.append(l5)
        # b 的旧租约 l4 仍可归还。
        selector.end_request(l4)
        leases.remove(l4)

        selector.upsert_endpoint("b", 10)  # 同名新实例
        self.assertEqual(selector.in_flight_of("b"), 0)
        l6 = selector.start_request()  # a 在途 3/4=0.75，b 0 -> b
        self.assertEqual(l6.endpoint, "b")
        leases.append(l6)

        # l2 与 l4 已提前归还，列表中剩余的都是未归还租约。
        leases.remove(l2)
        for lease in leases:
            selector.end_request(lease)
        self.assertEqual(selector.in_flight_of("a"), 0)
        self.assertEqual(selector.in_flight_of("b"), 0)

    def test_empty_pool_burst_between_requests(self) -> None:
        selector = EndpointSelector(RandomByteStream(b"".join(u64(v) for v in range(8))))
        selector.upsert_endpoint("a", 1)
        selector.upsert_endpoint("b", 1)
        lease = selector.start_request()
        selector.remove_endpoint("a")
        selector.remove_endpoint("b")
        with self.assertRaises(NoEndpointAvailableError):
            selector.start_request()
        # 旧租约在全空池上仍可归还。
        selector.end_request(lease)
        # 池恢复后正常服务。
        selector.upsert_endpoint("c", 1)
        new_lease = selector.start_request()
        self.assertEqual(new_lease.endpoint, "c")

    def test_generations_keep_old_and_new_instances_independent(self) -> None:
        selector = EndpointSelector(RandomByteStream(b"".join(u64(v) for v in range(16))))
        selector.upsert_endpoint("x", 2)
        old = [selector.start_request() for _ in range(3)]
        self.assertEqual(selector.in_flight_of("x"), 3)
        selector.remove_endpoint("x")
        selector.upsert_endpoint("x", 2)
        self.assertEqual(selector.in_flight_of("x"), 0)
        new = [selector.start_request() for _ in range(2)]
        self.assertEqual(selector.in_flight_of("x"), 2)
        # 旧实例租约全部归还，不影响新实例。
        for lease in old:
            selector.end_request(lease)
        self.assertEqual(selector.in_flight_of("x"), 2)
        for lease in new:
            selector.end_request(lease)
        self.assertEqual(selector.in_flight_of("x"), 0)
        # 代际编号严格递增。
        self.assertLess(old[0].generation, new[0].generation)


class EndToEndReplayTests(unittest.TestCase):
    def test_identical_byte_stream_reproduces_identical_decisions(self) -> None:
        rng = random.Random(20261126)
        data = b"".join(rng.getrandbits(64).to_bytes(8, "big") for _ in range(256))

        def run() -> tuple[list, int]:
            selector = EndpointSelector(RandomByteStream(data))
            selector.upsert_endpoint("a", 1)
            selector.upsert_endpoint("b", 2)
            selector.upsert_endpoint("c", 3)
            decisions: list = []
            held: list[Lease] = []
            for step in range(60):
                if step % 7 == 3 and held:
                    lease = held.pop(0)
                    selector.end_request(lease)
                    decisions.append(("end", lease.endpoint))
                elif step % 11 == 5:
                    selector.set_health("b", not selector.describe("b")["healthy"])
                    decisions.append(("health-b", selector.describe("b")["healthy"]))
                else:
                    lease = selector.start_request()
                    held.append(lease)
                    decisions.append(("start", lease.endpoint))
            for lease in held:
                selector.end_request(lease)
            return decisions, selector.random_stream.offset

        first_trace, first_offset = run()
        second_trace, second_offset = run()
        self.assertEqual(first_trace, second_trace)
        self.assertEqual(first_offset, second_offset)
        # 确实发生了带随机消耗的选择（偏移 > 0）。
        self.assertGreater(first_offset, 0)

    def test_replay_from_snapshot_with_iterator_source(self) -> None:
        # 迭代器型随机流只能前移：恢复时在全新迭代器流上定位到快照偏移，
        # 重放结果与 bytes 型来源完全一致。
        values = list(range(32))
        data = b"".join(u64(v) for v in values)

        def fresh_iterator_stream() -> RandomByteStream:
            return RandomByteStream(iter(u64(v) for v in values))

        selector = EndpointSelector(fresh_iterator_stream())
        selector.upsert_endpoint("a", 1)
        selector.upsert_endpoint("b", 4)
        leases = [selector.start_request() for _ in range(4)]
        state = selector.snapshot()
        tail = [(l.endpoint, l.request_id) for l in (selector.start_request() for _ in range(3))]

        # 在全新的迭代器流上恢复：restore 内部把流前移到快照偏移。
        restored = EndpointSelector.restore(state, fresh_iterator_stream())
        self.assertEqual(restored.random_stream.offset, state["random_stream"]["offset"])
        # 活动租约随快照恢复，在途负载与原实例一致；直接继续即可逐位重放。
        self.assertEqual(len(restored.snapshot()["active_leases"]), len(leases))
        replayed = [(l.endpoint, l.request_id) for l in (restored.start_request() for _ in range(3))]
        self.assertEqual(replayed, tail)
        self.assertEqual(restored.random_stream.offset, selector.random_stream.offset)
        # 数据来源是迭代器但字节内容一致，偏移与 bytes 型来源相同。
        bytes_selector = EndpointSelector(RandomByteStream(data))
        bytes_selector.upsert_endpoint("a", 1)
        bytes_selector.upsert_endpoint("b", 4)
        [bytes_selector.start_request() for _ in range(4)]
        self.assertEqual(bytes_selector.random_stream.offset, state["random_stream"]["offset"])


if __name__ == "__main__":
    unittest.main()
