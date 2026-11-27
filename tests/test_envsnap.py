"""envsnap 的本地合成数据测试。

覆盖题面要求的全部场景：
* 正常赋值 / eager 立即展开 / lazy 读取时展开；
* 空字串、未定义、间接引用环三态区分；
* 删除后重建同名变量（旧绑定不进最小切片）；
* 父子进程派生快照与独立修改、分别重定义路径前缀；
* 最小来源切片的实际重放核对（值一致或同一循环诊断）；
* 引用语法与 eager 赋值期环检测。
"""

import unittest

from envsnap import (
    CycleError,
    Engine,
    ReadStatus,
    Recorder,
    expand_references,
    replay,
    replay_all,
)


class DeleteThenRecreateTests(unittest.TestCase):
    """删除后重建同名变量：旧绑定必须被遮蔽，不进最小切片。"""

    def test_recreated_variable_slice_excludes_dead_binding(self):
        r = Recorder()
        id_old = r.set("X", "old")
        id_del = r.delete("X")
        id_new = r.set("X", "new")
        ex = r.explain("X")

        self.assertEqual(ex.result.status, ReadStatus.VALUE)
        self.assertEqual(ex.result.value, "new")
        # 最小切片只含重建后的绑定；删除事件与旧值都不再需要。
        self.assertEqual(ex.slice_ids, (id_new,))
        self.assertNotIn(id_old, ex.slice_ids)
        self.assertNotIn(id_del, ex.slice_ids)
        replayed = ex.verify()
        self.assertEqual(replayed.value, "new")

    def test_delete_then_read_is_undefined_and_replays_alone(self):
        r = Recorder()
        id_set = r.set("X", "old")
        id_del = r.delete("X")
        ex = r.explain("X")

        self.assertTrue(ex.result.is_undefined)
        self.assertIsNone(ex.result.value)
        # 删除事件本身足以在空环境上重放出 UNDEFINED。
        self.assertEqual(ex.slice_ids, (id_del,))
        self.assertNotIn(id_set, ex.slice_ids)
        ex.verify()

    def test_never_defined_has_empty_slice(self):
        r = Recorder()
        ex = r.explain("GHOST")
        self.assertTrue(ex.result.is_undefined)
        self.assertEqual(ex.slice_ids, ())
        # 空切片重放仍诊断为未定义。
        self.assertTrue(replay((), 0, "GHOST").is_undefined)
        ex.verify()

    def test_empty_string_is_distinct_from_undefined(self):
        r = Recorder()
        id_empty = r.set("EMPTY", "")
        ex_value = r.explain("EMPTY")
        ex_missing = r.explain("MISSING")

        self.assertEqual(ex_value.result.status, ReadStatus.VALUE)
        self.assertEqual(ex_value.result.value, "")
        self.assertEqual(ex_value.slice_ids, (id_empty,))
        self.assertTrue(ex_missing.result.is_undefined)
        self.assertNotEqual(ex_value.result.status, ex_missing.result.status)
        ex_value.verify()
        ex_missing.verify()


