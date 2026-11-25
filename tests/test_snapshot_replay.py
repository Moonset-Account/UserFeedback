"""快照保存与从快照重放的测试，重点验证随机流消耗位置一致。"""

from __future__ import annotations

import json
import unittest

from selector import EndpointSelector, Lease, RandomByteStream

MOD = 1 << 64
MAX_U64 = MOD - 1


def u64(value: int) -> bytes:
    return value.to_bytes(8, "big", signed=False)


def replay_script(selector: EndpointSelector) -> list:
    """在选择器上执行一段固定事件脚本，返回可比较的结果轨迹。"""
    trace: list = []
    selector.upsert_endpoint("a", 2)
    selector.upsert_endpoint("b", 3)
    selector.upsert_endpoint("c", 5)
    leases: list[Lease] = []
    for _ in range(6):
        lease = selector.start_request()
        leases.append(lease)
        trace.append(("start", lease.endpoint, lease.request_id))
    trace.append(("in_flight", selector.in_flight_of("a"),
                  selector.in_flight_of("b"), selector.in_flight_of("c")))
    selector.end_request(leases[0])
    selector.end_request(leases[3])
    selector.set_health("b", False)
    lease_d = selector.start_request()  # b 不健康，候选 a/c
    trace.append(("start", lease_d.endpoint, lease_d.request_id))
    leases.append(lease_d)
    selector.set_health("b", True)
    selector.remove_endpoint("c")
    lease_e = selector.start_request()  # 候选 a/b
    trace.append(("start", lease_e.endpoint, lease_e.request_id))
    # 离池的 c 上若有旧租约，归还仍应成功。
    for lease in leases:
        if lease.endpoint == "c":
            selector.end_request(lease)
            trace.append(("released-c", lease.generation))
            break
    selector.upsert_endpoint("c", 4)  # 同名新实例，在途从 0 开始
    trace.append(("new-c-in-flight", selector.in_flight_of("c")))
    lease_f = selector.start_request()
    trace.append(("start", lease_f.endpoint, lease_f.request_id))
    trace.append(("offset", selector.random_stream.offset))
    return trace


class SnapshotReplayTests(unittest.TestCase):
    def test_full_replay_matches_trace_and_offset(self) -> None:
        # 脚本包含多次双端点选择（每次消耗 2 个随机数），预留充足字节。
        data = b"".join(u64(v) for v in range(64))
        original = EndpointSelector(RandomByteStream(data))
        expected = replay_script(original)
        offset_after = original.random_stream.offset

        state = original.snapshot()
        # 快照必须是 JSON 可序列化的纯数据。
        encoded = json.dumps(state)
        restored = EndpointSelector.restore(
            json.loads(encoded), RandomByteStream(data)
        )
        self.assertEqual(restored.random_stream.offset, offset_after)
        # 重放脚本在恢复后的选择器上继续执行：从快照点起的新轨迹一致。
        continued: list = []
        lease = restored.start_request()
        continued.append(("start", lease.endpoint, lease.request_id))
        continued.append(("offset", restored.random_stream.offset))
        # 对照：原选择器在同一随机流位置继续。
        original_lease = original.start_request()
        self.assertEqual(lease.endpoint, original_lease.endpoint)
        self.assertEqual(lease.request_id, original_lease.request_id)
        self.assertEqual(restored.random_stream.offset, original.random_stream.offset)
        self.assertEqual(continued[0][1:],
                         ("start", original_lease.endpoint, original_lease.request_id)[1:])
        # 先完整跑一遍全新实例，确认整条脚本轨迹可逐位重放。
        fresh = EndpointSelector(RandomByteStream(data))
        self.assertEqual(replay_script(fresh), expected)
        self.assertEqual(fresh.random_stream.offset, offset_after)

    def test_snapshot_midstream_continuation_identical(self) -> None:
        data = b"".join(u64(v) for v in range(40))
        selector = EndpointSelector(RandomByteStream(data))
        selector.upsert_endpoint("a", 1)
        selector.upsert_endpoint("b", 2)
        selector.upsert_endpoint("c", 3)
        leases = [selector.start_request() for _ in range(4)]
        mid_state = selector.snapshot()
        mid_offset = selector.random_stream.offset
        for lease in leases:
            selector.end_request(lease)
        tail_original = [
            (l.endpoint, l.request_id, l.generation)
            for l in (selector.start_request() for _ in range(5))
        ]

        restored = EndpointSelector.restore(
            mid_state, RandomByteStream(data)
        )
        self.assertEqual(restored.random_stream.offset, mid_offset)
        # 恢复点上的活动租约同样存在，先归还再继续。
        active = list(restored.snapshot()["active_leases"])
        self.assertEqual(len(active), 4)
        for lease_state in active:
            restored.end_request(Lease.from_dict(lease_state))
        tail_restored = [
            (l.endpoint, l.request_id, l.generation)
            for l in (restored.start_request() for _ in range(5))
        ]
        self.assertEqual(tail_restored, tail_original)
        self.assertEqual(restored.random_stream.offset, selector.random_stream.offset)

    def test_snapshot_preserves_retired_generation_bookkeeping(self) -> None:
        data = b"".join(u64(v) for v in range(8))
        selector = EndpointSelector(RandomByteStream(data))
        selector.upsert_endpoint("a", 1)
        selector.upsert_endpoint("b", 1)
        lease = selector.start_request()
        # 两个健康端点：两轮抽样各消耗一个随机数。
        self.assertEqual(selector.random_stream.offset, 16)
        selector.remove_endpoint("a")
        state = selector.snapshot()
        self.assertTrue(any(name == "a" for name, _ in state["retired"]))
        restored = EndpointSelector.restore(state, RandomByteStream(data))
        # 随机流恢复到快照偏移（16 字节处）。
        self.assertEqual(restored.random_stream.offset, 16)
        # 旧实例的租约在恢复后的实例上仍可归还。
        restored.end_request(lease)
        # 加入第二个端点后再选择，需要消耗两个新随机数，偏移继续前进。
        restored.upsert_endpoint("c", 1)
        restored.start_request()
        self.assertEqual(restored.random_stream.offset, 32)

    def test_unknown_snapshot_version_rejected(self) -> None:
        selector = EndpointSelector(RandomByteStream(b""))
        state = selector.snapshot()
        state["version"] = 999
        with self.assertRaises(ValueError):
            EndpointSelector.restore(state, RandomByteStream(b""))

    def test_snapshot_is_json_safe_with_string_and_int_ids(self) -> None:
        selector = EndpointSelector(RandomByteStream(b""))
        selector.upsert_endpoint("pod-1", 2)
        selector.upsert_endpoint(7, 4)
        encoded = json.dumps(selector.snapshot())
        restored = EndpointSelector.restore(
            json.loads(encoded), RandomByteStream(b"")
        )
        self.assertEqual(set(restored.healthy_endpoints), {"pod-1", 7})


if __name__ == "__main__":
    unittest.main()
