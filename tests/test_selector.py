"""选择器核心规则测试：抽样、负载比较、健康、离池/重入、异常路径。"""

from __future__ import annotations

import unittest

from selector import (
    DuplicateLeaseError,
    EndpointSelector,
    InvalidLeaseError,
    InvalidWeightError,
    Lease,
    NoEndpointAvailableError,
    RandomByteStream,
    RandomnessExhaustedError,
    UnknownEndpointError,
)


def u64(value: int) -> bytes:
    return value.to_bytes(8, "big", signed=False)


def make_selector(*values: int) -> EndpointSelector:
    return EndpointSelector(RandomByteStream(b"".join(u64(v) for v in values)))


class EmptyAndSinglePoolTests(unittest.TestCase):
    def test_empty_pool_raises_without_touching_stream(self) -> None:
        selector = make_selector(0, 0)
        with self.assertRaises(NoEndpointAvailableError):
            selector.start_request()
        self.assertEqual(selector.random_stream.offset, 0)

    def test_all_unhealthy_is_empty_pool(self) -> None:
        selector = make_selector(0)
        selector.upsert_endpoint("a", 1, healthy=False)
        with self.assertRaises(NoEndpointAvailableError):
            selector.start_request()
        self.assertEqual(selector.random_stream.offset, 0)

    def test_single_healthy_endpoint_chosen_directly(self) -> None:
        selector = make_selector(0)  # 这个随机数不应被消耗
        selector.upsert_endpoint("solo", 3)
        lease = selector.start_request()
        self.assertEqual(lease.endpoint, "solo")
        self.assertEqual(selector.random_stream.offset, 0)
        self.assertEqual(selector.in_flight_of("solo"), 1)

    def test_recovery_after_pool_refills(self) -> None:
        selector = make_selector(0)
        with self.assertRaises(NoEndpointAvailableError):
            selector.start_request()
        selector.upsert_endpoint("a", 1)
        lease = selector.start_request()
        self.assertEqual(lease.endpoint, "a")


class TwoCandidateSelectionTests(unittest.TestCase):
    def test_lower_in_flight_to_weight_ratio_wins(self) -> None:
        selector = make_selector(*range(12))
        selector.upsert_endpoint("a", 1)  # 在途 0
        selector.upsert_endpoint("b", 3)  # 在途 0，初始平局，按编号 a 胜
        lease = selector.start_request()
        self.assertEqual(lease.endpoint, "a")
        selector.end_request(lease)

        # 制造负载差：
        # 第二次选择时 a 在途 1（比率 1/1=1），b 在途 0（比率 0），b 更轻。
        l1 = selector.start_request()
        self.assertEqual(l1.endpoint, "a")
        l2 = selector.start_request()
        self.assertEqual(l2.endpoint, "b")
        # 再来一次：a 比率 1，b 比率 1/3 -> b 更轻。
        l3 = selector.start_request()
        self.assertEqual(l3.endpoint, "b")
        selector.end_request(l1)
        selector.end_request(l2)
        selector.end_request(l3)
        self.assertEqual(selector.in_flight_of("a"), 0)
        self.assertEqual(selector.in_flight_of("b"), 0)

    def test_tie_broken_by_endpoint_id_strings(self) -> None:
        selector = make_selector(0, 0)
        selector.upsert_endpoint("zeta", 2)
        selector.upsert_endpoint("alpha", 2)
        # 两候选都会被抽出，负载同为 0，编号小者 alpha 胜。
        lease = selector.start_request()
        self.assertEqual(lease.endpoint, "alpha")

    def test_tie_broken_by_endpoint_id_integers(self) -> None:
        selector = make_selector(0, 0)
        selector.upsert_endpoint(10, 1)
        selector.upsert_endpoint(2, 1)
        lease = selector.start_request()
        self.assertEqual(lease.endpoint, 2)

    def test_weight_dominates_same_in_flight(self) -> None:
        # a: 在途 1 权重 1 -> 比率 1；b: 在途 1 权重 4 -> 比率 0.25，b 胜。
        selector = make_selector(*range(12))
        selector.upsert_endpoint("a", 1)
        selector.upsert_endpoint("b", 4)
        first = selector.start_request()  # 初始平局按编号 -> a
        self.assertEqual(first.endpoint, "a")
        second = selector.start_request()  # a 比率 1，b 比率 0 -> b
        self.assertEqual(second.endpoint, "b")
        third = selector.start_request()  # a 比率 1，b 比率 0.25 -> b
        self.assertEqual(third.endpoint, "b")

    def test_two_draws_consume_two_random_numbers(self) -> None:
        selector = make_selector(11, 22)
        selector.upsert_endpoint("a", 1)
        selector.upsert_endpoint("b", 1)
        selector.start_request()
        self.assertEqual(selector.random_stream.offset, 16)

    def test_weighted_sampling_without_replacement_distinct_pair(self) -> None:
        # 三端点，随机数决定两次加权抽中的端点，二者必须不同。
        selector = make_selector(0, 0)
        selector.upsert_endpoint("a", 1)
        selector.upsert_endpoint("b", 1)
        selector.upsert_endpoint("c", 1)
        observed: set[str] = set()
        # 直接观察内部两轮抽样结果。
        candidates = [(n, r) for n, r in selector._endpoints.items()]
        first_name, _ = selector._weighted_draw(candidates)
        remaining = [item for item in candidates if item[0] != first_name]
        second_name, _ = selector._weighted_draw(remaining)
        observed.update((first_name, second_name))
        self.assertEqual(len(observed), 2)