class BranchAndPathPrefixTests(unittest.TestCase):
    """父子进程：派生瞬间继承快照，随后独立修改路径前缀。"""

    def test_parent_and_child_redefine_path_prefix_independently(self):
        r = Recorder()
        id_root_v1 = r.set("PATH", "/root/bin")
        id_spawn, _child = r.engine.record_spawn(0)
        child = Recorder()
        child.engine = r.engine
        child.proc = _child
        id_root_v2 = r.set("PATH", "/root/v2")
        id_child_v = child.set("PATH", "/child/bin")

        root_ex = r.explain("PATH")
        child_ex = child.explain("PATH")

        self.assertEqual(root_ex.result.value, "/root/v2")
        self.assertEqual(root_ex.slice_ids, (id_root_v2,))
        self.assertEqual(child_ex.result.value, "/child/bin")
        # 子进程的切片必须带上 spawn 边才能重建分支。
        self.assertEqual(child_ex.slice_ids, (id_spawn, id_child_v))
        root_ex.verify()
        child_ex.verify()

        # 各自快照互不污染。
        self.assertEqual(r.snapshot()["PATH"], "/root/v2")
        self.assertEqual(child.snapshot()["PATH"], "/child/bin")

    def test_child_inherits_snapshot_at_spawn_moment(self):
        r = Recorder()
        id_base = r.set("VAR", "base")
        id_spawn, child_proc = r.engine.record_spawn(0)
        child = Recorder()
        child.engine = r.engine
        child.proc = child_proc
        r.set("VAR", "parent-new")  # 派生之后父进程的修改对子进程不可见

        ex = child.explain("VAR")
        self.assertEqual(ex.result.value, "base")
        self.assertEqual(ex.slice_ids, (id_base, id_spawn))
        ex.verify()
        # 父进程读到的是自己的新值。
        self.assertEqual(r.read("VAR").value, "parent-new")

    def test_child_unset_cuts_inheritance_with_local_terminal(self):
        r = Recorder()
        id_base = r.set("VAR", "base")
        id_spawn, child_proc = r.engine.record_spawn(0)
        child = Recorder()
        child.engine = r.engine
        child.proc = child_proc
        id_child_del = child.delete("VAR")

        ex = child.explain("VAR")
        self.assertTrue(ex.result.is_undefined)
        # 本地删除事件就是终结事件，祖先赋值不再需要；但非根进程的切片
        # 仍需 spawn 链，否则重放时该进程不存在。
        self.assertEqual(ex.slice_ids, (id_spawn, id_child_del))
        self.assertNotIn(id_base, ex.slice_ids)
        ex.verify()

    def test_inherited_variable_slice_contains_spawn_edge(self):
        r = Recorder()
        r.set("PATH", "/root/bin")
        id_tool = r.set("TOOL", "/opt/tool")  # 派生之前存在：子进程可继承
        id_spawn, child_proc = r.engine.record_spawn(0)
        child = Recorder()
        child.engine = r.engine
        child.proc = child_proc
        r.set("PATH", "/root/v2")
        child.set("PATH", "/child/bin")
        r.set("LATE", "/new-after-spawn")  # 派生之后新增：子进程不可见

        ex = child.explain("TOOL")
        self.assertEqual(ex.result.value, "/opt/tool")
        self.assertEqual(ex.slice_ids, tuple(sorted((id_spawn, id_tool))))
        ex.verify()

        ex_late = child.explain("LATE")
        self.assertTrue(ex_late.result.is_undefined)
        # 仅需 spawn 链即可在重放中复现"派生时刻该变量尚不存在"。
        self.assertEqual(ex_late.slice_ids, (id_spawn,))
        ex_late.verify()


