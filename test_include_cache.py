"""include_cache 的合成数据单元测试。

注意：按任务要求，本测试文件仅作为源码交付，未在本环境中运行，
不声称验证通过。
"""

import unittest

from include_cache import (
    IncludeCache,
    SourceTree,
    parse_directives,
    parse_include_dirs,
)


def make_base_tree():
    """构造一棵合成源码树：

    src/main.c          -- 入口，引号包含 "util.h"，尖括号包含 <lib.h>
    src/util.h          -- 嵌套尖括号包含 <conf.h>
    inc1/lib.h          -- 无嵌套
    inc1/conf.h         -- 命中点（inc1 在 inc2 之前）
    inc2/lib.h          -- 后备文件（未被选中）
    inc2/conf.h         -- 后备文件（未被选中）
    """
    return SourceTree({
        "src/main.c": '#include "util.h"\n#include <lib.h>\n',
        "src/util.h": '#include <conf.h>\n',
        "inc1/lib.h": "/* lib v1 */\n",
        "inc1/conf.h": "/* conf v1 */\n",
        "inc2/lib.h": "/* lib v2 fallback */\n",
        "inc2/conf.h": "/* conf v2 fallback */\n",
    })


OPTIONS = ["-I", "inc1", "-Iinc2", "-O2"]


class ParseTest(unittest.TestCase):
    def test_parse_include_dirs(self):
        self.assertEqual(parse_include_dirs(OPTIONS), ["inc1", "inc2"])

    def test_parse_directives(self):
        data = b'#include "a.h"\n#  include <b.h>\n// #include <c.h>\n'
        self.assertEqual(parse_directives(data),
                         [(True, "a.h"), (False, "b.h")])


