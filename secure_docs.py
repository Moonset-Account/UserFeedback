"""File-operation boundary for localized help-center draft editing.

Callers address documents by (workspace, logical document ID).  A trusted
per-workspace index (``index.json`` inside the workspace directory) maps
logical IDs to relative file paths; callers never supply disk paths.

Rules enforced here:

* reads require workspace membership;
* writes additionally require that the editor is responsible for the
  document's language;
* writes must match the expected content version (optimistic concurrency);
* any symlink component between the workspace root and the target, or a
  target that is not a regular file, is rejected;
* after an index reload, previously issued document handles are stale and
  every operation on them is rejected.

Only the Python 3 standard library is used.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SecureDocsError(Exception):
    """Base class for all boundary errors."""


class UnknownWorkspaceError(SecureDocsError):
    """The workspace does not exist under the root (or the name is invalid)."""


class UnknownDocumentError(SecureDocsError):
    """The logical document ID is not present in the workspace index."""


class IndexFormatError(SecureDocsError):
    """The trusted index is malformed."""


class NotWorkspaceMemberError(SecureDocsError):
    """The user is not a member of the workspace."""


class NotLanguageEditorError(SecureDocsError):
    """The user is not responsible for the document's language."""


class VersionConflictError(SecureDocsError):
    """The expected content version does not match the current version."""


class StaleReferenceError(SecureDocsError):
    """The document handle predates an index reload."""


class UnsafePathError(SecureDocsError):
    """The indexed path escapes the boundary or is not a regular file."""


# ---------------------------------------------------------------------------
# User directory (local adapter standing in for the external identity system)
# ---------------------------------------------------------------------------


class UserDirectory:
    """Workspace membership and language responsibility records.

    ``users`` maps user name -> {"workspaces": [...], "languages": [...]}.
    """

    def __init__(self, users):
        if not isinstance(users, dict):
            raise IndexFormatError("users file must contain a JSON object")
        self._users = dict(users)

    @classmethod
    def from_file(cls, path):
        with open(path, "r", encoding="utf-8") as fh:
            return cls(json.load(fh))

    def is_member(self, user, workspace):
        record = self._users.get(user)
        return bool(record) and workspace in record.get("workspaces", [])

    def edits_language(self, user, language):
        record = self._users.get(user)
        return bool(record) and language in record.get("languages", [])


# ---------------------------------------------------------------------------
# Path resolution inside one workspace
# ---------------------------------------------------------------------------


def _is_simple_name(name):
    return (
        isinstance(name, str)
        and name not in ("", ".", "..")
        and "/" not in name
        and "\\" not in name
        and not os.path.isabs(name)
    )


def resolve_within(base_dir, rel_path):
    """Resolve ``rel_path`` under ``base_dir`` enforcing the boundary.

    Rejects absolute paths, ``.``/``..``/empty components, symlink
    components between the base and the target, and targets that are not
    regular files.  Returns the final path (still expressed via
    ``base_dir``; no symlink is followed to get there).
    """
    if not isinstance(rel_path, str) or not rel_path:
        raise UnsafePathError("empty document path")
    if os.path.isabs(rel_path):
        raise UnsafePathError("absolute path not allowed: %r" % (rel_path,))
    parts = rel_path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise UnsafePathError("illegal path components in %r" % (rel_path,))

    current = base_dir
    for part in parts[:-1]:
        current = os.path.join(current, part)
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            raise UnsafePathError(
                "missing directory component in %r" % (rel_path,))
        if stat.S_ISLNK(st.st_mode):
            raise UnsafePathError(
                "symlink directory component in %r" % (rel_path,))
        if not stat.S_ISDIR(st.st_mode):
            raise UnsafePathError(
                "non-directory component in %r" % (rel_path,))

    final = os.path.join(current, parts[-1])
    try:
        st = os.lstat(final)
    except FileNotFoundError:
        raise UnsafePathError("document file does not exist: %r" % (rel_path,))
    if stat.S_ISLNK(st.st_mode):
        raise UnsafePathError("document is a symlink: %r" % (rel_path,))
    if not stat.S_ISREG(st.st_mode):
        raise UnsafePathError(
            "document is not a regular file: %r" % (rel_path,))
    return final


def _atomic_replace(final_path, data):
    """Write ``data`` (bytes) to ``final_path`` atomically."""
    directory = os.path.dirname(final_path)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-",
                                    suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, final_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Workspace index
# ---------------------------------------------------------------------------


def _parse_entry(doc_id, entry):
    if not isinstance(entry, dict):
        raise IndexFormatError("index entry for %r is not an object" % doc_id)
    rel_path = entry.get("path")
    language = entry.get("language")
    version = entry.get("version")
    if not isinstance(rel_path, str) or not rel_path:
        raise IndexFormatError("index entry for %r has no path" % doc_id)
    if not isinstance(language, str) or not language:
        raise IndexFormatError("index entry for %r has no language" % doc_id)
    if (not isinstance(version, int)) or isinstance(version, bool) \
            or version < 0:
        raise IndexFormatError(
            "index entry for %r has a bad version" % doc_id)
    return rel_path, language, version


class _WorkspaceIndex:
    __slots__ = ("workspace", "directory", "index_path", "generation",
                 "documents")

    def __init__(self, workspace, directory, index_path, generation,
                 documents):
        self.workspace = workspace
        self.directory = directory
        self.index_path = index_path
        self.generation = generation
        self.documents = documents


# ---------------------------------------------------------------------------
# Store and document handles
# ---------------------------------------------------------------------------