class EagerVsLazyTests(unittest.TestCase):
    """eager 按当时值烘焙；lazy 保存表达式到读取时展开。"""

    def test_eager_value_is_baked_at_assignment_time(self):
        r = Recorder()
        r.set("A", "hello")
        id_b = r.set("B", "pre-$A-post", eager=True)
        r.set("A", "changed")

        ex = r.explain("B")
        self.assertEqual(ex.result.value, "pre-hello-post")
        # 烘焙后 B 是字面值，最早的 A 绑定不再是重放依赖。
        self.assertEqual(ex.slice_ids, (id_b,))
        ex.verify()

    def test_eager_undefined_reference_expands_to_empty(self):
        r = Recorder()
        id_c = r.set("C", "[$NOPE]", eager=True)
        ex = r.explain("C")
        self.assertEqual(ex.result.value, "[]")
        self.assertEqual(ex.slice_ids, (id_c,))
        ex.verify()

    def test_lazy_expression_evaluates_at_read_time(self):
        r = Recorder()
        id_lazy = r.set("LATEST", "$VERSION", eager=False)
        id_v1 = r.set("VERSION", "1.0")
        ex1 = r.explain("LATEST")
        self.assertEqual(ex1.result.value, "1.0")
        self.assertEqual(ex1.slice_ids, (id_lazy, id_v1))
        ex1.verify()

        id_v2 = r.set("VERSION", "2.0")
        ex2 = r.explain("LATEST")
        self.assertEqual(ex2.result.value, "2.0")
        # 旧 VERSION 被遮蔽，最小切片只保留新绑定。
        self.assertEqual(ex2.slice_ids, (id_lazy, id_v2))
        self.assertNotIn(id_v1, ex2.slice_ids)
        ex2.verify()

    def test_lazy_chain_shadowed_intermediary_is_excluded(self):
        r = Recorder()
        id_p1 = r.set("P", "/base")
        id_q = r.set("Q", "$P", eager=False)
        id_p2 = r.set("P", "/new")
        ex = r.explain("Q")
        self.assertEqual(ex.result.value, "/new")
        self.assertEqual(ex.slice_ids, (id_q, id_p2))
        self.assertNotIn(id_p1, ex.slice_ids)
        ex.verify()

    def test_lazy_reference_to_deleted_variable_is_empty_string_not_undefined(self):
        r = Recorder()
        id_a = r.set("A", "$B", eager=False)  # A 本身有绑定
        r.set("B", "x")
        id_del = r.delete("B")

        ex_a = r.explain("A")
        # 未定义引用按 shell 语义展开为空字符串：A 仍是已定义变量。
        self.assertEqual(ex_a.result.status, ReadStatus.VALUE)
        self.assertEqual(ex_a.result.value, "")
        self.assertEqual(ex_a.slice_ids, (id_a, id_del))
        ex_a.verify()

        ex_b = r.explain("B")
        self.assertTrue(ex_b.result.is_undefined)
        ex_b.verify()

    def test_lazy_mixed_literal_and_undefined_reference(self):
        r = Recorder()
        r.set("A", "v-$B/", eager=False)
        ex = r.explain("A")
        self.assertEqual(ex.result.value, "v-/")
        ex.verify()


class CycleTests(unittest.TestCase):
    """间接引用环：读取期诊断与确定性环链。"""

    def test_two_node_lazy_cycle(self):
        r = Recorder()
        id_a = r.set("A", "$B", eager=False)
        id_b = r.set("B", "$A", eager=False)

        ex_a = r.explain("A")
        self.assertTrue(ex_a.result.is_cycle)
        self.assertEqual(ex_a.result.cycle, ("A", "B", "A"))
        self.assertEqual(ex_a.slice_ids, (id_a, id_b))
        ex_a.verify()  # 重放必须给出 *同一个* 循环诊断

        ex_b = r.explain("B")
        self.assertEqual(ex_b.result.cycle, ("B", "A", "B"))
        self.assertEqual(ex_b.slice_ids, (id_a, id_b))
        ex_b.verify()

    def test_self_cycle(self):
        r = Recorder()
        id_x = r.set("X", "$X", eager=False)
        ex = r.explain("X")
        self.assertTrue(ex.result.is_cycle)
        self.assertEqual(ex.result.cycle, ("X", "X"))
        self.assertEqual(ex.slice_ids, (id_x,))
        ex.verify()

    def test_three_node_cycle_chain_is_deterministic(self):
        r = Recorder()
        ids = (
            r.set("A", "$B", eager=False),
            r.set("B", "$C", eager=False),
            r.set("C", "$A", eager=False),
        )
        ex = r.explain("A")
        self.assertEqual(ex.result.cycle, ("A", "B", "C", "A"))
        self.assertEqual(ex.slice_ids, ids)
        ex.verify()

    def test_eager_assignment_into_cycle_raises_and_records_nothing(self):
        r = Recorder()
        r.set("A", "$B", eager=False)
        r.set("B", "$A", eager=False)
        before = len(r.events)
        with self.assertRaises(CycleError) as ctx:
            r.set("C", "$A", eager=True)
        self.assertEqual(ctx.exception.cycle, ("A", "B", "A"))
        # 环在赋值当时被检测，不得写入半截绑定。
        self.assertEqual(len(r.events), before)

    def test_child_can_break_parent_cycle_independently(self):
        r = Recorder()
        id_a = r.set("A", "$B", eager=False)
        id_b = r.set("B", "$A", eager=False)
        id_spawn, child_proc = r.engine.record_spawn(0)
        child = Recorder()
        child.engine = r.engine
        child.proc = child_proc
        id_child_b = child.set("B", "fixed")

        child_ex = child.explain("A")
        self.assertEqual(child_ex.result.status, ReadStatus.VALUE)
        self.assertEqual(child_ex.result.value, "fixed")
        self.assertEqual(child_ex.slice_ids, (id_a, id_spawn, id_child_b))
        child_ex.verify()

        root_ex = r.explain("A")
        self.assertTrue(root_ex.result.is_cycle)
        self.assertEqual(root_ex.result.cycle, ("A", "B", "A"))
        self.assertEqual(root_ex.slice_ids, (id_a, id_b))
        root_ex.verify()


