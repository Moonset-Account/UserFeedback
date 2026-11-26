"""UID validation, generation and persistent per-project mapping.

The module is deliberately dependency free (standard library only).

Key guarantees
--------------
* Every pseudonym emitted by :class:`UidMapper` is a legal DICOM UID
  (digits and dots, no zero-prefixed arcs, at most 64 characters).
* Mappings are namespaced by *receiving project*: the same source UID
  maps to different pseudonyms for different projects.
* The mapping can be JSON-persisted and reloaded by a later process;
  reloading reproduces every previously issued pseudonym exactly.
* All allocation is serialized by an in-process ``RLock`` and, when a
  mapping file is used, by an ``fcntl`` advisory lock plus atomic
  replace, so concurrent exporters share one mapping safely.
* If an injected short-code generator produces a number that is already
  issued to another instance, the collision is detected and a different
  number is allocated.  An issued number is never reassigned and an
  existing forward mapping is never rewritten.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Callable, Dict, Iterator, Optional, Set, Union

try:  # POSIX advisory lock (macOS / Linux); optional on other platforms.
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    fcntl = None  # type: ignore

UID_MAX_LENGTH = 64

# Legal DICOM UID / ISO object identifier textual form:
# arcs of decimal digits; an arc may be "0" but otherwise must not have
# a leading zero.  At least two arcs (one dot) are required for emitted
# pseudonyms; the validator itself accepts a single legal arc too.
_UID_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))*$")
_ARC_RE = re.compile(r"^(?:0|[1-9][0-9]*)$")

# A conservative, synthetic default root used only when the caller does
# not configure one.  "1.2.840" is the ANSI/USA root; 999999 is used
# here as a clearly local/test arc.
DEFAULT_BASE_ROOT = "1.2.840.999999.1"

MAPPING_FORMAT = "deid-gateway-uid-map/1"


class UidError(ValueError):
    """Base class for UID related errors."""


class IllegalUidError(UidError):
    """A value that is not a legal numeric dot string was requested."""


class InvalidShortCodeError(UidError):
    """An injected short-code generator returned a non numeric code."""


class MappingConflictError(UidError):
    """A binding would reassign an already issued pseudonym.

    The gateway never resolves such a conflict by overwriting an
    existing mapping; callers receive this error instead.
    """


class UidCollisionError(MappingConflictError):
    """Raised when no unique pseudonym could be allocated."""


def is_legal_uid(value: object, *, max_length: int = UID_MAX_LENGTH) -> bool:
    """Return ``True`` iff *value* is a legal DICOM UID string.

    Rules enforced: ``str``, non-empty, at most *max_length*
    characters, only digits / dots, no empty arcs and no arc with a
    leading zero unless the arc is exactly ``"0"``.
    """

    if not isinstance(value, str):
        return False
    if not value or len(value) > max_length:
        return False
    return _UID_RE.fullmatch(value) is not None


def assert_legal_uid(value: str) -> str:
    """Validate *value* as an emitted UID (must contain a dot as well)."""

    if not is_legal_uid(value):
        raise IllegalUidError(f"not a legal DICOM UID: {value!r}")
    if "." not in value:
        raise IllegalUidError(f"emitted UID must contain a dot: {value!r}")
    return value


def canonical_short_code(code: object) -> str:
    """Canonicalize a generator result to one legal numeric arc.

    Integers and zero-padded numeric strings are accepted; the returned
    arc never has a leading zero (``"007"`` -> ``"7"``).  This means an
    injected ``"007"`` collides with an already issued ``"7"`` instead
    of slipping through as a different illegal string.
    """

    if isinstance(code, bool):  # bool is an int subclass - reject explicitly
        raise InvalidShortCodeError(f"short code must be numeric, got {code!r}")
    if isinstance(code, int):
        number = code
    elif isinstance(code, str) and code.strip().isdigit():
        number = int(code.strip())
    else:
        raise InvalidShortCodeError(
            "short code generator must yield a non-negative integer or a "
            f"digit string, got {code!r}"
        )
    if number < 0:
        raise InvalidShortCodeError(f"short code must be non-negative, got {code!r}")
    return str(number)


class ShortCodeGenerator:
    """Monotonic per-namespace numeric short-code source.

    Tests can inject collisions by subclassing and overriding
    :meth:`next_code`, or by using :class:`ScriptedShortCodeGenerator`.
    The mapper treats every returned code as an untrusted candidate and
    verifies uniqueness against the issued mapping.
    """

    def __init__(self, start: int = 1) -> None:
        if start < 0:
            raise ValueError("start must be >= 0")
        self._start = int(start)
        self._high_water: Dict[str, int] = {}
        self._lock = threading.RLock()

    def seed(self, namespace: str, high_water: int) -> None:
        """Advance the counter so future codes are > ``high_water``."""

        with self._lock:
            high_water = int(high_water)
            self._high_water[namespace] = max(
                self._high_water.get(namespace, self._start - 1), high_water
            )

    def next_code(self, namespace: str = "default") -> str:
        """Return the next short numeric code for *namespace*."""

        with self._lock:
            current = self._high_water.get(namespace, self._start - 1) + 1
            self._high_water[namespace] = current
            return str(current)

    # Common alternative method names used by injected test generators.
    def generate(self, namespace: str = "default") -> str:
        return self.next_code(namespace)

    def next_id(self, namespace: str = "default") -> str:
        return self.next_code(namespace)

    def next(self, namespace: str = "default") -> str:  # noqa: A003
        return self.next_code(namespace)


class ScriptedShortCodeGenerator(ShortCodeGenerator):
    """Generator that yields a scripted sequence first.

    This is the intended collision-injection seam.  For example::

        gen = ScriptedShortCodeGenerator(["7", "7", "8"])

    yields ``7`` (possibly free), then a duplicate ``7`` and finally
    ``8``.  After the script is exhausted it falls back to a monotonic
    counter.  Use :meth:`always` for an adversarial generator that never
    stops colliding.
    """

    def __init__(
        self,
        codes: Optional[list] = None,
        *,
        repeat: Optional[object] = None,
        start: int = 1,
    ) -> None:
        super().__init__(start=start)
        self._script = list(codes or [])
        self._repeat = repeat
        self._script_lock = threading.RLock()

    @classmethod
    def collide_once(cls, code: object, *, then_start: Optional[int] = None):
        """Yield *code* once more, then continue monotonically."""

        return cls([code], start=int(code) + 1 if then_start is None else then_start)

    @classmethod
    def always(cls, code: object) -> "ScriptedShortCodeGenerator":
        """Generator that always yields the same (colliding) *code*."""

        return cls(repeat=code)

    def seed(self, namespace: str, high_water: int) -> None:  # noqa: D401
        # Seeding must not disturb an explicit collision script.
        if self._repeat is None:
            super().seed(namespace, high_water)

    def next_code(self, namespace: str) -> str:
        with self._script_lock:
            if self._repeat is not None:
                return canonical_short_code(self._repeat)
            if self._script:
                return canonical_short_code(self._script.pop(0))
        return super().next_code(namespace)


CodeGeneratorLike = Union[ShortCodeGenerator, Callable[..., object]]

_GENERATOR_METHOD_NAMES = (
    "next_code", "next_short_code", "next_id", "generate_code",
    "short_code", "generate", "next",
)


def _call_generator(generator: CodeGeneratorLike, project: str) -> object:
    # Preferred explicit protocol.
    next_code = getattr(generator, "next_code", None)
    if callable(next_code):
        return next_code(project)
    # Tolerate injected fakes named with neighboring vocabulary.
    for name in _GENERATOR_METHOD_NAMES[1:]:
        method = getattr(generator, name, None)
        if callable(method):
            try:
                return method(project)
            except TypeError:
                return method()
    if callable(generator):
        try:
            return generator(project)
        except TypeError:
            return generator()
    raise TypeError(
        "code_generator must be callable or provide one of "
        f"{_GENERATOR_METHOD_NAMES}; got {type(generator)!r}"
    )


class UidMapper:
    """Persistent, project-isolated, collision-safe UID mapping.

    Parameters
    ----------
    mapping_path:
        Optional JSON file.  When supplied, every new binding is made
        inside an inter-process file transaction and atomically saved.
    base_root:
        Legal numeric dot prefix for generated pseudonyms.
    code_generator:
        A :class:`ShortCodeGenerator`, an object with ``next_code`` or a
        plain callable ``project -> code``.  Inject a scripted/colliding
        generator here to exercise collision handling.
    preserved_uids:
        Exact UID values that must never be mapped (encoding constants
        can also be supplied by the field dictionary).
    max_attempts:
        How many candidate short codes to try before using a guaranteed
        unique disambiguation arc.
    """

    FORMAT = MAPPING_FORMAT

    def __init__(
        self,
        first: Optional[Union[str, "os.PathLike[str]", "UidMapper"]] = None,
        base_root: Optional[str] = None,
        *,
        mapping_path: Optional[Union[str, "os.PathLike[str]"]] = None,
        code_generator: Optional[CodeGeneratorLike] = None,
        generator: Optional[CodeGeneratorLike] = None,
        preserved_uids: Optional[Set[str]] = None,
        max_attempts: int = 32,
        prefix: Optional[str] = None,
        root: Optional[str] = None,
    ) -> None:
        # Flexible first positional argument:
        #   UidMapper("map.json")      -> mapping path
        #   UidMapper(Path(...))       -> mapping path
        #   UidMapper("1.2.3.4")       -> base root / prefix
        #   UidMapper(existing_mapper) -> shared in-memory state
        if isinstance(first, UidMapper):
            self.__dict__.update(first.__dict__)
            return
        if first is not None:
            candidate_path, candidate_root = self._classify_first_arg(first)
            if mapping_path is None:
                mapping_path = candidate_path
            if base_root is None:
                base_root = candidate_root

        if base_root is None:
            base_root = prefix or root or DEFAULT_BASE_ROOT
        if not is_legal_uid(base_root):
            raise IllegalUidError(f"base_root is not a legal UID: {base_root!r}")
        self.base_root = base_root
        self.path = Path(mapping_path) if mapping_path is not None else None
        chosen_generator = code_generator if code_generator is not None else generator
        self._generator: CodeGeneratorLike = chosen_generator or ShortCodeGenerator()
        self._owns_generator = chosen_generator is None
        self.max_attempts = max(1, int(max_attempts))

        self.preserved_uids: Set[str] = set(preserved_uids or ())

        self._lock = threading.RLock()
        self._forward: Dict[str, Dict[str, str]] = {}
        self._reverse: Dict[str, Dict[str, str]] = {}
        self._projects: Dict[str, str] = {}
        self._project_seq = 0
        self._fallback_seq: Dict[str, int] = {}
        # Observability for tests / operators.
        self.collision_events: list = []
        self.allocations = 0

        if self.path is not None and self.path.exists():
            with self._lock:
                self._load_file_locked(self.path, replace_root=False)

    @staticmethod
    def _classify_first_arg(
        first: Union[str, "os.PathLike[str]"],
    ):
        """Tell whether a positional argument is a mapping path or UID root."""

        if isinstance(first, os.PathLike):
            return first, None
        text = str(first)
        if "/" in text or "\\" in text or text.lower().endswith(".json"):
            return text, None
        if is_legal_uid(text):
            return None, text
        return text, None

    @property
    def generator(self) -> CodeGeneratorLike:
        return self._generator

    @generator.setter
    def generator(self, value: CodeGeneratorLike) -> None:
        self._generator = value

    # ------------------------------------------------------------------ io
    @classmethod
    def load(
        cls,
        path: Union[str, os.PathLike],
        *,
        code_generator: Optional[CodeGeneratorLike] = None,
        base_root: Optional[str] = None,
    ) -> "UidMapper":
        """Load a mapping file into a new mapper.

        The base root stored in the file is reused unless *base_root* is
        explicitly provided.  New processes using this constructor
        reproduce all stored pseudonyms exactly.
        """

        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        stored_root = data.get("base_root") if isinstance(data, dict) else None
        mapper = cls(
            path,
            base_root=base_root or stored_root or DEFAULT_BASE_ROOT,
            code_generator=code_generator,
        )
        return mapper

    def save(self, path: Optional[Union[str, os.PathLike]] = None) -> Path:
        """Atomically persist the mapping to *path* (or its configured one)."""

        target = Path(path) if path is not None else self.path
        if target is None:
            raise ValueError("no save path given and mapper has no mapping_path")
        with self._lock:
            self._write_atomic_locked(target)
        return target

    def _state_dict_locked(self) -> dict:
        return {
            "format": self.FORMAT,
            "base_root": self.base_root,
            "projects": dict(sorted(self._projects.items())),
            "project_seq": self._project_seq,
            "fallback_seq": dict(sorted(self._fallback_seq.items())),
            "forward": {
                project: dict(sorted(table.items()))
                for project, table in sorted(self._forward.items())
            },
        }

    def to_json(self, *, indent: int = 2) -> str:
        with self._lock:
            return json.dumps(self._state_dict_locked(), ensure_ascii=False, indent=indent)

    def _write_atomic_locked(self, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    self._state_dict_locked(),
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

    def _load_file_locked(self, path: Path, *, replace_root: bool = True) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise MappingConflictError(f"mapping file is not a JSON object: {path}")
        if replace_root and data.get("base_root"):
            self.base_root = str(data["base_root"])
        self._project_seq = int(data.get("project_seq", 0))
        self._projects = {str(k): str(v) for k, v in (data.get("projects") or {}).items()}
        self._fallback_seq = {
            str(k): int(v) for k, v in (data.get("fallback_seq") or {}).items()
        }
        forward: Dict[str, Dict[str, str]] = {}
        reverse: Dict[str, Dict[str, str]] = {}
        raw_forward = data.get("forward") or {}
        if not isinstance(raw_forward, dict):
            raise MappingConflictError("mapping file has invalid 'forward' table")
        for project, table in raw_forward.items():
            project = str(project)
            if not isinstance(table, dict):
                raise MappingConflictError(f"forward[{project!r}] must be an object")
            forward[project] = {}
            reverse[project] = {}
            for original, pseudonym in table.items():
                original, pseudonym = str(original), str(pseudonym)
                owner = reverse[project].get(pseudonym)
                if owner is not None and owner != original:
                    raise MappingConflictError(
                        "mapping file is inconsistent: pseudonym "
                        f"{pseudonym!r} is bound to both {owner!r} and {original!r}"
                    )
                forward[project][original] = pseudonym
                reverse[project][pseudonym] = original
        self._forward = forward
        self._reverse = reverse
        self._seed_generator_from_state_locked()

    def _seed_generator_from_state_locked(self) -> None:
        seed = getattr(self._generator, "seed", None)
        if not callable(seed):
            return
        high_water: Dict[str, int] = {}
        for project, table in self._forward.items():
            worst = 0
            for pseudonym in table.values():
                tail = pseudonym.rsplit(".", 1)[-1]
                if tail.isdigit():
                    worst = max(worst, int(tail))
            high_water[project] = worst
        for project, value in high_water.items():
            with contextlib.suppress(Exception):
                seed(project, value)

    @contextlib.contextmanager
    def _transaction(self, *, persist: bool = True) -> Iterator[None]:
        """Serialize a read/latest -> mutate -> persist operation."""

        self._lock.acquire()
        lock_handle = None
        try:
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                lock_path = self.path.with_name(self.path.name + ".lock")
                lock_handle = open(lock_path, "a+", encoding="utf-8")
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                # Another process may have committed bindings since our
                # last transaction; adopt the authoritative file state.
                if self.path.exists():
                    self._load_file_locked(self.path)
            yield
            if persist and self.path is not None:
                self._write_atomic_locked(self.path)
        finally:
            if lock_handle is not None:
                try:
                    if fcntl is not None:
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                finally:
                    lock_handle.close()
            self._lock.release()

    # ------------------------------------------------------------- projects
    @staticmethod
    def normalize_project(project: Optional[str]) -> str:
        if project is None or project == "":
            return "default"
        return str(project)

    def _project_code_locked(self, project: str) -> str:
        code = self._projects.get(project)
        if code is not None:
            return code
        self._project_seq += 1
        code = str(self._project_seq)
        # Project codes share the same uniqueness discipline.
        used = set(self._projects.values())
        while code in used:  # pragma: no cover - defensive
            self._project_seq += 1
            code = str(self._project_seq)
        self._projects[project] = code
        return code

    def project_code(self, project: Optional[str] = None) -> str:
        project = self.normalize_project(project)
        with self._lock:
            return self._project_code_locked(project)

    # ------------------------------------------------------------- querying
    def is_preserved(self, uid: object) -> bool:
        return isinstance(uid, str) and uid in self.preserved_uids

    def resolve(self, pseudonym: str, project: Optional[str] = None) -> Optional[str]:
        """Return the source UID currently bound to *pseudonym*, if any."""

        project = self.normalize_project(project)
        with self._transaction(persist=False):
            return self._reverse.setdefault(project, {}).get(pseudonym)

    def lookup(self, source_uid: str, project: Optional[str] = None) -> Optional[str]:
        """Return the existing pseudonym for *source_uid* without allocating."""

        project = self.normalize_project(project)
        with self._lock:
            return self._forward.setdefault(project, {}).get(source_uid)

    def mapping_for(self, project: Optional[str] = None) -> Dict[str, str]:
        """Return a copy of the ``source -> pseudonym`` table for *project*."""

        project = self.normalize_project(project)
        with self._lock:
            return dict(self._forward.setdefault(project, {}))

    def projects(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._projects)

    # -------------------------------------------------------------- binding
    def bind(
        self,
        source_uid: str,
        pseudonym: str,
        project: Optional[str] = None,
    ) -> str:
        """Explicitly register an existing issued binding.

        Binding the same pair twice is idempotent.  Binding a source to a
        different pseudonym, or assigning an already issued pseudonym to
        another source, raises :class:`MappingConflictError` and changes
        nothing.
        """

        project = self.normalize_project(project)
        if not isinstance(source_uid, str) or not source_uid:
            raise IllegalUidError(f"source_uid must be a non-empty string, got {source_uid!r}")
        assert_legal_uid(pseudonym)
        with self._transaction():
            forward = self._forward.setdefault(project, {})
            reverse = self._reverse.setdefault(project, {})
            existing = forward.get(source_uid)
            if existing is not None:
                if existing != pseudonym:
                    raise MappingConflictError(
                        f"source UID {source_uid!r} is already issued as "
                        f"{existing!r}; refusing to replace with {pseudonym!r}"
                    )
                return pseudonym
            owner = reverse.get(pseudonym)
            if owner is not None and owner != source_uid:
                raise MappingConflictError(
                    f"pseudonym {pseudonym!r} was already issued to {owner!r}; "
                    f"refusing to reassign it to {source_uid!r}"
                )
            forward[source_uid] = pseudonym
            reverse[pseudonym] = source_uid
            self.allocations += 1
            return pseudonym

    # ------------------------------------------------------------ allocation
    def _compose_locked(self, project: str, code: str) -> str:
        project_code = self._project_code_locked(project)
        return f"{self.base_root}.{project_code}.{code}"

    def _fallback_locked(self, project: str, source_uid: str) -> str:
        """Deterministic-then-UUID unique fallback after code collisions."""

        reverse = self._reverse.setdefault(project, {})
        # First try a monotonic disambiguation arc under arc 99.  The
        # sequence is persisted, so it can never hand out the same
        # fallback twice.
        seq = self._fallback_seq.get(project, 0)
        prefix = self._compose_locked(project, "99")
        if len(f"{prefix}.{seq + 1}") <= UID_MAX_LENGTH:
            for _ in range(10_000):
                seq += 1
                candidate = f"{prefix}.{seq}"
                if len(candidate) <= UID_MAX_LENGTH and is_legal_uid(candidate):
                    if candidate not in reverse:
                        self._fallback_seq[project] = seq
                        return candidate
            self._fallback_seq[project] = seq

        # Long base roots can make the form above too long; use the
        # standard 2.25.<128-bit integer> UID root.  The reverse table
        # makes even a vanishingly unlikely clash fatal-and-retry rather
        # than aliasing two instances.
        for _ in range(16):
            candidate = f"2.25.{uuid.uuid4().int}"
            if len(candidate) <= UID_MAX_LENGTH and candidate not in reverse:
                return candidate
        raise UidCollisionError("could not allocate a unique fallback UID")

    def map_uid(
        self,
        source_uid: object,
        project: Optional[str] = None,
    ):
        """Return the project-scoped pseudonym for *source_uid*.

        * ``None`` / empty values pass through.
        * Preserved registered constants pass through unchanged.
        * Values that are already known pseudonyms of this project pass
          through unchanged (idempotent under accidental double walks).
        * Otherwise a unique legal UID is allocated and persisted.
        """

        if source_uid is None:
            return None
        if not isinstance(source_uid, str):
            if isinstance(source_uid, bytes):
                source_uid = source_uid.decode("ascii")
            else:
                source_uid = str(source_uid)
        stripped = source_uid.strip()
        if stripped == "":
            return source_uid
        if stripped in self.preserved_uids:
            return source_uid

        project = self.normalize_project(project)
        with self._transaction():
            forward = self._forward.setdefault(project, {})
            reverse = self._reverse.setdefault(project, {})

            existing = forward.get(stripped)
            if existing is not None:
                return existing
            # Already a pseudonym issued inside this project: do not
            # pseudonymize the pseudonym.
            if stripped in reverse:
                return stripped

            candidate: Optional[str] = None
            for attempt in range(self.max_attempts):
                raw_code = _call_generator(self._generator, project)
                try:
                    code = canonical_short_code(raw_code)
                    candidate = self._compose_locked(project, code)
                except InvalidShortCodeError:
                    self.collision_events.append(
                        {
                            "project": project,
                            "source": stripped,
                            "attempt": attempt,
                            "kind": "invalid_code",
                            "code": repr(raw_code),
                        }
                    )
                    continue

                if len(candidate) > UID_MAX_LENGTH or not is_legal_uid(candidate):
                    self.collision_events.append(
                        {
                            "project": project,
                            "source": stripped,
                            "attempt": attempt,
                            "kind": "illegal_candidate",
                            "candidate": candidate,
                        }
                    )
                    continue

                owner = reverse.get(candidate)
                if owner is None:
                    break
                if owner == stripped:  # pragma: no cover - consistency repair
                    forward[stripped] = candidate
                    return candidate
                # Collision with another already issued instance.
                # Deliberately do NOT touch forward[owner] / reverse.
                self.collision_events.append(
                    {
                        "project": project,
                        "source": stripped,
                        "attempt": attempt,
                        "kind": "short_code_collision",
                        "candidate": candidate,
                        "owner": owner,
                    }
                )
                self._notify_collision(project, code, candidate, owner)
            else:
                candidate = None

            if candidate is None or reverse.get(candidate) is not None:
                candidate = self._fallback_locked(project, stripped)
                self.collision_events.append(
                    {
                        "project": project,
                        "source": stripped,
                        "kind": "fallback",
                        "candidate": candidate,
                    }
                )

            assert_legal_uid(candidate)
            owner = reverse.get(candidate)
            if owner is not None and owner != stripped:
                # Last line of defence: never alias two instances.
                raise MappingConflictError(
                    f"internal collision: {candidate!r} already issued to {owner!r}"
                )
            forward[stripped] = candidate
            reverse[candidate] = stripped
            self.allocations += 1
            return candidate

    def _notify_collision(self, project, code, candidate, owner) -> None:
        """Best effort callback into an injected generator that exposes one."""

        for hook_name in ("note_collision", "on_collision", "report_collision"):
            hook = getattr(self._generator, hook_name, None)
            if callable(hook):
                with contextlib.suppress(Exception):
                    hook(project, code) if hook_name != "on_collision" else hook(
                        project, code, candidate, owner
                    )
                return

    # Friendly method-name aliases.
    def mask_uid(self, source_uid, project: Optional[str] = None):
        return self.map_uid(source_uid, project)

    def get_pseudonym(self, source_uid, project: Optional[str] = None):
        return self.map_uid(source_uid, project)

    def map(self, source_uid, project: Optional[str] = None):  # noqa: A003
        return self.map_uid(source_uid, project)

    __call__ = map_uid


# Descriptive aliases so callers/tests using different vocabulary find
# the same implementation.
UidMapping = UidMapper
UidMappingStore = UidMapper
MappingStore = UidMapper
UidRegistry = UidMapper
UidMasker = UidMapper
Masker = UidMapper
UidAnonymizer = UidMapper

UidGenerator = ShortCodeGenerator
ShortUidGenerator = ShortCodeGenerator
ShortIdGenerator = ShortCodeGenerator
IdGenerator = ShortCodeGenerator
CodeGenerator = ShortCodeGenerator