class RecordAndQueryTest(unittest.TestCase):
    def setUp(self):
        self.cache = IncludeCache()
        self.tree = make_base_tree()
        self.cache.record("src/main.c", OPTIONS, self.tree)

    def test_identical_tree_needs_no_rebuild(self):
        result = self.cache.query("src/main.c", make_base_tree())
        self.assertFalse(result.rebuild)
        self.assertIsNone(result.difference)

    def test_fallback_file_may_change_alone(self):
        """未选中的后备文件（inc2/*）单独变化不触发重编。"""
        new_tree = make_base_tree()
        new_tree.add("inc2/lib.h", "/* lib v2 changed */\n")
        new_tree.add("inc2/conf.h", "/* conf v2 changed */\n")
        result = self.cache.query("src/main.c", new_tree)
        self.assertFalse(result.rebuild)

    def test_earlier_dir_new_shadowing_file(self):
        """靠前目录新出现同名文件（内容不同），命中前移 -> 重编。"""
        cache = IncludeCache()
        t1 = SourceTree({
            "src/main.c": "#include <shadow.h>\n",
            "inc2/shadow.h": "/* old hit */\n",
        })
        cache.record("src/main.c", ["-I", "inc1", "-I", "inc2"], t1)
        t2 = SourceTree({
            "src/main.c": "#include <shadow.h>\n",
            "inc1/shadow.h": "/* new shadowing file */\n",
            "inc2/shadow.h": "/* old hit */\n",
        })
        result = cache.query("src/main.c", t2)
        self.assertTrue(result.rebuild)
        self.assertEqual(result.difference.kind, "earlier-candidate-appeared")
        self.assertEqual(result.difference.path, "inc1/shadow.h")

    def test_shadowing_with_identical_bytes_still_rebuilds(self):
        """靠前目录新增与命中文件字节完全相同的同名文件，仍须重编：
        命中路径改变后，下一级相对 include 的查找起点随之改变。"""
        tree = make_base_tree()
        # main.c 改为包含 <shadow.h>，记录时只有 inc2 有
        cache = IncludeCache()
        t1 = SourceTree({
            "src/main.c": "#include <shadow.h>\n",
            "inc2/shadow.h": "/* same bytes */\n",
        })
        cache.record("src/main.c", ["-I", "inc1", "-I", "inc2"], t1)
        t2 = SourceTree({
            "src/main.c": "#include <shadow.h>\n",
            "inc1/shadow.h": "/* same bytes */\n",  # 字节相同但位置靠前
            "inc2/shadow.h": "/* same bytes */\n",
        })
        result = cache.query("src/main.c", t2)
        self.assertTrue(result.rebuild)
        self.assertEqual(result.difference.kind, "earlier-candidate-appeared")
        self.assertEqual(result.difference.path, "inc1/shadow.h")

    def test_hit_content_change_rebuilds(self):
        tree = make_base_tree()
        tree.add("inc1/conf.h", "/* conf v1 edited */\n")
        result = self.cache.query("src/main.c", tree)
        self.assertTrue(result.rebuild)
        self.assertEqual(result.difference.kind, "hit-changed")

    def test_nested_difference_chain(self):
        """嵌套 include 的差异链应从入口途经中间文件到达差异点。"""
        tree = make_base_tree()
        tree.add("inc1/conf.h", "/* conf v1 edited */\n")
        result = self.cache.query("src/main.c", tree)
        self.assertTrue(result.rebuild)
        chain = result.chain
        self.assertEqual(chain[0], "entry: src/main.c")
        # 链中应包含 util.h 对 <conf.h> 的包含，且命中 inc1/conf.h
        self.assertTrue(any("util.h" in link and "conf.h" in link
                            for link in chain))
        self.assertTrue(any("inc1/conf.h" in link for link in chain))
        # 链应自顶向下：入口 -> main.c 的 "util.h" -> util.h 的 <conf.h>
        self.assertGreaterEqual(len(chain), 3)

    def test_quoted_include_relative_to_includer(self):
        """引号包含从 includer 所在目录开始查找。"""
        cache = IncludeCache()
        t1 = SourceTree({
            "src/main.c": '#include "sub/rel.h"\n',
            "src/sub/rel.h": "/* rel */\n",
        })
        cache.record("src/main.c", [], t1)
        # 新树中该相对文件变化
        t2 = SourceTree({
            "src/main.c": '#include "sub/rel.h"\n',
            "src/sub/rel.h": "/* rel changed */\n",
        })
        self.assertTrue(cache.query("src/main.c", t2).rebuild)
        # 不变的树不重编
        t3 = SourceTree({
            "src/main.c": '#include "sub/rel.h"\n',
            "src/sub/rel.h": "/* rel */\n",
        })
        self.assertFalse(cache.query("src/main.c", t3).rebuild)

    def test_cyclic_includes_terminate(self):
        """循环包含：记录与查询都必须结束。"""
        cache = IncludeCache()
        tree = SourceTree({
            "src/main.c": '#include "a.h"\n',
            "src/a.h": '#include "b.h"\n',
            "src/b.h": '#include "a.h"\n',  # 环：b -> a -> b ...
        })
        record = cache.record("src/main.c", [], tree)  # 应能结束
        # a.h、b.h、（b 对 a 的）回边事件都被记录，但不再展开
        self.assertEqual(len(record.events), 3)
        result = cache.query("src/main.c", tree)
        self.assertFalse(result.rebuild)
        # 环上文件变化仍能被检测
        tree2 = SourceTree({
            "src/main.c": '#include "a.h"\n',
            "src/a.h": '#include "b.h"\n',
            "src/b.h": '#include "a.h"\n/* changed */\n',
        })
        self.assertTrue(cache.query("src/main.c", tree2).rebuild)

    def test_self_include_terminates(self):
        cache = IncludeCache()
        tree = SourceTree({"src/main.c": '#include "main.c"\n'})
        record = cache.record("src/main.c", [], tree)
        self.assertEqual(len(record.events), 1)
        self.assertFalse(cache.query("src/main.c", tree).rebuild)

    def test_entry_content_change(self):
        tree = make_base_tree()
        tree.add("src/main.c", '#include "util.h"\n')
        result = self.cache.query("src/main.c", tree)
        self.assertTrue(result.rebuild)
        self.assertEqual(result.difference.kind, "entry-changed")

    def test_options_change(self):
        result = self.cache.query("src/main.c", make_base_tree(),
                                  options=["-I", "inc1", "-Iinc2", "-O3"])
        self.assertTrue(result.rebuild)
        self.assertEqual(result.difference.kind, "options-changed")

    def test_unresolved_include_appearing_later(self):
        """记录时未命中的 include，新树中出现候选文件 -> 重编。"""
        cache = IncludeCache()
        t1 = SourceTree({"src/main.c": "#include <maybe.h>\n"})
        cache.record("src/main.c", ["-I", "inc1"], t1)
        t2 = SourceTree({
            "src/main.c": "#include <maybe.h>\n",
            "inc1/maybe.h": "/* now exists */\n",
        })
        result = cache.query("src/main.c", t2)
        self.assertTrue(result.rebuild)
        self.assertEqual(result.difference.kind, "earlier-candidate-appeared")

    def test_hit_removed(self):
        # 删除命中文件：构造一棵没有 inc1/conf.h 的新树
        tree2 = SourceTree({
            "src/main.c": '#include "util.h"\n#include <lib.h>\n',
            "src/util.h": '#include <conf.h>\n',
            "inc1/lib.h": "/* lib v1 */\n",
            "inc2/conf.h": "/* conf v2 fallback */\n",
        })
        result = self.cache.query("src/main.c", tree2)
        self.assertTrue(result.rebuild)
        self.assertEqual(result.difference.kind, "hit-missing")

    def test_rebuild_set_multiple_entries(self):
        cache = IncludeCache()
        t1 = SourceTree({
            "src/a.c": "#include <x.h>\n",
            "src/b.c": "#include <y.h>\n",
            "inc/x.h": "/* x */\n",
            "inc/y.h": "/* y */\n",
        })
        cache.record("src/a.c", ["-I", "inc"], t1)
        cache.record("src/b.c", ["-I", "inc"], t1)
        t2 = SourceTree({
            "src/a.c": "#include <x.h>\n",
            "src/b.c": "#include <y.h>\n",
            "inc/x.h": "/* x changed */\n",
            "inc/y.h": "/* y */\n",
        })
        rebuilds = cache.rebuild_set(t2)
        self.assertEqual(set(rebuilds), {"src/a.c"})
        self.assertTrue(rebuilds["src/a.c"].chain)


if __name__ == "__main__":
    unittest.main()
