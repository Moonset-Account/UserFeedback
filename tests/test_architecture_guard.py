"""架构守卫：强制“采集核心”与样例生成器/测试真值分离。

边界定义：
- 采集核心 = collector / parser / identity / fetcher / storage / models / errors，
  这些模块禁止 import samplegen、禁止出现夹具目录与测试真值字段；
- webapp 是编排层（HTTP 服务 + 样例托管 + 任务调度），允许惰性导入
  samplegen 来生成虚构夹具，但同样禁止接触测试真值字段，且必须在
  函数内部惰性导入，采集核心代码路径不经过它。
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = ROOT / "feedcollector"
CORE_MODULES = {
    "collector.py", "parser.py", "identity.py", "fetcher.py",
    "storage.py", "models.py", "errors.py", "__init__.py",
}


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


class ArchitectureGuardTests(unittest.TestCase):
    def test_collector_core_never_imports_samplegen(self):
        offenders = []
        for name in CORE_MODULES:
            path = COLLECTOR / name
            if path.exists() and "samplegen" in _imports_of(path):
                offenders.append(name)
        self.assertEqual(
            offenders, [],
            f"采集核心模块违规依赖样例生成器：{offenders}；"
            "采集只能经 HTTP/文件接口获得样例数据")

    def test_core_modules_have_no_fixture_or_truth_references(self):
        for name in CORE_MODULES:
            path = COLLECTOR / name
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("fixtures", text,
                             f"{name} 不应直接引用样例夹具目录")
            self.assertNotIn("expected", text,
                             f"{name} 不应读取测试真值字段")

    def test_webapp_samplegen_import_is_lazy_only(self):
        """编排层即使导入样例生成器，也只能在函数内惰性导入，
        保证 import feedcollector.collector 不牵连 samplegen。"""
        path = COLLECTOR / "webapp.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        top_level_samplegen = []
        for node in tree.body:  # 只检查模块顶层语句
            if isinstance(node, ast.ImportFrom) and node.module and \
                    node.module.startswith("samplegen"):
                top_level_samplegen.append(node.module)
            if isinstance(node, ast.Import):
                top_level_samplegen += [
                    a.name for a in node.names if a.name.startswith("samplegen")]
        self.assertEqual(top_level_samplegen, [],
                         "webapp 对 samplegen 的导入必须放在函数内部（惰性导入）")

    def test_webapp_never_touches_truth_fields(self):
        text = (COLLECTOR / "webapp.py").read_text(encoding="utf-8")
        self.assertNotIn("expected", text,
                         "编排层不得读取样例清单中的测试真值字段")

    def test_expected_truth_only_lives_in_samplegen_and_tests(self):
        for path in ROOT.rglob("*.py"):
            rel = path.relative_to(ROOT)
            if rel.parts[0] == "feedcollector":
                self.assertNotIn(
                    "expected", path.read_text(encoding="utf-8"),
                    f"{rel} 不得包含测试真值")


if __name__ == "__main__":
    unittest.main()