class HealthAndWeightEventsTests(unittest.TestCase):
    def test_unhealthy_endpoint_excluded(self) -> None:
        selector = make_selector(0, 0)
        selector.upsert_endpoint("a", 1)
        selector.upsert_endpoint("b", 1)
        selector.set_health("a", False)
        # 仅剩 b 健康 -> 直选，不耗随机。
        lease = selector.start_request()
        self.assertEqual(lease.endpoint, "b")
        self.assertEqual(selector.random_stream.offset, 0)
        self.assertEqual(selector.healthy_endpoints, ["b"])

    def test_health_toggle_restores_candidate(self) -> None:
        selector = make_selector(0, 0)
        selector.upsert_endpoint("a", 1)
        selector.upsert_endpoint("b", 1)
        selector.set_health("a", False)
        selector.set_health("a", True)
        lease = selector.start_request()
        self.assertIn(lease.endpoint, {"a", "b"})
        # 两个健康端点：两轮无放回抽样各消耗一个随机数。
        self.assertEqual(selector.random_stream.offset, 16)

    def test_weight_update_takes_effect_immediately(self) -> None:
        selector = make_selector(0)
        selector.upsert_endpoint("a", 1)
        selector.upsert_endpoint("b", 1)
        selector.set_weight("b", 100)
        # 阈值 0 落盘到 a（累计权重首个区间）——这里验证权重已被读取。
        candidates = [(n, r) for n, r in selector._endpoints.items()]
        name, _ = selector._weighted_draw(candidates)
        self.assertEqual(name, "a")
        self.assertEqual(selector.describe("b")["weight"], 100)

    def test_invalid_weights_rejected(self) -> None:
        selector = make_selector()
        for bad in (0, -1, 1.5, True, "3"):
            with self.assertRaises(InvalidWeightError):
                selector.upsert_endpoint("x", bad)  # type: ignore[arg-type]
        selector.upsert_endpoint("a", 1)
        with self.assertRaises(InvalidWeightError):
            selector.set_weight("a", 0)

    def test_unknown_endpoint_operations_raise(self) -> None:
        selector = make_selector()
        with self.assertRaises(UnknownEndpointError):
            selector.set_health("ghost", True)
        with self.assertRaises(UnknownEndpointError):
            selector.set_weight("ghost", 2)
        with self.assertRaises(UnknownEndpointError):
            selector.remove_endpoint("ghost")
        with self.assertRaises(UnknownEndpointError):
            selector.in_flight_of("ghost")

    def test_upsert_existing_keeps_in_flight(self) -> None:
        selector = make_selector(0)
        selector.upsert_endpoint("a", 1)
        lease = selector.start_request()
        self.assertEqual(selector.in_flight_of("a"), 1)
        selector.upsert_endpoint("a", 5)  # 仍在池：只改权重，不重置在途
        self.assertEqual(selector.in_flight_of("a"), 1)
        self.assertEqual(selector.describe("a")["weight"], 5)
        selector.end_request(lease)


