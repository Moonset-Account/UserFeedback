"""Unit tests for the secure help-center draft boundary.

All fixtures live in temporary directories.  Every test carries a stable
case_id (in its docstring) that README.md maps back to the requirements.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import secure_docs
from secure_docs import SecureDocStore, UserDirectory

USERS = {
    "alice": {"workspaces": ["ws-alpha"], "languages": ["fr"]},
    "erin": {"workspaces": ["ws-alpha"], "languages": ["fr"]},
    "bob": {"workspaces": ["ws-alpha"], "languages": ["de"]},
    "carol": {"workspaces": ["ws-beta"], "languages": ["fr"]},
    "dave": {"workspaces": [], "languages": []},
}

ALPHA_INDEX = {
    "documents": {
        "intro": {"path": "drafts/intro.md", "language": "fr", "version": 1},
        "guide": {"path": "drafts/guide.md", "language": "de", "version": 1},
    }
}

BETA_INDEX = {
    "documents": {
        "intro": {"path": "drafts/intro.md", "language": "fr", "version": 1},
    }
}


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class BoundaryTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="secure-docs-")
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

        _write(os.path.join(self.root, "users.json"), json.dumps(USERS))
        _write(os.path.join(self.root, "outside", "secret.md"),
               "outside secret v1\n")

        _write(os.path.join(self.root, "ws-alpha", "index.json"),
               json.dumps(ALPHA_INDEX, indent=2))
        _write(os.path.join(self.root, "ws-alpha", "drafts", "intro.md"),
               "alpha intro v1\n")
        _write(os.path.join(self.root, "ws-alpha", "drafts", "guide.md"),
               "alpha guide v1\n")

        _write(os.path.join(self.root, "ws-beta", "index.json"),
               json.dumps(BETA_INDEX, indent=2))
        _write(os.path.join(self.root, "ws-beta", "drafts", "intro.md"),
               "beta intro v1\n")

        self.users = UserDirectory.from_file(
            os.path.join(self.root, "users.json"))
        self.store = SecureDocStore(self.root, self.users)

    # -- helpers ------------------------------------------------------------

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def read_file(self, *parts):
        with open(self.path(*parts), "rb") as fh:
            return fh.read()

    def assert_intact(self, expected_text, *parts):
        self.assertEqual(self.read_file(*parts),
                         expected_text.encode("utf-8"))

    def index_version(self, workspace, doc_id):
        with open(self.path(workspace, "index.json"),
                  "r", encoding="utf-8") as fh:
            return json.load(fh)["documents"][doc_id]["version"]

    # -- tests ----------------------------------------------------------------

    def test_normal_edit_ok(self):
        """case_id=normal_edit_ok: member+language editor edits own draft."""
        handle = self.store.open_document("alice", "ws-alpha", "intro")
        self.assertEqual(handle.read("alice"), "alpha intro v1\n")
        self.assertEqual(handle.current_version(), 1)
        new_version = handle.write("alice", "alpha intro v2\n", 1)
        self.assertEqual(new_version, 2)
        self.assertEqual(handle.read("alice"), "alpha intro v2\n")
        self.assertEqual(handle.current_version(), 2)
        # version is persisted so later processes see it too
        self.assertEqual(self.index_version("ws-alpha", "intro"), 2)
        self.assert_intact("alpha intro v2\n",
                           "ws-alpha", "drafts", "intro.md")

    def test_read_requires_workspace_membership(self):
        """case_id=read_requires_membership: non-member cannot read."""
        with self.assertRaises(secure_docs.NotWorkspaceMemberError):
            self.store.read("dave", "ws-alpha", "intro")
        self.assert_intact("alpha intro v1\n",
                           "ws-alpha", "drafts", "intro.md")

    def test_write_requires_language_editor(self):
        """case_id=write_requires_language_editor: member editing another
        language's draft is rejected and the draft stays intact."""
        with self.assertRaises(secure_docs.NotLanguageEditorError):
            self.store.write("bob", "ws-alpha", "intro", "hijack\n", 1)
        self.assert_intact("alpha intro v1\n",
                           "ws-alpha", "drafts", "intro.md")
        self.assertEqual(self.index_version("ws-alpha", "intro"), 1)
        # bob *can* edit the German draft he is responsible for
        self.store.write("bob", "ws-alpha", "guide", "alpha guide v2\n", 1)
        self.assert_intact("alpha guide v2\n",
                           "ws-alpha", "drafts", "guide.md")

    def test_version_conflict_second_writer_loses(self):
        """case_id=version_conflict_same_version: two editors contend for
        version 1; the second write fails and the first write survives."""
        h_alice = self.store.open_document("alice", "ws-alpha", "intro")
        h_erin = self.store.open_document("erin", "ws-alpha", "intro")
        self.assertEqual(h_alice.write("alice", "alice edit v2\n", 1), 2)
        with self.assertRaises(secure_docs.VersionConflictError):
            h_erin.write("erin", "erin edit v2\n", 1)
        self.assert_intact("alice edit v2\n",
                           "ws-alpha", "drafts", "intro.md")
        self.assertEqual(self.index_version("ws-alpha", "intro"), 2)
        # erin can retry against the new version
        self.assertEqual(h_erin.write("erin", "erin edit v3\n", 2), 3)
        self.assert_intact("erin edit v3\n",
                           "ws-alpha", "drafts", "intro.md")

    def test_same_name_docs_isolated_across_workspaces(self):
        """case_id=same_name_cross_workspace: doc id 'intro' exists in two
        workspaces; editing one never touches the other."""
        self.store.write("alice", "ws-alpha", "intro", "alpha intro v2\n", 1)
        self.assert_intact("alpha intro v2\n",
                           "ws-alpha", "drafts", "intro.md")
        self.assert_intact("beta intro v1\n",
                           "ws-beta", "drafts", "intro.md")
        self.assertEqual(self.index_version("ws-beta", "intro"), 1)
        # carol is a member of ws-beta only and reads her own intro
        self.assertEqual(self.store.read("carol", "ws-beta", "intro"),
                         "beta intro v1\n")
        # alice is not a member of ws-beta even though the doc id exists
        with self.assertRaises(secure_docs.NotWorkspaceMemberError):
            self.store.read("alice", "ws-beta", "intro")
        self.assert_intact("beta intro v1\n",
                           "ws-beta", "drafts", "intro.md")

    def _install_alpha_entry(self, doc_id, path):
        index = dict(ALPHA_INDEX)
        index["documents"] = dict(ALPHA_INDEX["documents"])
        index["documents"][doc_id] = {
            "path": path, "language": "fr", "version": 1,
        }
        _write(self.path("ws-alpha", "index.json"),
               json.dumps(index, indent=2))
        self.store.reload_index("ws-alpha")

    def test_symlink_directory_component_rejected(self):
        """case_id=symlink_dir_component_rejected: a symlinked directory
        between workspace root and target is refused; target intact."""
        os.symlink(self.path("outside"), self.path("ws-alpha", "linkdir"))
        self._install_alpha_entry("evil", "linkdir/secret.md")
        with self.assertRaises(secure_docs.UnsafePathError):
            self.store.read("alice", "ws-alpha", "evil")
        with self.assertRaises(secure_docs.UnsafePathError):
            self.store.write("alice", "ws-alpha", "evil", "pwned\n", 1)
        self.assert_intact("outside secret v1\n", "outside", "secret.md")

    def test_symlink_target_file_rejected(self):
        """case_id=symlink_file_target_rejected: the document itself being
        a symlink is refused; the outside file stays intact."""
        os.symlink(self.path("outside", "secret.md"),
                   self.path("ws-alpha", "drafts", "link.md"))
        self._install_alpha_entry("evil", "drafts/link.md")
        with self.assertRaises(secure_docs.UnsafePathError):
            self.store.read("alice", "ws-alpha", "evil")
        with self.assertRaises(secure_docs.UnsafePathError):
            self.store.write("alice", "ws-alpha", "evil", "pwned\n", 1)
        self.assert_intact("outside secret v1\n", "outside", "secret.md")

    def test_non_regular_target_rejected(self):
        """case_id=non_regular_target_rejected: index pointing at a
        directory is refused."""
        self._install_alpha_entry("evil", "drafts")
        with self.assertRaises(secure_docs.UnsafePathError):
            self.store.read("alice", "ws-alpha", "evil")
        with self.assertRaises(secure_docs.UnsafePathError):
            self.store.write("alice", "ws-alpha", "evil", "pwned\n", 1)
        self.assertTrue(os.path.isdir(self.path("ws-alpha", "drafts")))

    def test_stale_reference_after_index_reload(self):
        """case_id=stale_reference_after_reload: handles opened before a
        reload are invalid afterwards; the draft is untouched."""
        handle = self.store.open_document("alice", "ws-alpha", "intro")
        self.store.reload_index("ws-alpha")
        with self.assertRaises(secure_docs.StaleReferenceError):
            handle.read("alice")
        with self.assertRaises(secure_docs.StaleReferenceError):
            handle.write("alice", "stale write\n", 1)
        self.assert_intact("alpha intro v1\n",
                           "ws-alpha", "drafts", "intro.md")
        self.assertEqual(self.index_version("ws-alpha", "intro"), 1)
        # a freshly opened handle works again
        fresh = self.store.open_document("alice", "ws-alpha", "intro")
        self.assertEqual(fresh.read("alice"), "alpha intro v1\n")

    def test_index_path_traversal_rejected(self):
        """case_id=index_path_traversal_rejected: even a (corrupted) trusted
        index cannot escape the workspace via '..'."""
        self._install_alpha_entry("evil", "../outside/secret.md")
        with self.assertRaises(secure_docs.UnsafePathError):
            self.store.read("alice", "ws-alpha", "evil")
        with self.assertRaises(secure_docs.UnsafePathError):
            self.store.write("alice", "ws-alpha", "evil", "pwned\n", 1)
        self.assert_intact("outside secret v1\n", "outside", "secret.md")

    def test_unknown_workspace_and_document(self):
        """case_id=unknown_workspace_and_document: bad ids are rejected."""
        with self.assertRaises(secure_docs.UnknownWorkspaceError):
            self.store.read("alice", "ws-ghost", "intro")
        with self.assertRaises(secure_docs.UnknownWorkspaceError):
            self.store.read("alice", "../outside", "secret")
        with self.assertRaises(secure_docs.UnknownDocumentError):
            self.store.read("alice", "ws-alpha", "no-such-doc")
        self.assert_intact("alpha intro v1\n",
                           "ws-alpha", "drafts", "intro.md")

    def test_cli_read_write_round_trip(self):
        """case_id=cli_round_trip: app.py read/write/reload over subprocess;
        conflicting write exits 4 and leaves the draft intact."""
        app = os.path.join(PROJECT_ROOT, "app.py")

        def run_cli(*argv):
            return subprocess.run(
                [sys.executable, app, "--root", self.root] + list(argv),
                capture_output=True, text=True, timeout=30)

        res = run_cli("read", "--workspace", "ws-alpha",
                      "--user", "alice", "--doc", "intro")
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["content"], "alpha intro v1\n")
        self.assertEqual(payload["version"], 1)

        res = run_cli("write", "--workspace", "ws-alpha",
                      "--user", "alice", "--doc", "intro",
                      "--expected-version", "1",
                      "--content", "alpha intro v2\n")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout)["version"], 2)

        # stale expected version is a conflict (exit 4), draft intact
        res = run_cli("write", "--workspace", "ws-alpha",
                      "--user", "erin", "--doc", "intro",
                      "--expected-version", "1",
                      "--content", "erin clobber\n")
        self.assertEqual(res.returncode, 4, res.stdout)
        self.assertEqual(json.loads(res.stdout)["error"],
                         "VersionConflictError")
        self.assert_intact("alpha intro v2\n",
                           "ws-alpha", "drafts", "intro.md")

        # non-editor is forbidden (exit 3)
        res = run_cli("write", "--workspace", "ws-alpha",
                      "--user", "bob", "--doc", "intro",
                      "--expected-version", "2",
                      "--content", "bob clobber\n")
        self.assertEqual(res.returncode, 3, res.stdout)
        self.assert_intact("alpha intro v2\n",
                          "ws-alpha", "drafts", "intro.md")

        res = run_cli("reload", "--workspace", "ws-alpha")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(json.loads(res.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
