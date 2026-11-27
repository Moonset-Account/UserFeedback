"""固定随机种子的属性测试：随机分叉轨迹上的切片最小性与重放一致性。

每条随机轨迹都会核对：

1. 全日志重放得到的每个 read 结论与引擎实时结论完全一致
   （状态 / 值 / 环链三元组）；
2. 引擎给出的最小切片重放结论一致（即 ``Explanation.verify()``）；
3. 切片中不存在已被同进程后续同名事件遮蔽的"死事件"；
4. 切片是子集最小的：任何严格子集要么重放结论不同、要么缺少 spawn 边
   无法重建进程；唯一的例外是惰性表达式引用了空字符串绑定或删除事件——
   由于"未定义引用"与"空字符串引用"都渲染成空串，去掉它们碰巧不改变
   渲染值，但它们仍是解析时真实遍历到的来源，引擎按因果依赖保留。
"""

import itertools
import random
import unittest
from collections import defaultdict

from envsnap import CycleError, Engine, ReadStatus, replay, replay_all

NAMES = ["A", "B", "C", "PATH", "X"]


def _key(result):
    return (result.status, result.value, result.cycle)


def _render_outcome(engine: Engine, events, proc: int, name: str):
    try:
        return _key(replay(events, proc, name))
    except (ValueError, KeyError):
        return None  # 切片无法重建该进程，视为不等价


def _benign_dropped(engine: Engine, dropped_ids) -> bool:
    """空字符串绑定 / 删除事件经惰性引用渲染为空，去掉后值恰好不变。"""
    for eid in dropped_ids:
        ev = engine.event(eid)
        if ev.kind == "unset":
            continue
        if ev.kind == "assign" and ev.eager and ev.raw == "":
            continue
        return False
    return True


class RandomizedReplayPropertyTests(unittest.TestCase):
    def test_random_branched_traces(self):
        rng = random.Random(20261128)
        for trial in range(60):
            with self.subTest(trial=trial):
                eng = Engine()
                procs = [0]
                reads = []  # (read_event_id, proc, name, expected_key)
                steps = rng.randint(6, 16)
                for _ in range(steps):
                    proc = rng.choice(procs)
                    action = rng.choice(
                        ["eager", "eager", "lazy", "unset", "spawn", "read"]
                    )
                    name = rng.choice(NAMES)
                    if action == "eager":
                        rhs = rng.choice(
                            [
                                rng.choice(["v", "", "/bin", "1.0"]),
                                f"${rng.choice(NAMES)}",
                                f"${{{rng.choice(NAMES)}}}/x",
                            ]
                        )
                        try:
                            eng.record_assign(proc, name, rhs, eager=True)
                        except CycleError:
                            pass  # 环在赋值当时被拒，且不得写入事件
                    elif action == "lazy":
                        rhs = rng.choice(
                            [
                                f"${rng.choice(NAMES)}",
                                f"pre-${rng.choice(NAMES)}-post",
                                f"${{{rng.choice(NAMES)}}}",
                            ]
                        )
                        eng.record_assign(proc, name, rhs, eager=False)
                    elif action == "unset":
                        eng.record_unset(proc, name)
                    elif action == "spawn":
                        _eid, child = eng.record_spawn(proc)
                        procs.append(child)
                    else:
                        result = eng.read_at(proc, name, eng._next_id - 1)
                        eid, stored = eng.record_read(proc, name)
                        self.assertEqual(_key(stored), _key(result))
                        reads.append((eid, proc, name, _key(result)))

                # 保证每条轨迹至少包含一次读取。
                if not reads:
                    proc = rng.choice(procs)
                    name = rng.choice(NAMES)
                    result = eng.read_at(proc, name, eng._next_id - 1)
                    eid, stored = eng.record_read(proc, name)
                    self.assertEqual(_key(stored), _key(result))
                    reads.append((eid, proc, name, _key(result)))
                self.assertGreaterEqual(len(reads), 1)

                # 1) 全日志重放：每个 read 只解析其 id 之前的事件前缀。
                replayed_full = replay_all(eng.events)
                for eid, proc, name, expected in reads:
                    self.assertEqual(
                        _key(replayed_full[eid]),
                        expected,
                        msg=f"full-log replay mismatch at read {eid}",
                    )

                # 同名事件表，用于"死事件"检查。
                same_name = defaultdict(list)
                for ev in eng.events:
                    if ev.kind in ("assign", "unset"):
                        same_name[(ev.proc, ev.name)].append(ev.id)

                for eid, proc, name, expected in reads:
                    ex = eng.explain(eid)

                    # 2) 切片重放一致（状态、值、环链全部相等）。
                    ex.verify()
                    self.assertEqual(_key(ex.result), expected)

                    # 3) 切片里没有被同进程后续同名事件遮蔽的死事件。
                    for sid in ex.slice_ids:
                        ev = eng.event(sid)
                        if ev.kind in ("assign", "unset"):
                            later = [
                                j
                                for j in same_name[(ev.proc, ev.name)]
                                if j > ev.id
                            ]
                            self.assertFalse(
                                any(j in ex.slice_ids for j in later),
                                msg=f"shadowed event {sid} kept in slice {ex.slice_ids}",
                            )

                    # 4) 子集最小性（空串/删除引用的良性例外见模块说明）。
                    ids = list(ex.slice_ids)
                    for r in range(len(ids) - 1, 0, -1):
                        for combo in itertools.combinations(ids, r):
                            if _render_outcome(
                                eng, [eng.event(j) for j in combo], proc, name
                            ) == expected:
                                dropped = set(ids) - set(combo)
                                self.assertTrue(
                                    _benign_dropped(eng, dropped),
                                    msg=(
                                        f"non-minimal slice {ids}: subset {combo} "
                                        f"already replays to {expected}"
                                    ),
                                )


if __name__ == "__main__":
    unittest.main()
