"""边界与静态校验：非法配置、空程序、原始指令序列输入。"""

import unittest

from collectives import (
    Executor,
    Group,
    Handle,
    Initiate,
    Program,
    TagMismatchError,
    Wait,
)


class ValidationTest(unittest.TestCase):
    def test_duplicate_group_name_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Executor(
                [Group("g", [0, 1]), Group("g", [0, 1])],
                {0: Program(0), 1: Program(1)},
            )

    def test_duplicate_members_in_group_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Group("g", [0, 0, 1])

    def test_empty_group_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Group("g", [])

    def test_missing_program_for_group_member_rejected(self) -> None:
        group = Group("g", [0, 1])
        with self.assertRaises(ValueError):
            Executor([group], {0: Program(0)})

    def test_initiate_unknown_group_rejected(self) -> None:
        group = Group("g", [0])
        program = Program(0)
        program.initiate("other", "t")
        with self.assertRaises(ValueError):
            Executor([group], {0: program})

    def test_initiate_by_non_member_rejected(self) -> None:
        group = Group("g", [0])
        outsider = Program(1)
        outsider.initiate("g", "t")
        with self.assertRaises(ValueError):
            Executor([group], {0: Program(0), 1: outsider})

    def test_wait_foreign_handle_rejected(self) -> None:
        program = Program(0)
        foreign = Handle(rank=1, local_index=0)
        with self.assertRaises(ValueError):
            program.wait(foreign)

    def test_wait_unknown_handle_rejected(self) -> None:
        program = Program(0)
        program.initiate("g", "t")
        with self.assertRaises(ValueError):
            program.wait(Handle(rank=0, local_index=99))

    def test_program_registered_under_wrong_rank_rejected(self) -> None:
        group = Group("g", [0, 1])
        with self.assertRaises(ValueError):
            Executor([group], {0: Program(1), 1: Program(0)})

    def test_empty_programs_finish_successfully(self) -> None:
        group = Group("g", [0, 1])
        result = Executor([group], {0: Program(0), 1: Program(1)}).run()
        self.assertEqual(result.ops_by_rank, {0: (), 1: ()})
        self.assertEqual(len(result.events), 2)

    def test_raw_instruction_sequence_accepted(self) -> None:
        group = Group("g", [0, 1])
        h0 = Handle(rank=0, local_index=0)
        h1 = Handle(rank=1, local_index=0)
        raw_programs = {
            0: (Initiate(group="g", tag="t", handle=h0), Wait(handle=h0)),
            1: (Initiate(group="g", tag="t", handle=h1), Wait(handle=h1)),
        }
        result = Executor([group], raw_programs).run()
        self.assertEqual(result.op_by_handle(h0).members, (0, 1))
        self.assertEqual(result.op_by_handle(h1).tag, "t")

    def test_raw_sequence_with_bad_wait_rejected(self) -> None:
        group = Group("g", [0])
        raw = (Wait(handle=Handle(rank=0, local_index=3)),)
        with self.assertRaises(ValueError):
            Executor([group], {0: raw}).run()

    def test_integer_tags_compared_by_value(self) -> None:
        group = Group("g", [0, 1])
        p0 = Program(0)
        p0.initiate("g", 42)
        p1 = Program(1)
        p1.initiate("g", 42)
        result = Executor([group], {0: p0, 1: p1}).run()
        self.assertEqual(next(iter(result.ops_by_rank[0])).tag, 42)

        p0 = Program(0)
        p0.initiate("g", 42)
        p1 = Program(1)
        p1.initiate("g", 7)
        with self.assertRaises(TagMismatchError):
            Executor([group], {0: p0, 1: p1}).run()

    def test_extra_rank_not_in_any_group_runs_alone(self) -> None:
        group = Group("g", [0])
        solo = Group("solo", [1])
        p0 = Program(0)
        h0 = p0.initiate("g", "t")
        p0.wait(h0)
        p1 = Program(1)
        h1 = p1.initiate("solo", "t")
        p1.wait(h1)
        result = Executor([group, solo], {0: p0, 1: p1}).run()
        self.assertEqual(result.op_by_handle(h0).group, "g")
        self.assertEqual(result.op_by_handle(h1).group, "solo")


if __name__ == "__main__":
    unittest.main()
