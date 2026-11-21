"""linker_sim 的合成数据单元测试。

注意：按任务要求，这些测试在本环境中未实际运行，不声明验证通过。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linker_sim import link  # noqa: E402


def obj(name, defines=(), undefs=()):
    return {"type": "object", "name": name, "defines": list(defines),
            "undefs": list(undefs)}


def member(name, defines=(), undefs=()):
    return {"name": name, "defines": list(defines), "undefs": list(undefs)}


def archive(name, members):
    return {"type": "archive", "name": name, "members": list(members)}


def weak(name):
    return {"name": name, "kind": "weak"}


def selected_pairs(report):
    return [(m["archive"], m["member"]) for m in report["selected_members"]]


class TestPlainObjects(unittest.TestCase):
    def test_plain_objects_always_included(self):
        report = link([
            obj("main.o", defines=["main"], undefs=["foo"]),
            obj("foo.o", defines=["foo"]),
        ])
        self.assertEqual(report["included_objects"], ["main.o", "foo.o"])
        self.assertEqual(report["selected_members"], [])
        self.assertEqual(report["symbols"]["foo"],
                         {"kind": "strong", "source": "foo.o"})
        self.assertEqual(report["unsatisfied_strong_undefs"], {})

    def test_two_plain_objects_strong_conflict(self):
        report = link([
            obj("a.o", defines=["foo"]),
            obj("b.o", defines=["foo"]),
        ])
        self.assertEqual(len(report["conflicts"]), 1)
        conflict = report["conflicts"][0]
        self.assertEqual(conflict["symbol"], "foo")
        self.assertEqual(conflict["sources"], ["a.o", "b.o"])
        self.assertEqual(conflict["kept"], "a.o")
        self.assertEqual(report["symbols"]["foo"]["source"], "a.o")


class TestArchiveExtraction(unittest.TestCase):
    def test_member_extracted_for_strong_undef(self):
        report = link([
            obj("main.o", undefs=["foo"]),
            archive("libx.a", [member("x1.o", defines=["foo"])]),
        ])
        self.assertEqual(selected_pairs(report), [("libx.a", "x1.o")])
        self.assertEqual(report["selected_members"][0]["trigger"], "foo")
        self.assertEqual(report["symbols"]["foo"]["source"], "libx.a(x1.o)")

    def test_unneeded_member_not_extracted(self):
        report = link([
            obj("main.o", undefs=["foo"]),
            archive("libx.a", [
                member("x1.o", defines=["foo"]),
                member("x2.o", defines=["unused"]),
            ]),
        ])
        self.assertEqual(selected_pairs(report), [("libx.a", "x1.o")])

    def test_single_archive_rescanned_until_stable(self):
        # m1 在归档中排在 m2 之前，但只有 m2 被抽取后才需要 m1，
        # 必须从头重扫才能补抽 m1。
        report = link([
            obj("main.o", undefs=["b"]),
            archive("libx.a", [
                member("m1.o", defines=["a"]),
                member("m2.o", defines=["b"], undefs=["a"]),
            ]),
        ])
        self.assertEqual(selected_pairs(report),
                         [("libx.a", "m2.o"), ("libx.a", "m1.o")])
        self.assertEqual(report["unsatisfied_strong_undefs"], {})

    def test_weak_undef_does_not_trigger_extraction(self):
        report = link([
            obj("main.o", undefs=[weak("foo")]),
            archive("libx.a", [member("x1.o", defines=["foo"])]),
        ])
        self.assertEqual(report["selected_members"], [])
        self.assertEqual(report["unsatisfied_strong_undefs"], {})
        self.assertEqual(list(report["unsatisfied_weak_undefs"]), ["foo"])

    def test_strong_undef_upgrades_weak_undef(self):
        report = link([
            obj("a.o", undefs=[weak("foo")]),
            obj("b.o", undefs=["foo"]),
            archive("libx.a", [member("x1.o", defines=["foo"])]),
        ])
        self.assertEqual(selected_pairs(report), [("libx.a", "x1.o")])

    def test_strong_def_replaces_weak_def(self):
        # main.o 弱定义 foo；成员因 bar 被抽取，其强定义 foo 替代弱定义。
        report = link([
            obj("main.o", defines=[weak("foo")], undefs=["bar"]),
            archive("libx.a", [member("x1.o", defines=["bar", "foo"])]),
        ])
        self.assertEqual(report["symbols"]["foo"],
                         {"kind": "strong", "source": "libx.a(x1.o)"})

    def test_weak_def_does_not_override_strong_def(self):
        report = link([
            obj("main.o", defines=["foo"], undefs=["bar"]),
            archive("libx.a",
                    [member("x1.o", defines=["bar", weak("foo")])]),
        ])
        self.assertEqual(report["symbols"]["foo"],
                         {"kind": "strong", "source": "main.o"})

    def test_conflict_with_extracted_member(self):
        report = link([
            obj("main.o", defines=["foo"], undefs=["bar"]),
            archive("libx.a", [member("x1.o", defines=["bar", "foo"])]),
        ])
        self.assertEqual(len(report["conflicts"]), 1)
        self.assertEqual(report["conflicts"][0]["sources"],
                         ["main.o", "libx.a(x1.o)"])
        self.assertEqual(report["symbols"]["foo"]["source"], "main.o")

    def test_unsatisfied_strong_undef_reported_with_referrer(self):
        report = link([
            obj("main.o", undefs=["missing"]),
            archive("libx.a", [member("x1.o", defines=["other"])]),
        ])
        self.assertEqual(report["unsatisfied_strong_undefs"],
                         {"missing": ["main.o"]})


# 相互依赖的两份归档：liba.a 的成员需要 libb.a 的符号，libb.a 的成员又
# 需要 liba.a 里另一个成员的符号。
MAIN = obj("main.o", defines=["main"], undefs=["foo"])
LIBA = archive("liba.a", [
    member("a1.o", defines=["foo"], undefs=["bar"]),
    member("a2.o", defines=["baz"]),
])
LIBB = archive("libb.a", [
    member("b1.o", defines=["bar"], undefs=["baz"]),
])


class TestArchiveGroups(unittest.TestCase):
    def test_ungrouped_leaves_back_reference_unsatisfied(self):
        report = link([MAIN, LIBA, LIBB])
        # liba.a 扫描时 baz 还不是未定义，a2.o 不会被抽取；
        # 等 libb.a 引入 baz 的强引用时，liba.a 已经越过，不回头。
        self.assertEqual(selected_pairs(report),
                         [("liba.a", "a1.o"), ("libb.a", "b1.o")])
        self.assertEqual(list(report["unsatisfied_strong_undefs"]), ["baz"])
        self.assertEqual(report["unsatisfied_strong_undefs"]["baz"],
                         ["libb.a(b1.o)"])

    def test_group_rescans_and_extracts_extra_member(self):
        report = link([MAIN, {"type": "group", "archives": [LIBA, LIBB]}])
        # 组内反复扫描：第二轮回到 liba.a，补抽 a2.o。
        self.assertEqual(selected_pairs(report),
                         [("liba.a", "a1.o"), ("libb.a", "b1.o"),
                          ("liba.a", "a2.o")])
        self.assertEqual(report["unsatisfied_strong_undefs"], {})
        self.assertEqual(report["symbols"]["baz"],
                         {"kind": "strong", "source": "liba.a(a2.o)"})

    def test_group_selects_exactly_one_extra_member(self):
        ungrouped = link([MAIN, LIBA, LIBB])
        grouped = link([MAIN, {"type": "group", "archives": [LIBA, LIBB]}])
        extra = (set(selected_pairs(grouped))
                 - set(selected_pairs(ungrouped)))
        self.assertEqual(extra, {("liba.a", "a2.o")})

    def test_archive_before_group_is_not_revisited(self):
        # liba.a 在组之前出现；组内引入的 baz 引用不会让它被重扫。
        report = link([MAIN, LIBA, {"type": "group", "archives": [LIBB]}])
        self.assertEqual(selected_pairs(report),
                         [("liba.a", "a1.o"), ("libb.a", "b1.o")])
        self.assertEqual(list(report["unsatisfied_strong_undefs"]), ["baz"])


if __name__ == "__main__":
    unittest.main()
