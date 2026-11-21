"""RoundScheduler 的本地合成数据单元测试。

注意：这些测试仅使用本地构造的字符串 Token，不依赖任何外部服务或真实模型。
按本次任务约定，测试代码已编写但尚未在本环境中运行（见 README 的“验证状态”）。
"""

import unittest

from scheduler import (
    FinishReason,
    RejectReason,
    RoundScheduler,
    SessionState,
)


def cycling_provider(pattern):
    """返回按调用次序循环产出 pattern 中 Token 的注入回调。"""

    def _provider(request_id, round_index):
        # 不同会话独立计数：用函数属性保存各会话的调用次数。
        counters = getattr(_provider, "counters", {})
        idx = counters.get(request_id, 0)
        counters[request_id] = idx + 1
        return pattern[idx % len(pattern)]

    _provider.counters = {}
    return _provider


def fixed_provider(token):
    return lambda request_id, round_index: token


class RoundSchedulerTest(unittest.TestCase):
    # ------------------------------------------------------------------ #
    # 1. 基本流程：先处理提示词，下一轮才开始出词
    # ------------------------------------------------------------------ #

    def test_prefill_then_decode_starts_next_round(self):
        sch = RoundScheduler(slot_budget=4, cache_capacity=100,
                             stop_tokens=["<eos>"])
        sch.add_request("r1", prompt_len=3, max_new_tokens=2)

        r0 = sch.step(cycling_provider(["a", "b"]))
        self.assertEqual(r0.round_index, 0)
        self.assertEqual(r0.round_number, 1)
        # 提示词 3 个 Token 一轮处理完，没有任何解码工作。
        self.assertEqual(len(r0.prompt_work), 1)
        self.assertEqual(r0.prompt_work[0].request_id, "r1")
        self.assertEqual((r0.prompt_work[0].prompt_start,
                          r0.prompt_work[0].prompt_end), (0, 3))
        self.assertTrue(r0.prompt_work[0].completed)
        self.assertEqual(r0.decode_work, [])
        self.assertEqual(r0.prefill_slots_used, 3)
        self.assertEqual(r0.decode_slots_used, 0)
        self.assertEqual(r0.unused_slots, 1)
        self.assertEqual(sch.get_session("r1").state, SessionState.DECODING)

        r1 = sch.step(cycling_provider(["a", "b"]))
        self.assertEqual(len(r1.decode_work), 1)
        self.assertEqual(r1.decode_work[0].token, "a")
        self.assertEqual(r1.decode_work[0].output_index, 0)
        self.assertEqual(r1.prompt_work, [])
        self.assertEqual(r1.decode_slots_used, 1)
        self.assertEqual(r1.unused_slots, 3)

    # ------------------------------------------------------------------ #
    # 2. 槽位优先级：解码先各占一个槽，剩余槽才给提示词处理
    # ------------------------------------------------------------------ #

    def test_decode_slots_reserved_before_prefill(self):
        # B=3：两个解码会话先占 2 槽，只剩 1 槽给新会话的提示词。
        sch = RoundScheduler(slot_budget=3, cache_capacity=100)
        sch.add_request("old1", prompt_len=1, max_new_tokens=5)
        sch.add_request("old2", prompt_len=1, max_new_tokens=5)
        sch.step(fixed_provider("x"))  # 处理两条长度 1 的提示词
        sch.step(fixed_provider("x"))  # 两个老会话进入解码

        sch.add_request("new", prompt_len=4, max_new_tokens=2)
        result = sch.step(fixed_provider("x"))

        decode_ids = [w.request_id for w in result.decode_work]
        self.assertEqual(sorted(decode_ids), ["old1", "old2"])
        self.assertEqual(result.decode_slots_used, 2)
        # 新会话只拿到 1 个剩余槽，提示词只处理了 1/4。
        self.assertEqual(len(result.prompt_work), 1)
        self.assertEqual(result.prompt_work[0].request_id, "new")
        self.assertEqual(result.prompt_work[0].tokens_processed, 1)
        self.assertFalse(result.prompt_work[0].completed)
        self.assertEqual(result.prefill_slots_used, 1)
        self.assertEqual(result.unused_slots, 0)

    # ------------------------------------------------------------------ #
    # 3. 提示词分段处理：跨轮推进，处理完的下一轮才出词
    # ------------------------------------------------------------------ #

    def test_prompt_split_across_rounds(self):
        sch = RoundScheduler(slot_budget=2, cache_capacity=100)
        sch.add_request("r1", prompt_len=5, max_new_tokens=2)

        r0 = sch.step()
        self.assertEqual(r0.prompt_work[0].prompt_end, 2)
        self.assertFalse(r0.prompt_work[0].completed)

        r1 = sch.step()
        self.assertEqual((r1.prompt_work[0].prompt_start,
                          r1.prompt_work[0].prompt_end), (2, 4))
        self.assertFalse(r1.prompt_work[0].completed)
        self.assertEqual(r1.decode_work, [])

        r2 = sch.step()
        self.assertEqual((r2.prompt_work[0].prompt_start,
                          r2.prompt_work[0].prompt_end), (4, 5))
        self.assertTrue(r2.prompt_work[0].completed)
        self.assertEqual(r2.decode_work, [])  # 当轮仍不出词

        r3 = sch.step(fixed_provider("t"))
        self.assertEqual(len(r3.decode_work), 1)
        self.assertEqual(r3.decode_work[0].token, "t")

    # ------------------------------------------------------------------ #
    # 4. 接纳：按入队序，队首放不下则跳过，后面能放下的照样接纳
    # ------------------------------------------------------------------ #

    def test_admission_skips_head_that_does_not_fit(self):
        sch = RoundScheduler(slot_budget=4, cache_capacity=10)
        # big 预留 9，small 预留 3；big 先入队。
        sch.add_request("big", prompt_len=5, max_new_tokens=4)    # 预留 9
        sch.add_request("small", prompt_len=2, max_new_tokens=1)  # 预留 3

        result = sch.step()
        self.assertEqual(
            [p.request_id for p in result.prompt_work], ["small"]
        )
        self.assertEqual(result.waiting_session_ids, ["big"])
        # big 没有被拒绝，只是暂时等待。
        self.assertEqual(sch.get_session("big").state, SessionState.WAITING)
        self.assertEqual(sch.cache_reserved, 3)

    def test_admission_preserves_enqueue_order_when_space_frees(self):
        sch = RoundScheduler(slot_budget=4, cache_capacity=10)
        sch.add_request("big", prompt_len=5, max_new_tokens=4)    # 预留 9
        sch.add_request("small", prompt_len=2, max_new_tokens=1)  # 预留 3

        first = sch.step()
        self.assertEqual(first.waiting_session_ids, ["big"])

        # small 在第二轮解码并立即到输出上限，轮末释放。
        second = sch.step(fixed_provider("x"))
        self.assertEqual(second.cache_reserved_end, 0)
        self.assertEqual(
            [f.request_id for f in second.finished], ["small"]
        )

        # 第三轮 big 被接纳。
        third = sch.step()
        self.assertEqual(third.waiting_session_ids, [])
        self.assertEqual(
            [p.request_id for p in third.prompt_work], ["big"]
        )
        self.assertEqual(third.cache_reserved_end, 9)

    # ------------------------------------------------------------------ #
    # 5. 单条需求超过总容量：拒绝
    # ------------------------------------------------------------------ #

    def test_request_exceeding_total_capacity_is_rejected(self):
        sch = RoundScheduler(slot_budget=2, cache_capacity=10)
        sch.add_request("huge", prompt_len=8, max_new_tokens=5)  # 预留 13 > 10

        result = sch.step()
        self.assertEqual(len(result.rejected), 1)
        rejection = result.rejected[0]
        self.assertEqual(rejection.request_id, "huge")
        self.assertEqual(rejection.reason, RejectReason.EXCEEDS_CAPACITY)
        self.assertEqual(rejection.required_tokens, 13)
        self.assertEqual(result.cache_reserved_end, 0)
        self.assertEqual(sch.get_session("huge").state, SessionState.FINISHED)
        self.assertFalse(sch.has_pending_work())

    def test_oversized_rejection_does_not_block_later_requests(self):
        sch = RoundScheduler(slot_budget=4, cache_capacity=10)
        sch.add_request("huge", prompt_len=9, max_new_tokens=5)  # 14 > 10
        sch.add_request("ok", prompt_len=2, max_new_tokens=2)    # 4 <= 10

        result = sch.step()
        self.assertEqual([r.request_id for r in result.rejected], ["huge"])
        self.assertEqual(
            [p.request_id for p in result.prompt_work], ["ok"]
        )
        self.assertEqual(result.cache_reserved_end, 4)

    # ------------------------------------------------------------------ #
    # 6. 取消：等待中取消与活跃取消都在轮末释放并上报
    # ------------------------------------------------------------------ #

    def test_cancel_waiting_request_reports_without_reservation(self):
        sch = RoundScheduler(slot_budget=2, cache_capacity=100)
        sch.add_request("r1", prompt_len=3, max_new_tokens=2)
        self.assertTrue(sch.cancel_request("r1"))

        result = sch.step()
        self.assertEqual(len(result.finished), 1)
        self.assertEqual(result.finished[0].request_id, "r1")
        self.assertEqual(result.finished[0].reason, FinishReason.CANCELLED)
        self.assertEqual(result.prompt_work, [])
        self.assertEqual(result.cache_reserved_end, 0)
        self.assertFalse(sch.has_pending_work())
        # 已终止会话重复取消返回 False。
        self.assertFalse(sch.cancel_request("r1"))
        self.assertFalse(sch.cancel_request("unknown"))

    def test_cancel_active_request_skips_work_and_releases_at_round_end(self):
        sch = RoundScheduler(slot_budget=4, cache_capacity=100)
        sch.add_request("r1", prompt_len=2, max_new_tokens=5)
        sch.step()  # 处理完提示词，下一轮进入解码

        self.assertTrue(sch.cancel_request("r1"))
        result = sch.step(fixed_provider("should-not-be-used"))
        # 被取消的会话既不解码也不处理提示词。
        self.assertEqual(result.decode_work, [])
        self.assertEqual(len(result.finished), 1)
        self.assertEqual(result.finished[0].reason, FinishReason.CANCELLED)
        self.assertEqual(result.finished[0].output_len, 0)
        self.assertEqual(result.cache_reserved_end, 0)

    # ------------------------------------------------------------------ #
    # 7. 停止符与输出上限：终止原因与轮末释放
    # ------------------------------------------------------------------ #

    def test_stop_token_finishes_and_keeps_token_in_output(self):
        sch = RoundScheduler(slot_budget=2, cache_capacity=100,
                             stop_tokens=["<eos>"])
        sch.add_request("r1", prompt_len=1, max_new_tokens=5)
        sch.step()  # prefill

        tokens = iter(["a", "<eos>"])
        result = sch.step(lambda rid, rnd: next(tokens))
        self.assertEqual(len(result.decode_work), 2)
        self.assertEqual(result.decode_work[0].finish_reason, None)
        self.assertEqual(result.decode_work[1].finish_reason, FinishReason.STOP)
        self.assertEqual(len(result.finished), 1)
        self.assertEqual(result.finished[0].reason, FinishReason.STOP)
        # 停止符本身保留在输出序列中。
        self.assertEqual(result.finished[0].output_tokens, ("a", "<eos>"))
        self.assertEqual(result.cache_reserved_end, 0)

    def test_length_limit_finishes_at_max_new_tokens(self):
        sch = RoundScheduler(slot_budget=4, cache_capacity=100,
                             stop_tokens=["<eos>"])
        sch.add_request("r1", prompt_len=1, max_new_tokens=3)
        sch.step()

        result = sch.step(cycling_provider(["a", "b", "c"]))
        self.assertEqual(len(result.decode_work), 3)
        self.assertEqual(
            result.decode_work[-1].finish_reason, FinishReason.LENGTH
        )
        self.assertEqual(result.finished[0].reason, FinishReason.LENGTH)
        self.assertEqual(result.finished[0].output_len, 3)
        self.assertEqual(result.cache_reserved_end, 0)

    def test_stop_token_takes_precedence_over_length(self):
        # 最后一步同时命中停止符和输出上限：停止符优先。
        sch = RoundScheduler(slot_budget=4, cache_capacity=100,
                             stop_tokens=["<eos>"])
        sch.add_request("r1", prompt_len=1, max_new_tokens=2)
        sch.step()

        result = sch.step(cycling_provider(["a", "<eos>"]))
        self.assertEqual(
            result.decode_work[-1].finish_reason, FinishReason.STOP
        )
        self.assertEqual(result.finished[0].reason, FinishReason.STOP)

    # ------------------------------------------------------------------ #
    # 8. 预留与释放：提前结束腾出的空间下一轮可用
    # ------------------------------------------------------------------ #

    def test_released_cache_is_reusable_next_round(self):
        sch = RoundScheduler(slot_budget=4, cache_capacity=6)
        sch.add_request("a", prompt_len=1, max_new_tokens=2)  # 预留 3
        sch.add_request("b", prompt_len=1, max_new_tokens=2)  # 预留 3
        first = sch.step()
        self.assertEqual(first.cache_reserved_end, 6)

        # 两个会话各出一个词，都还没结束。
        second = sch.step(cycling_provider(["x"]))
        self.assertEqual(second.cache_reserved_end, 6)

        # 再来一个需要 3 预留的会话：当前放不下，只能等待。
        sch.add_request("c", prompt_len=1, max_new_tokens=2)
        third = sch.step(cycling_provider(["x"]))
        self.assertEqual(third.waiting_session_ids, ["c"])

        # a、b 到输出上限，轮末释放全部缓存。
        self.assertEqual(
            sorted(f.request_id for f in third.finished), ["a", "b"]
        )
        self.assertEqual(third.cache_reserved_end, 0)

        # 下一轮 c 即可使用腾出的空间。
        fourth = sch.step()
        self.assertEqual(fourth.waiting_session_ids, [])
        self.assertEqual(
            [p.request_id for p in fourth.prompt_work], ["c"]
        )
        self.assertEqual(fourth.cache_reserved_end, 3)

    # ------------------------------------------------------------------ #
    # 9. 并发会话上限不超过 B
    # ------------------------------------------------------------------ #

    def test_active_session_count_capped_at_slot_budget(self):
        sch = RoundScheduler(slot_budget=2, cache_capacity=1000)
        for rid in ("a", "b", "c"):
            sch.add_request(rid, prompt_len=10, max_new_tokens=10)

        result = sch.step()
        self.assertEqual(len(result.active_session_ids), 2)
        self.assertEqual(result.waiting_session_ids, ["c"])

        # 缓存明明足够，c 仍因并发上限继续等待。
        self.assertEqual(sch.cache_reserved, 2 * 20)

    # ------------------------------------------------------------------ #
    # 10. 多会话提示词处理按入队序消耗剩余槽
    # ------------------------------------------------------------------ #

    def test_prefill_slots_shared_in_enqueue_order(self):
        sch = RoundScheduler(slot_budget=3, cache_capacity=100)
        sch.add_request("a", prompt_len=2, max_new_tokens=2)
        sch.add_request("b", prompt_len=3, max_new_tokens=2)

        result = sch.step()
        # a 先拿 2 槽处理完，剩余 1 槽给 b。
        work = {p.request_id: p for p in result.prompt_work}
        self.assertEqual(work["a"].tokens_processed, 2)
        self.assertTrue(work["a"].completed)
        self.assertEqual(work["b"].tokens_processed, 1)
        self.assertFalse(work["b"].completed)
        self.assertEqual(result.prefill_slots_used, 3)
        self.assertEqual(result.unused_slots, 0)

    # ------------------------------------------------------------------ #
    # 11. 空轮与映射形式的 Token 注入
    # ------------------------------------------------------------------ #

    def test_step_without_requests_is_empty_round(self):
        sch = RoundScheduler(slot_budget=2, cache_capacity=10)
        result = sch.step()
        self.assertEqual(result.round_index, 0)
        self.assertEqual(result.decode_work, [])
        self.assertEqual(result.prompt_work, [])
        self.assertEqual(result.finished, [])
        self.assertEqual(result.rejected, [])
        self.assertEqual(result.unused_slots, 2)
        self.assertFalse(sch.has_pending_work())

    def test_mapping_token_provider(self):
        sch = RoundScheduler(slot_budget=4, cache_capacity=100)
        sch.add_request("r1", prompt_len=1, max_new_tokens=2)
        sch.step()

        result = sch.step({"r1": "mapped-token"})
        self.assertEqual(result.decode_work[0].token, "mapped-token")

    def test_missing_provider_for_decode_raises(self):
        sch = RoundScheduler(slot_budget=2, cache_capacity=100)
        sch.add_request("r1", prompt_len=1, max_new_tokens=2)
        sch.step()
        with self.assertRaises(ValueError):
            sch.step()

    # ------------------------------------------------------------------ #
    # 12. 参数校验与重复入队
    # ------------------------------------------------------------------ #

    def test_constructor_validation(self):
        with self.assertRaises(ValueError):
            RoundScheduler(slot_budget=0, cache_capacity=10)
        with self.assertRaises(ValueError):
            RoundScheduler(slot_budget=2, cache_capacity=0)
        with self.assertRaises(ValueError):
            RoundScheduler(slot_budget=True, cache_capacity=10)  # type: ignore[arg-type]

    def test_add_request_validation_and_duplicate(self):
        sch = RoundScheduler(slot_budget=2, cache_capacity=10)
        with self.assertRaises(ValueError):
            sch.add_request("", prompt_len=1, max_new_tokens=1)
        with self.assertRaises(ValueError):
            sch.add_request("r", prompt_len=0, max_new_tokens=1)
        with self.assertRaises(ValueError):
            sch.add_request("r", prompt_len=1, max_new_tokens=0)

        sch.add_request("r", prompt_len=1, max_new_tokens=1)
        with self.assertRaises(ValueError):
            sch.add_request("r", prompt_len=1, max_new_tokens=1)

    # ------------------------------------------------------------------ #
    # 13. 会话快照信息
    # ------------------------------------------------------------------ #

    def test_session_snapshot_fields(self):
        sch = RoundScheduler(slot_budget=4, cache_capacity=100,
                             stop_tokens=["<eos>"])
        sch.add_request("r1", prompt_len=3, max_new_tokens=2)
        info = sch.get_session("r1")
        self.assertEqual(info.state, SessionState.WAITING)
        self.assertEqual(info.reserved_tokens, 5)
        self.assertEqual(info.prompt_processed, 0)
        self.assertEqual(info.enqueue_seq, 0)

        sch.step()
        info = sch.get_session("r1")
        self.assertEqual(info.state, SessionState.DECODING)
        self.assertEqual(info.prompt_processed, 3)

    def test_finished_list_ordered_by_enqueue_sequence(self):
        sch = RoundScheduler(slot_budget=4, cache_capacity=100,
                             stop_tokens=["<eos>"])
        sch.add_request("first", prompt_len=1, max_new_tokens=2)
        sch.add_request("second", prompt_len=1, max_new_tokens=2)
        sch.step()
        result = sch.step({"first": "<eos>", "second": "<eos>"})
        self.assertEqual(
            [f.request_id for f in result.finished], ["first", "second"]
        )


if __name__ == "__main__":
    unittest.main()
