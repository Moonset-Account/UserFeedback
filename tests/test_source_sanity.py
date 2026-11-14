"""Static self-checks on delivered source files (no execution of app code).

These keep the delivered tree honest: every module compiles, the CLI exposes the
three documented subcommands, and the verification wrapper really uses the
required discovery entry point and the count checks.
"""

import ast
import os
import py_compile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SourceSanityTests(unittest.TestCase):
    def test_every_delivered_module_compiles(self) -> None:
        import tempfile

        compiled = []
        with tempfile.TemporaryDirectory() as tmp:
            for directory in ("quota", "tests", "tools"):
                for name in sorted(os.listdir(os.path.join(ROOT, directory))):
                    if name.endswith(".py"):
                        path = os.path.join(ROOT, directory, name)
                        cfile = os.path.join(tmp, f"{directory}_{name}.pyc")
                        py_compile.compile(path, doraise=True, cfile=cfile)
                        compiled.append(path)
            app = os.path.join(ROOT, "app.py")
            py_compile.compile(app, doraise=True, cfile=os.path.join(tmp, "app.pyc"))
            compiled.append(app)
        self.assertGreaterEqual(len(compiled), 10)

    def test_cli_declares_the_three_subcommands(self) -> None:
        with open(os.path.join(ROOT, "app.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        add_parser_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_parser"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ]
        names = {node.args[0].value for node in add_parser_calls}
        self.assertEqual(names, {"decide", "batch", "demo"})

    def test_wrapper_uses_required_discovery_and_counts(self) -> None:
        with open(os.path.join(ROOT, "tools", "local_verify.py"), encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        # The contract the delivery rules demand of the wrapper.
        self.assertIn("discover", attributes)
        self.assertIn("countTestCases", attributes)
        self.assertIn("defaultTestLoader", attributes)
        self.assertIn("executed_not_skipped", attributes)
        self.assertIn("RecordingResult", names)

    def test_no_third_party_imports(self) -> None:
        allowed = set(__import__("sys").stdlib_module_names)
        local_packages = {"quota", "tools", "tests"}
        offenders = []
        for directory in ("quota", "tests", "tools"):
            for name in sorted(os.listdir(os.path.join(ROOT, directory))):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(ROOT, directory, name)
                with open(path, encoding="utf-8") as handle:
                    tree = ast.parse(handle.read())
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            root = alias.name.split(".")[0]
                            if root not in allowed and root not in local_packages:
                                offenders.append((path, alias.name))
                    elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                        root = node.module.split(".")[0]
                        if root not in allowed and root not in local_packages:
                            offenders.append((path, node.module))
        self.assertEqual(offenders, [], f"non-stdlib imports: {offenders}")


class TraceabilityTests(unittest.TestCase):
    """Every case id the README cites must resolve to a real test or demo case."""

    def test_readme_unit_ids_resolve(self) -> None:
        import importlib
        import re

        readme = os.path.join(ROOT, "README.md")
        with open(readme, encoding="utf-8") as handle:
            text = handle.read()
        pattern = re.compile(r"`tests/([a-z_]+)\.py::([A-Za-z]+)::(test_[a-z_]+)`")
        references = set(pattern.findall(text))
        self.assertGreaterEqual(len(references), 12, "README should cite the suite")

        missing = []
        for module_name, class_name, method_name in sorted(references):
            module = importlib.import_module(f"tests.{module_name}")
            klass = getattr(module, class_name, None)
            if klass is None or not hasattr(klass, method_name):
                missing.append(f"{module_name}.{class_name}.{method_name}")
        self.assertEqual(missing, [], f"README cites missing tests: {missing}")

    def test_readme_demo_ids_exist(self) -> None:
        import re

        from quota.demo import SCENARIOS

        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
            text = handle.read()
        cited = set(re.findall(r"`(demo-[a-z-]+)`", text))
        actual = {case_id for case_id, _ in SCENARIOS}
        self.assertEqual(cited - actual, set(), "README cites unknown demo scenario ids")
        self.assertGreaterEqual(len(cited), 4)


if __name__ == "__main__":
    unittest.main()
