"""Unit tests for diff parser."""

import unittest
from src.diff_parser import parse_diff, DiffFile, DiffLine, get_target_path
from tests.synthetic_values import assemble


class TestDiffParserBasic(unittest.TestCase):
    """Test basic diff parsing functionality."""

    def test_parse_empty_diff(self):
        files = parse_diff("")
        self.assertEqual(len(files), 0)

    def test_parse_single_file_diff(self):
        diff = """--- a/src/config.py
+++ b/src/config.py
@@ -1,2 +1,4 @@
 DATABASE_URL = "postgresql://localhost/mydb"
+AWS_ACCESS_KEY_ID = "AKIAIO...MPLE"
+AWS_SECRET_ACCESS_KEY="wJal..."
 DEBUG = False
"""
        files = parse_diff(diff)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].target_path, "src/config.py")
        self.assertEqual(files[0].source_path, "src/config.py")

    def test_parse_added_lines(self):
        diff = """--- a/src/config.py
+++ b/src/config.py
@@ -1,2 +1,4 @@
 DATABASE_URL = "postgresql://localhost/mydb"
+AWS_ACCESS_KEY_ID = "AKIAIO...MPLE"
+AWS_SECRET_ACCESS_KEY="wJal..."
 DEBUG = False
"""
        files = parse_diff(diff)
        self.assertGreaterEqual(len(files[0].added_lines), 2)

    def test_parse_removed_lines(self):
        diff = """--- a/src/config.py
+++ b/src/config.py
@@ -1,3 +1,2 @@
 DATABASE_URL = "postgresql://localhost/mydb"
-OLD_VAR = "removed"
 DEBUG = False
"""
        files = parse_diff(diff)
        self.assertGreaterEqual(len(files[0].removed_lines), 1)

    def test_parse_multiple_files(self):
        diff = """--- a/src/config.py
+++ b/src/config.py
@@ -1,2 +1,3 @@
 DATABASE_URL = "postgresql://localhost/mydb"
+NEW_VAR = "added"
 DEBUG = False
--- a/.env
+++ b/.env
@@ -1,1 +1,2 @@
 SECRET_KEY=abc123
+NEW_SECRET=def456
"""
        files = parse_diff(diff)
        self.assertGreaterEqual(len(files), 2)

    def test_parse_new_file(self):
        diff = f"""--- /dev/null
+++ b/.env.production
@@ -0,0 +1,3 @@
+STRIPE_SECRET_KEY={assemble("sk", "_live_", "abc123def456")}
+DB_HOST=prod-db.internal
+DB_PASSWORD=***"""
        files = parse_diff(diff)
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].is_new)
        self.assertEqual(files[0].target_path, ".env.production")


class TestDiffParserLineNumbers(unittest.TestCase):
    """Test that line numbers are tracked correctly."""

    def test_added_lines_have_line_numbers(self):
        diff = """--- a/src/config.py
+++ b/src/config.py
@@ -1,2 +1,4 @@
 DATABASE_URL = "postgresql://localhost/mydb"
+AWS_ACCESS_KEY_ID = "AKIAIO...MPLE"
+AWS_SECRET_ACCESS_KEY="wJal..."
 DEBUG = False
"""
        files = parse_diff(diff)
        for line in files[0].added_lines:
            self.assertGreater(line.line_number, 0)


class TestDiffParserEdgeCases(unittest.TestCase):
    """Test edge cases in diff parsing."""

    def test_binary_file_marker(self):
        diff = """--- a/image.png
+++ b/image.png
Binary files a/image.png and b/image.png differ
--- a/src/config.py
+++ b/src/config.py
@@ -1,1 +1,2 @@
 OLD_LINE
+NEW_LINE
"""
        files = parse_diff(diff)
        self.assertGreaterEqual(len(files), 1)

    def test_empty_lines_in_diff(self):
        diff = """--- a/src/config.py
+++ b/src/config.py
@@ -1,2 +1,3 @@
 LINE_ONE
+
 LINE_THREE
"""
        files = parse_diff(diff)
        # Should parse without crashing
        self.assertGreaterEqual(len(files), 0)


class TestGetTargetPath(unittest.TestCase):
    """Test get_target_path helper."""

    def test_normal_diff(self):
        df = DiffFile(source_path="src/config.py", target_path="src/config.py")
        self.assertEqual(get_target_path(df), "src/config.py")

    def test_new_file(self):
        df = DiffFile(source_path="/dev/null", target_path=".env.production", is_new=True)
        self.assertEqual(get_target_path(df), ".env.production")

    def test_deleted_file(self):
        df = DiffFile(source_path="src/old.py", target_path="/dev/null", is_deleted=True)
        self.assertEqual(get_target_path(df), "src/old.py")


if __name__ == "__main__":
    unittest.main()