class SliceReplayTests(unittest.TestCase):
    """整日志重放与切片重放的一致性核对。"""

    def test_replay_all_matches_every_recorded_read(self):
        r = Recorder()
        r.set("PATH", "/root/bin")
        r.set("GREET", "hello $NAME", eager=False)
        r.set("NAME", "world")
        expected = {}
        eid, res = r.engine.record_read(0, "PATH")
        expected[eid] = res
        eid, res = r.engine.record_read(0, "GREET")
        expected[eid] = res

        _id_spawn, child_proc = r.engine.record_spawn(0)
        child = Recorder()
        child.engine = r.engine
        child.proc = child_proc
        child.set("NAME", "child")
        eid, res = r.engine.record_read(child_proc, "GREET")
        expected[eid] = res
        child.delete("PATH")
        eid, res = r.engine.record_read(child_proc, "PATH")
        expected[eid] = res

        replayed = replay_all(r.engine.events)
        self.assertEqual(set(replayed), set(expected))
        for eid, original in expected.items():
            got = replayed[eid]
            self.assertEqual(got.status, original.status)
            self.assertEqual(got.value, original.value)
            self.assertEqual(got.cycle, original.cycle)

    def test_repeated_assignments_keep_only_last_binding(self):
        r = Recorder()
        r.set("X", "a")
        r.set("X", "b")
        id_last = r.set("X", "c")
        ex = r.explain("X")
        self.assertEqual(ex.result.value, "c")
        self.assertEqual(ex.slice_ids, (id_last,))
        ex.verify()

    def test_wrong_slice_is_detected_by_value_mismatch(self):
        r = Recorder()
        r.set("X", "a")
        id_b = r.set("X", "b")
        r.explain("X")
        # 故意只重放旧切片之外的错误事件组合，确认核对不是橡皮图章。
        wrong = replay([r.engine.event(1)], 0, "X")
        self.assertEqual(wrong.value, "a")
        self.assertNotEqual(wrong.value, "b")
        self.assertEqual(id_b, r.engine.event(2).id)

    def test_replay_without_spawn_edge_cannot_reconstruct_child(self):
        r = Recorder()
        r.set("V", "1")
        id_spawn, child_proc = r.engine.record_spawn(0)
        child = Recorder()
        child.engine = r.engine
        child.proc = child_proc
        ex = child.explain("V")
        self.assertIn(id_spawn, ex.slice_ids)
        with self.assertRaises(ValueError):
            replay(
                [e for e in ex.slice_events if e.kind != "spawn"],
                child_proc,
                "V",
            )

    def test_grandchild_spawn_chain_replays_with_remapped_proc_ids(self):
        # 回归：多级派生时重放引擎重新分配进程号，必须做编号映射。
        r = Recorder()
        id_base = r.set("ROOTVAR", "r")
        id_spawn1, child_proc = r.engine.record_spawn(0)
        child = Recorder()
        child.engine = r.engine
        child.proc = child_proc
        id_child = child.set("MIDVAR", "m")
        id_spawn2, grand_proc = child.engine.record_spawn(child.proc)
        grand = Recorder()
        grand.engine = r.engine
        grand.proc = grand_proc
        id_grand = grand.set("LEAFVAR", "l")

        # 孙进程读取自己、父进程、根进程三个层级的变量。
        leaf_ex = grand.explain("LEAFVAR")
        mid_ex = grand.explain("MIDVAR")
        root_ex = grand.explain("ROOTVAR")

        self.assertEqual(leaf_ex.result.value, "l")
        self.assertEqual(mid_ex.result.value, "m")
        self.assertEqual(root_ex.result.value, "r")
        self.assertEqual(
            leaf_ex.slice_ids,
            tuple(sorted((id_spawn1, id_spawn2, id_grand))),
        )
        self.assertEqual(
            mid_ex.slice_ids,
            tuple(sorted((id_spawn1, id_spawn2, id_child))),
        )
        self.assertEqual(
            root_ex.slice_ids,
            tuple(sorted((id_spawn1, id_spawn2, id_base))),
        )
        leaf_ex.verify()
        mid_ex.verify()
        root_ex.verify()

        # 孙进程的修改不会泄漏回父/根进程。
        self.assertTrue(child.explain("LEAFVAR").result.is_undefined)
        self.assertTrue(r.explain("MIDVAR").result.is_undefined)