class RemovalAndReadmissionTests(unittest.TestCase):
    def test_removed_endpoint_excluded_but_lease_returnable(self) -> None:
        selector = make_selector(0, 0)
        selector.upsert_endpoint("a", 1)
        selector.upsert_endpoint("b", 1)
        lease_a = selector.start_request()  # 初始平局 -> a
        self.assertEqual(lease_a.endpoint, "a")
        selector.remove_endpoint("a")
        self.assertEqual(selector.healthy_endpoints, ["b"])
        # 旧租约仍可归还，不抛异常。
        selector.end_request(lease_a)
        # 只剩 b，直选。
        lease_b = selector.start_request()
        self.assertEqual(lease_b.endpoint, "b")

    def test_readmitted_same_name_starts_at_zero_in_flight(self) -> None:
        selector = make_selector(0, 0)
        selector.upsert_endpoint("a", 1)
        old_lease = selector.start_request()
        self.assertEqual(selector.in_flight_of("a"), 1)
        old_generation = old_lease.generation
        selector.remove_endpoint("a")
        selector.upsert_endpoint("a", 1)
        self.assertEqual(selector.in_flight_of("a"), 0)
        new_lease = selector.start_request()
        self.assertNotEqual(new_lease.generation, old_generation)
        # 旧实例的未归还租约仍可归还，且不影响新实例的在途数。
        selector.end_request(old_lease)
        self.assertEqual(selector.in_flight_of("a"), 1)
        selector.end_request(new_lease)
        self.assertEqual(selector.in_flight_of("a"), 0)

    def test_end_unknown_and_duplicate_lease_raise(self) -> None:
        selector = make_selector(0)
        selector.upsert_endpoint("a", 1)
        lease = selector.start_request()
        selector.end_request(lease)
        with self.assertRaises(DuplicateLeaseError):
            selector.end_request(lease)
        with self.assertRaises(InvalidLeaseError):
            selector.end_request(Lease(endpoint="a", generation=999, request_id=42))
        with self.assertRaises(InvalidLeaseError):
            selector.end_request("not-a-lease")  # type: ignore[arg-type]

    def test_lease_ids_are_unique_and_monotonic(self) -> None:
        selector = make_selector(0)
        selector.upsert_endpoint("a", 1)
        ids = [selector.start_request().request_id for _ in range(5)]
        self.assertEqual(ids, list(range(5)))
        self.assertEqual(len(set(ids)), 5)


class RandomnessBoundaryTests(unittest.TestCase):
    def test_exhausted_stream_surfaces_error(self) -> None:
        selector = EndpointSelector(RandomByteStream(b""))
        selector.upsert_endpoint("a", 1)
        selector.upsert_endpoint("b", 1)
        with self.assertRaises(RandomnessExhaustedError):
            selector.start_request()

    def test_partial_uint64_surfaces_error(self) -> None:
        selector = EndpointSelector(RandomByteStream(b"\x00" * 3))
        selector.upsert_endpoint("a", 1)
        selector.upsert_endpoint("b", 1)
        with self.assertRaises(RandomnessExhaustedError):
            selector.start_request()

    def test_constructor_rejects_non_stream(self) -> None:
        with self.assertRaises(TypeError):
            EndpointSelector(b"raw-bytes")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