class SecureDocStore:
    """Boundary object rooted at a directory that holds workspaces."""

    INDEX_FILENAME = "index.json"

    def __init__(self, root, users):
        if not isinstance(users, UserDirectory):
            raise TypeError("users must be a UserDirectory")
        self._root = os.path.abspath(root)
        self._users = users
        self._indexes = {}
        self._generation_counter = 0

    @property
    def root(self):
        return self._root

    # -- workspace / index management ------------------------------------

    def _workspace_dir(self, workspace):
        if not _is_simple_name(workspace):
            raise UnknownWorkspaceError(
                "invalid workspace name: %r" % (workspace,))
        path = os.path.join(self._root, workspace)
        if os.path.islink(path) or not os.path.isdir(path):
            raise UnknownWorkspaceError(
                "unknown workspace: %r" % (workspace,))
        return path

    def load_workspace(self, workspace):
        """(Re)load the workspace index from disk.

        Every reload bumps the index generation, which invalidates all
        document handles issued against earlier generations.
        """
        ws_dir = self._workspace_dir(workspace)
        index_path = os.path.join(ws_dir, self.INDEX_FILENAME)
        if os.path.islink(index_path) or not os.path.isfile(index_path):
            raise UnknownWorkspaceError(
                "workspace %r has no usable index" % (workspace,))
        with open(index_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        documents = raw.get("documents") if isinstance(raw, dict) else None
        if not isinstance(documents, dict):
            raise IndexFormatError(
                "index for workspace %r has no 'documents' object"
                % (workspace,))
        self._generation_counter += 1
        index = _WorkspaceIndex(workspace, ws_dir, index_path,
                                self._generation_counter, documents)
        self._indexes[workspace] = index
        return index

    def reload_index(self, workspace):
        """Reload the index; all existing handles for it become stale."""
        return self.load_workspace(workspace)

    def _index(self, workspace):
        index = self._indexes.get(workspace)
        if index is None:
            index = self.load_workspace(workspace)
        return index

    def _persist_index(self, index):
        payload = json.dumps({"documents": index.documents},
                             indent=2, sort_keys=True)
        _atomic_replace(index.index_path, payload.encode("utf-8"))

    # -- public operations -------------------------------------------------

    def open_document(self, user, workspace, doc_id):
        """Return a handle for (workspace, doc_id); membership is required."""
        index = self._index(workspace)
        if not self._users.is_member(user, workspace):
            raise NotWorkspaceMemberError(
                "user %r is not a member of workspace %r"
                % (user, workspace))
        if doc_id not in index.documents:
            raise UnknownDocumentError(
                "workspace %r has no document %r" % (workspace, doc_id))
        return DocumentHandle(self, index, doc_id)

    def read(self, user, workspace, doc_id):
        return self.open_document(user, workspace, doc_id).read(user)

    def write(self, user, workspace, doc_id, content, expected_version):
        handle = self.open_document(user, workspace, doc_id)
        return handle.write(user, content, expected_version)


class DocumentHandle:
    """A reference to a logical document, bound to an index generation."""

    __slots__ = ("_store", "_index", "_doc_id")

    def __init__(self, store, index, doc_id):
        self._store = store
        self._index = index
        self._doc_id = doc_id

    # -- introspection ----------------------------------------------------

    @property
    def workspace(self):
        return self._index.workspace

    @property
    def doc_id(self):
        return self._doc_id

    def _check_fresh(self):
        current = self._store._indexes.get(self._index.workspace)
        if current is None or current.generation != self._index.generation:
            raise StaleReferenceError(
                "handle for %r in workspace %r predates an index reload"
                % (self._doc_id, self._index.workspace))

    def _entry(self):
        self._check_fresh()
        entry = self._index.documents.get(self._doc_id)
        if entry is None:
            raise UnknownDocumentError(
                "workspace %r no longer has document %r"
                % (self._index.workspace, self._doc_id))
        return _parse_entry(self._doc_id, entry)

    def current_version(self):
        _, _, version = self._entry()
        return version

    def language(self):
        _, language, _ = self._entry()
        return language

    # -- operations ---------------------------------------------------------

    def read(self, user):
        rel_path, _language, _version = self._entry()
        if not self._store._users.is_member(user, self._index.workspace):
            raise NotWorkspaceMemberError(
                "user %r is not a member of workspace %r"
                % (user, self._index.workspace))
        final = resolve_within(self._index.directory, rel_path)
        fd = os.open(final, os.O_RDONLY | _O_NOFOLLOW)
        with os.fdopen(fd, "rb") as fh:
            data = fh.read()
        return data.decode("utf-8")

    def write(self, user, content, expected_version):
        rel_path, language, version = self._entry()
        users = self._store._users
        if not users.is_member(user, self._index.workspace):
            raise NotWorkspaceMemberError(
                "user %r is not a member of workspace %r"
                % (user, self._index.workspace))
        if not users.edits_language(user, language):
            raise NotLanguageEditorError(
                "user %r is not an editor for language %r"
                % (user, language))
        if expected_version != version:
            raise VersionConflictError(
                "document %r is at version %d, not expected version %r"
                % (self._doc_id, version, expected_version))
        final = resolve_within(self._index.directory, rel_path)
        data = content.encode("utf-8") if isinstance(content, str) \
            else bytes(content)
        _atomic_replace(final, data)
        self._index.documents[self._doc_id]["version"] = version + 1
        self._store._persist_index(self._index)
        return version + 1