class SyntaxTests(unittest.TestCase):
    """引用语法：$VAR / ${VAR} / $$ 与非法形式。"""

    def test_reference_forms(self):
        r = Recorder()
        r.set("FOO", "bar")
        r.set("UNDER_X1", "ok")
        self.assertEqual(r.read("FOO").value, "bar")
        r.set("A", "$FOO", eager=True)
        self.assertEqual(r.read("A").value, "bar")
        r.set("B", "${FOO}-baz", eager=True)
        self.assertEqual(r.read("B").value, "bar-baz")
        r.set("C", "$$", eager=True)
        self.assertEqual(r.read("C").value, "$")
        r.set("D", "$UNDER_X1!", eager=True)
        self.assertEqual(r.read("D").value, "ok!")

    def test_invalid_syntax_raises(self):
        r = Recorder()
        for bad in ("$", "${UNCLOSED", "${}", "$1"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    r.set("Z", bad, eager=True)

    def test_expand_references_unit_helper(self):
        def resolve(name, stack):
            values = {"A": "1", "B": "2"}
            if name in values:
                return ReadStatus.VALUE, values[name], None, frozenset()
            return ReadStatus.UNDEFINED, None, None, frozenset()

        status, value, _cycle, _deps = expand_references("x$A${B}y", resolve, ())
        self.assertEqual(status, ReadStatus.VALUE)
        self.assertEqual(value, "x12y")
        status, value, _cycle, _deps = expand_references("$MISSING", resolve, ())
        self.assertEqual(value, "")


class EngineValidationTests(unittest.TestCase):
    def test_unknown_process_rejected(self):
        eng = Engine()
        with self.assertRaises(KeyError):
            eng.record_assign(99, "X", "v")
        with self.assertRaises(KeyError):
            eng.record_unset(99, "X")

    def test_explain_requires_read_event(self):
        eng = Engine()
        eid = eng.record_assign(0, "X", "v")
        with self.assertRaises(ValueError):
            eng.explain(eid)

    def test_events_are_globally_ordered_and_typed(self):
        r = Recorder()
        r.set("A", "1")
        r.delete("A")
        spawn_id, child = r.engine.record_spawn(0)
        evs = r.events
        self.assertEqual([e.id for e in evs], [1, 2, 3])
        self.assertEqual(evs[0].kind, "assign")
        self.assertEqual(evs[1].kind, "unset")
        self.assertEqual(evs[2].kind, "spawn")
        self.assertEqual(evs[2].child, child)
        self.assertEqual(spawn_id, 3)


if __name__ == "__main__":
    unittest.main()
