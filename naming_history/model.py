"""Core namespace model for the naming-history index.

The model is a POSIX-like namespace:

* objects ("inodes") are either files or directories;
* a directory entry maps ``(parent directory, name) -> child object``;
* a file may be reachable through zero or more entries (hard links);
* a directory has at most one parent entry;
* ``root`` is the only directory without a parent entry.

Every mutation returns a brand new :class:`Namespace`; an invalid mutation
raises instead of returning a value.  This is what lets the history index
guarantee that a rejected event is never committed and therefore never
becomes queryable evidence.

Paths are never stored.  They are derived on demand by walking the single
parent chain of a directory, which is the property that makes the
"same name reused later" and "temporary path during exchange" bugs
impossible by construction.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

ROOT_ID = "root"
FILE = "file"
DIR = "dir"

_KINDS = (FILE, DIR)


class NamingHistoryError(Exception):
    """Base class for every error raised by the naming-history index."""


class InvalidEventError(NamingHistoryError):
    """An event (or an initial tree) violates the namespace invariants."""


class UnknownObjectError(NamingHistoryError):
    """An object id is not part of the namespace at the queried moment."""


class GoneObjectError(UnknownObjectError):
    """The object existed earlier but has left the namespace by this point."""


class FutureObjectError(UnknownObjectError):
    """The object is only introduced by an event after this sequence."""


class AlreadyExistsError(NamingHistoryError):
    """A directory entry with the requested name already exists."""


class NotFoundError(NamingHistoryError):
    """The requested directory entry does not exist."""


class InvalidQueryError(NamingHistoryError):
    """A query refers to a sequence number outside the committed history."""


def check_name(name: str) -> None:
    """Reject names that cannot be a single directory entry in this model."""

    if not isinstance(name, str):
        raise InvalidEventError(
            "entry name must be a string, got %s" % type(name).__name__
        )
    if not name:
        raise InvalidEventError("entry name must not be empty")
    if name in (".", ".."):
        raise InvalidEventError("entry name %r is reserved" % (name,))
    if "/" in name or "\x00" in name:
        raise InvalidEventError(
            "entry name %r contains a forbidden character" % (name,)
        )


class Namespace:
    """An immutable snapshot of the directory namespace.

    ``kind`` maps an object id to ``FILE`` or ``DIR``.
    ``entries`` maps a parent directory id to ``{name: child id}``.
    ``retired`` is the set of directory ids that already existed and were
    removed earlier in this history.  Re-using such an id for a brand new
    object would make one identifier mean two different things at different
    sequence numbers, which is exactly the "same name appeared later"
    confusion the index must prevent.
    """

    __slots__ = ("kind", "entries", "retired")

    def __init__(
        self,
        kind: Dict[str, str],
        entries: Dict[str, Dict[str, str]],
        retired: frozenset = frozenset(),
    ) -> None:
        self.kind = dict(kind)
        self.entries = {
            parent: dict(mapping)
            for parent, mapping in entries.items()
            if mapping
        }
        self.retired = retired
        self._validate()

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------
    def _validate(self) -> None:
        for obj, kind in self.kind.items():
            if not isinstance(obj, str) or not obj:
                raise InvalidEventError("object ids must be non-empty strings")
            if kind not in _KINDS:
                raise InvalidEventError(
                    "object %r has unknown kind %r" % (obj, kind)
                )
        if self.kind.get(ROOT_ID) != DIR:
            raise InvalidEventError(
                "the namespace must declare a directory named %r" % (ROOT_ID,)
            )
        if ROOT_ID in self.retired:
            raise InvalidEventError("the root directory can never be retired")

        incoming: Dict[str, int] = {}
        for parent, mapping in self.entries.items():
            if parent not in self.kind:
                raise UnknownObjectError(
                    "directory entry parent %r is unknown" % (parent,)
                )
            if self.kind[parent] != DIR:
                raise InvalidEventError(
                    "directory entry parent %r is not a directory" % (parent,)
                )
            for name, child in mapping.items():
                check_name(name)
                if child not in self.kind:
                    raise UnknownObjectError(
                        "directory entry child %r is unknown" % (child,)
                    )
                if child == ROOT_ID:
                    raise InvalidEventError(
                        "the root directory cannot appear as a child entry"
                    )
                incoming[child] = incoming.get(child, 0) + 1

        # Every directory except root has exactly one visible parent.
        for obj, kind in self.kind.items():
            if kind != DIR or obj == ROOT_ID:
                continue
            count = incoming.get(obj, 0)
            if count == 0:
                raise InvalidEventError(
                    "directory %r has no parent entry" % (obj,)
                )
            if count > 1:
                raise InvalidEventError(
                    "directory %r would have more than one parent" % (obj,)
                )

        # A file may keep zero links; it then has no visible path but stays
        # retired so the same id can never be reintroduced as a new object.
        for obj, kind in self.kind.items():
            if kind == FILE and incoming.get(obj, 0) == 0:
                if obj not in self.retired:
                    raise InvalidEventError(
                        "file %r has no links and is not retired" % (obj,)
                    )

        # Every parent directory must be reachable from root; this rejects
        # detached subtrees and parent cycles.
        reachable = self._reachable_dirs()
        for parent in self.entries:
            if parent not in reachable:
                raise InvalidEventError(
                    "directory %r is not reachable from root "
                    "(parent cycle or detached subtree)" % (parent,)
                )

    def _reachable_dirs(self) -> set:
        seen = set()
        stack = [ROOT_ID]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            if self.kind.get(current) != DIR:
                continue
            seen.add(current)
            for child in self.entries.get(current, {}).values():
                if self.kind.get(child) == DIR and child not in seen:
                    stack.append(child)
        return seen

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _clone_entries(self) -> Dict[str, Dict[str, str]]:
        return {
            parent: dict(mapping)
            for parent, mapping in self.entries.items()
        }

    def _retire_if_unlinked(
        self,
        kind: Dict[str, str],
        entries: Dict[str, Dict[str, str]],
        obj_id: str,
    ) -> Tuple[Dict[str, str], frozenset]:
        """Retire an object that no longer has any visible path.

        A directory that is removed has no parent.  A file whose last hard
        link disappears has no paths.  Either way the id must never be
        reused later, or the same identifier would denote two different
        objects at two sequence numbers.
        """

        still_linked = any(
            child == obj_id
            for mapping in entries.values()
            for child in mapping.values()
        )
        if still_linked:
            return kind, self.retired
        pruned = dict(kind)
        pruned.pop(obj_id, None)
        return pruned, self.retired | {obj_id}

    def _require_dir(self, obj_id: str) -> None:
        if not isinstance(obj_id, str) or obj_id not in self.kind:
            raise UnknownObjectError("object %r is unknown" % (obj_id,))
        if self.kind[obj_id] != DIR:
            raise InvalidEventError(
                "object %r is not a directory" % (obj_id,)
            )

    def _reverse_index(self) -> Dict[str, List[Tuple[str, str]]]:
        """Map child id -> list of ``(parent, name)`` positions."""

        reverse: Dict[str, List[Tuple[str, str]]] = {}
        for parent, mapping in self.entries.items():
            for name, child in mapping.items():
                reverse.setdefault(child, []).append((parent, name))
        return reverse

    @staticmethod
    def _extend(parent_path: str, name: str) -> str:
        if parent_path == "/":
            return "/" + name
        return parent_path + "/" + name

    def _dir_path(
        self,
        dir_id: str,
        reverse: Dict[str, List[Tuple[str, str]]],
        seen: Tuple[str, ...],
    ) -> str:
        if dir_id == ROOT_ID:
            return "/"
        if dir_id in seen:
            raise InvalidEventError(
                "parent cycle detected while resolving %r" % (dir_id,)
            )
        positions = reverse.get(dir_id, [])
        if len(positions) != 1:
            raise InvalidEventError(
                "directory %r has %d parent entries, expected 1"
                % (dir_id, len(positions))
            )
        parent, name = positions[0]
        parent_path = self._dir_path(parent, reverse, seen + (dir_id,))
        return self._extend(parent_path, name)

    def _is_descendant(self, ancestor: str, node: str) -> bool:
        """True if ``node`` is ``ancestor`` itself or lives below it."""

        reverse = self._reverse_index()
        current = node
        while True:
            if current == ancestor:
                return True
            if current == ROOT_ID:
                return False
            positions = reverse.get(current, [])
            if len(positions) != 1:
                return False
            current = positions[0][0]

    # ------------------------------------------------------------------
    # path queries
    # ------------------------------------------------------------------
    def paths_of(self, obj_id: str) -> List[str]:
        """Return every complete path of ``obj_id`` in this snapshot.

        An object that is not part of this snapshot raises
        :class:`UnknownObjectError`; the concrete subclass
        (:class:`GoneObjectError` or :class:`FutureObjectError`) is chosen by
        the index, which is the layer that can see the whole timeline.
        """

        if not isinstance(obj_id, str) or obj_id not in self.kind:
            raise UnknownObjectError(
                "object %r is not known at this point in history" % (obj_id,)
            )
        if obj_id == ROOT_ID:
            return ["/"]

        reverse = self._reverse_index()
        positions = reverse.get(obj_id, [])
        if not positions:
            return []

        paths = set()
        for parent, name in positions:
            parent_path = self._dir_path(parent, reverse, ())
            paths.add(self._extend(parent_path, name))
        return sorted(paths)

    def all_paths(self) -> Dict[str, List[str]]:
        """Return ``object id -> sorted paths`` for every known object."""

        reverse = self._reverse_index()
        result: Dict[str, List[str]] = {}
        for obj in self.kind:
            if obj == ROOT_ID:
                result[obj] = ["/"]
                continue
            paths = set()
            for parent, name in reverse.get(obj, []):
                parent_path = self._dir_path(parent, reverse, ())
                paths.add(self._extend(parent_path, name))
            result[obj] = sorted(paths)
        return result

    # ------------------------------------------------------------------
    # mutations (each returns a new Namespace)
    # ------------------------------------------------------------------
    def link(self, dir_id: str, name: str, obj_id: str) -> "Namespace":
        """Create a new directory entry ``dir_id/name -> obj_id``.

        A link may introduce a previously unseen file object.  Directories
        can never be hard linked, which is what enforces the single-parent
        rule for directories.
        """

        self._require_dir(dir_id)
        check_name(name)
        if name in self.entries.get(dir_id, {}):
            raise AlreadyExistsError(
                "entry %r already exists in %r" % (name, dir_id)
            )
        if not isinstance(obj_id, str) or not obj_id:
            raise InvalidEventError("object id must be a non-empty string")
        if obj_id == ROOT_ID:
            raise InvalidEventError("the root directory cannot be linked")

        if obj_id not in self.kind:
            if obj_id in self.retired:
                raise InvalidEventError(
                    "object id %r belonged to an earlier object that has "
                    "left the namespace and must not be re-used for a new "
                    "object" % (obj_id,)
                )
            kind = dict(self.kind)
            kind[obj_id] = FILE
        else:
            if self.kind[obj_id] == DIR:
                raise InvalidEventError(
                    "directories cannot be hard linked (%r already has a "
                    "parent, or would gain a second one)" % (obj_id,)
                )
            kind = self.kind

        entries = self._clone_entries()
        entries.setdefault(dir_id, {})[name] = obj_id
        return Namespace(kind, entries, self.retired)

    def unlink(self, dir_id: str, name: str) -> "Namespace":
        """Remove the directory entry ``dir_id/name``.

        Removing a directory entry that points at a directory requires that
        directory to be empty, mirroring ``rmdir``; this prevents a whole
        subtree from silently losing its visible prefix.
        """

        self._require_dir(dir_id)
        check_name(name)
        mapping = self.entries.get(dir_id, {})
        if name not in mapping:
            raise NotFoundError(
                "entry %r does not exist in %r" % (name, dir_id)
            )

        child = mapping[name]
        if self.kind[child] == DIR and self.entries.get(child):
            raise InvalidEventError(
                "directory %r is not empty and cannot be unlinked" % (child,)
            )

        entries = self._clone_entries()
        del entries[dir_id][name]
        if not entries[dir_id]:
            del entries[dir_id]
        # A file whose last link disappears, or a removed empty directory,
        # now has no visible path.  Retire it so the id never comes back as
        # a different object at a later sequence number.
        kind, retired = self._retire_if_unlinked(self.kind, entries, child)
        return Namespace(kind, entries, retired)

    def rename(
        self,
        src_dir: str,
        src_name: str,
        dst_dir: str,
        dst_name: str,
    ) -> "Namespace":
        """Move one directory entry, replacing the destination if allowed.

        Renaming a directory moves its whole subtree implicitly, because
        paths are always derived from the current parent chain.
        """

        self._require_dir(src_dir)
        self._require_dir(dst_dir)
        check_name(src_name)
        check_name(dst_name)

        src_mapping = self.entries.get(src_dir, {})
        if src_name not in src_mapping:
            raise NotFoundError(
                "entry %r does not exist in %r" % (src_name, src_dir)
            )
        child = src_mapping[src_name]

        # Moving a directory into its own subtree would create a cycle.
        if self.kind[child] == DIR and self._is_descendant(child, dst_dir):
            raise InvalidEventError(
                "cannot move directory %r into its own subtree" % (child,)
            )

        if src_dir == dst_dir and src_name == dst_name:
            return self

        dst_mapping = self.entries.get(dst_dir, {})
        target = dst_mapping.get(dst_name)

        # POSIX: if both names already refer to the same object, no-op.
        if target is not None and target == child:
            return self

        entries = self._clone_entries()
        kind = self.kind
        retired = self.retired
        if target is not None:
            if self.kind[child] == DIR:
                if self.kind[target] != DIR:
                    raise InvalidEventError(
                        "cannot replace non-directory %r with a directory"
                        % (target,)
                    )
                if self.entries.get(target):
                    raise InvalidEventError(
                        "target directory %r is not empty" % (target,)
                    )
            else:
                if self.kind[target] == DIR:
                    raise InvalidEventError(
                        "cannot replace directory %r with a non-directory"
                        % (target,)
                    )
            del entries[dst_dir][dst_name]
            # The replaced name pointed at a different object; that object
            # loses a link and is retired if this was its last one.
            kind, retired = self._retire_if_unlinked(kind, entries, target)

        del entries[src_dir][src_name]
        if not entries[src_dir]:
            del entries[src_dir]
        entries.setdefault(dst_dir, {})[dst_name] = child
        return Namespace(kind, entries, retired)

    def exchange(
        self,
        a_dir: str,
        a_name: str,
        b_dir: str,
        b_name: str,
    ) -> "Namespace":
        """Atomically swap two existing directory entries.

        This is the ``renameat2(RENAME_EXCHANGE)`` primitive modelled as a
        single committed state change.  No temporary name is ever created,
        so no intermediate path can be queried as evidence.
        """

        self._require_dir(a_dir)
        self._require_dir(b_dir)
        check_name(a_name)
        check_name(b_name)

        a_mapping = self.entries.get(a_dir, {})
        b_mapping = self.entries.get(b_dir, {})
        if a_name not in a_mapping:
            raise NotFoundError(
                "entry %r does not exist in %r" % (a_name, a_dir)
            )
        if b_name not in b_mapping:
            raise NotFoundError(
                "entry %r does not exist in %r" % (b_name, b_dir)
            )

        x = a_mapping[a_name]
        y = b_mapping[b_name]

        if a_dir == b_dir and a_name == b_name:
            return self
        if x == y:
            # Both names already point at the same object; swapping the
            # bindings changes nothing observable.
            return self

        # After the swap x lives under b_dir and y lives under a_dir.
        # Reject swaps that would place a directory into its own subtree.
        if self.kind[x] == DIR and self._is_descendant(x, b_dir):
            raise InvalidEventError(
                "exchange would move directory %r into its own subtree" % (x,)
            )
        if self.kind[y] == DIR and self._is_descendant(y, a_dir):
            raise InvalidEventError(
                "exchange would move directory %r into its own subtree" % (y,)
            )

        entries = self._clone_entries()
        entries[a_dir][a_name] = y
        entries[b_dir][b_name] = x
        return Namespace(self.kind, entries, self.retired)
