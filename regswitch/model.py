"""Register context-switch model for an isolated execution environment.

The model captures the register-state rules of a machine that switches
between execution contexts of different trust levels:

* Threads and handlers carry a :class:`TrustLevel`.  A handler whose trust
  is lower than the context it preempts is a *low-trust* handler.
* Every handler has an :class:`EntryPolicy` naming the general-purpose
  registers (GPRs) and *paired* vector registers it is allowed to retain.
* On entry, the preempted context's authoritative GPR state is sealed into
  a private frame owned by its protection domain.  The entered handler
  observes the kept registers (inheritance per mask) and reads zero for
  everything else.
* Vector registers are saved/restored lazily, in groups of register pairs.
  An interrupt may arrive before any lazy save happened; a handler takes
  ownership of a vector group only on its first use of that group.  At
  that point the live owner's copy is sealed into the owner's private
  frame and the handler either inherits the live contents (kept group,
  handler trust >= owner trust) or sees zeros.
* Every context owns at most one state copy of each register: GPRs are
  switched eagerly, so the current context's copy is the register file and
  every suspended context holds exactly one sealed copy in its private
  frame.  For each vector group, at most one context is the *live owner*
  (its copy is the register file); every other context that has used the
  group holds its own copy sealed in its own private frame.  Return from
  a handler, nested preemption, thread exit and scheduling to another
  protection domain all maintain this single-valid-copy invariant, which
  :meth:`Machine.audit` verifies.
"""

from __future__ import annotations

from enum import IntEnum

MASK64 = (1 << 64) - 1

__all__ = [
    "MASK64",
    "TrustLevel",
    "EntryPolicy",
    "Machine",
    "ModelError",
    "UnknownContextError",
    "UnknownRegisterError",
    "InvalidPolicyError",
    "StackError",
    "StateIntegrityError",
]


class TrustLevel(IntEnum):
    """Trust ordering for threads and handlers (higher is more trusted)."""

    PUBLIC = 0
    PROTECTED = 1


class ModelError(Exception):
    """Base class for all model errors."""


class UnknownContextError(ModelError):
    """A referenced domain, thread or handler does not exist."""


class UnknownRegisterError(ModelError):
    """A referenced register does not exist on this machine."""


class InvalidPolicyError(ModelError):
    """An entry policy references registers the machine does not have."""


class StackError(ModelError):
    """The operation is illegal for the current interrupt/schedule stack."""


class StateIntegrityError(ModelError):
    """The single-valid-copy invariant was violated."""


def _vslot(group: int) -> str:
    """Frame slot key for a lazy vector group."""
    return f"vgrp{group}"


class EntryPolicy:
    """Registers an entered handler is allowed to retain (inherit).

    ``keep_gprs``: iterable of GPR names (e.g. ``{"x0", "x1"}``).
    ``keep_vector_pairs``: iterable of vector-register *pair* indices.
    A whole lazy-save group is inherited only if every pair of the group
    is listed here (and the handler's trust is at least the owner's).
    """

    __slots__ = ("keep_gprs", "keep_vector_pairs")

    def __init__(self, keep_gprs=(), keep_vector_pairs=()):
        self.keep_gprs = frozenset(keep_gprs)
        self.keep_vector_pairs = frozenset(int(p) for p in keep_vector_pairs)

    def __repr__(self):  # pragma: no cover - debugging aid
        return (
            f"EntryPolicy(keep_gprs={sorted(self.keep_gprs)}, "
            f"keep_vector_pairs={sorted(self.keep_vector_pairs)})"
        )


class PrivateFrame:
    """Per-context private save area, owned by a protection domain.

    A slot is *sealed* exactly once and *unsealed* (consumed) exactly
    once; this is what makes each saved copy unique.
    """

    __slots__ = ("owner_name", "domain", "_slots")

    def __init__(self, owner_name: str, domain: str):
        self.owner_name = owner_name
        self.domain = domain
        self._slots = {}

    def seal(self, slot: str, value) -> None:
        if slot in self._slots:
            raise StateIntegrityError(
                f"duplicate seal of {slot!r} into frame of {self.owner_name!r}"
            )
        self._slots[slot] = value

    def unseal(self, slot: str):
        try:
            return self._slots.pop(slot)
        except KeyError:
            raise StateIntegrityError(
                f"unseal of missing slot {slot!r} from frame of "
                f"{self.owner_name!r}"
            ) from None

    def __contains__(self, slot: str) -> bool:
        return slot in self._slots

    def slots(self) -> frozenset:
        return frozenset(self._slots)

    def snapshot(self) -> dict:
        return dict(self._slots)


class _Context:
    """An execution context: a thread or an active handler instance."""

    __slots__ = ("name", "kind", "trust", "domain", "policy", "frame")

    def __init__(self, name, kind, trust, domain, policy=None):
        self.name = name
        self.kind = kind  # "thread" | "handler"
        self.trust = TrustLevel(trust)
        self.domain = domain
        self.policy = policy
        self.frame = PrivateFrame(name, domain)


class _HandlerSpec:
    __slots__ = ("name", "trust", "domain", "policy")

    def __init__(self, name, trust, domain, policy):
        self.name = name
        self.trust = TrustLevel(trust)
        self.domain = domain
        self.policy = policy


class Machine:
    """The switchable machine: register file, contexts and private frames.

    Parameters
    ----------
    num_gprs:
        Number of general-purpose registers, named ``x0`` .. ``x{N-1}``.
    num_vector_pairs:
        Number of vector-register *pairs*; vector registers are ``v0`` ..
        ``v{2*num_vector_pairs-1}`` and pair ``p`` is ``(v{2p}, v{2p+1})``.
    pairs_per_group:
        How many consecutive pairs form one lazy-save vector group.
        Must divide ``num_vector_pairs``.
    vector_lanes:
        Number of 64-bit lanes per vector register.
    """

    def __init__(self, num_gprs=8, num_vector_pairs=4, pairs_per_group=1,
                 vector_lanes=4):
        if num_gprs < 1:
            raise ValueError("num_gprs must be >= 1")
        if num_vector_pairs < 1 or pairs_per_group < 1:
            raise ValueError("num_vector_pairs and pairs_per_group must be >= 1")
        if num_vector_pairs % pairs_per_group != 0:
            raise ValueError("pairs_per_group must divide num_vector_pairs")
        if vector_lanes < 1:
            raise ValueError("vector_lanes must be >= 1")
        self.num_gprs = num_gprs
        self.gpr_names = tuple(f"x{i}" for i in range(num_gprs))
        self.num_vector_pairs = num_vector_pairs
        self.pairs_per_group = pairs_per_group
        self.num_groups = num_vector_pairs // pairs_per_group
        self.vector_lanes = vector_lanes

        self._zero_vec = (0,) * vector_lanes
        self._gprs = {name: 0 for name in self.gpr_names}
        self._vregs = [self._zero_vec] * (num_vector_pairs * 2)

        self._domains = set()
        self._threads = {}
        self._handlers = {}
        self._stack = []  # execution stack; top is the current context
        # Live owner of each lazy vector group: the context whose copy of
        # the group is the register file right now, or None.  The live
        # owner is always the current context or a context suspended in
        # the current interrupt stack.
        self._group_owner = [None] * self.num_groups
        self._handler_serial = 0

    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #

    def add_domain(self, name: str) -> None:
        """Register a protection domain."""
        if name in self._domains:
            raise ModelError(f"duplicate domain {name!r}")
        self._domains.add(name)

    def spawn_thread(self, name: str, domain: str, trust: TrustLevel) -> None:
        """Create a thread in ``domain`` with an all-zero saved state."""
        if domain not in self._domains:
            raise UnknownContextError(f"unknown domain {domain!r}")
        if name in self._threads:
            raise ModelError(f"duplicate thread {name!r}")
        ctx = _Context(name, "thread", trust, domain)
        # A fresh thread's authoritative state is an all-zero saved copy,
        # so the single-valid-copy invariant holds uniformly.
        for slot in self.gpr_names:
            ctx.frame.seal(slot, 0)
        self._threads[name] = ctx

    def add_handler(self, name: str, trust: TrustLevel, domain: str,
                    policy: EntryPolicy) -> None:
        """Register an interrupt handler with its entry policy."""
        if domain not in self._domains:
            raise UnknownContextError(f"unknown domain {domain!r}")
        if name in self._handlers:
            raise ModelError(f"duplicate handler {name!r}")
        if not isinstance(policy, EntryPolicy):
            raise InvalidPolicyError("policy must be an EntryPolicy")
        unknown_gprs = policy.keep_gprs - set(self.gpr_names)
        if unknown_gprs:
            raise InvalidPolicyError(
                f"policy keeps unknown GPRs: {sorted(unknown_gprs)}"
            )
        bad_pairs = [p for p in policy.keep_vector_pairs
                     if not 0 <= p < self.num_vector_pairs]
        if bad_pairs:
            raise InvalidPolicyError(
                f"policy keeps unknown vector pairs: {bad_pairs}"
            )
        self._handlers[name] = _HandlerSpec(name, trust, domain, policy)

    # ------------------------------------------------------------------ #
    # register access (current context's view)
    # ------------------------------------------------------------------ #

    def read_gpr(self, name: str) -> int:
        """Read a GPR as the current context sees it."""
        self._require_current()
        self._check_gpr(name)
        return self._gprs[name]

    def write_gpr(self, name: str, value: int) -> None:
        """Write a GPR of the current context (64-bit wrapped)."""
        self._require_current()
        self._check_gpr(name)
        self._gprs[name] = int(value) & MASK64

    def read_vector_reg(self, index: int) -> tuple:
        """Read vector register ``v{index}``; lazily acquires its group."""
        self._require_current()
        self._check_vreg(index)
        self._acquire_group(self._group_of_vreg(index))
        return self._vregs[index]

    def write_vector_reg(self, index: int, values) -> None:
        """Write vector register ``v{index}``; lazily acquires its group."""
        self._require_current()
        self._check_vreg(index)
        vec = self._check_vector_value(values)
        self._acquire_group(self._group_of_vreg(index))
        self._vregs[index] = vec

    def read_vector_pair(self, pair: int) -> tuple:
        """Read pair ``p`` as ``(v{2p}, v{2p+1})``."""
        self._check_pair(pair)
        return (self.read_vector_reg(2 * pair),
                self.read_vector_reg(2 * pair + 1))

    def write_vector_pair(self, pair: int, first, second) -> None:
        """Write pair ``p`` = (``v{2p}``, ``v{2p+1}``)."""
        self._check_pair(pair)
        self.write_vector_reg(2 * pair, first)
        self.write_vector_reg(2 * pair + 1, second)

    # ------------------------------------------------------------------ #
    # control flow
    # ------------------------------------------------------------------ #

    def start(self, thread: str) -> None:
        """Boot the machine into ``thread`` (machine must be idle)."""
        if self._stack:
            raise StackError("machine is not idle")
        ctx = self._threads.get(thread)
        if ctx is None:
            raise UnknownContextError(f"unknown thread {thread!r}")
        for slot in self.gpr_names:
            self._gprs[slot] = ctx.frame.unseal(slot)
        self._stack = [ctx]
        self.audit()

    def interrupt(self, handler: str) -> str:
        """Preempt the current context and enter ``handler``.

        The preempted context's authoritative GPR state is sealed into its
        protection domain's private frame; the handler observes the
        policy's kept registers and zero elsewhere.  Vector groups are
        *not* saved here: they are saved lazily on the handler's first
        use.  Returns the handler instance's context name.
        """
        spec = self._handlers.get(handler)
        if spec is None:
            raise UnknownContextError(f"unknown handler {handler!r}")
        cur = self._require_current()
        for slot in self.gpr_names:
            live = self._gprs[slot]
            cur.frame.seal(slot, live)
            self._gprs[slot] = live if slot in spec.policy.keep_gprs else 0
        self._handler_serial += 1
        ctx = _Context(f"{handler}#{self._handler_serial}", "handler",
                       spec.trust, spec.domain, spec.policy)
        self._stack.append(ctx)
        self.audit()
        return ctx.name

    def iret(self) -> str:
        """Return from the current handler to the preempted context.

        The handler context is destroyed together with its private vector
        state; the preempted context's authoritative GPR state is restored
        exactly, and its vector groups are restored lazily on first use.
        Returns the name of the context that becomes current.
        """
        if len(self._stack) < 2:
            raise StackError("cannot return from the base thread")
        handler = self._stack.pop()
        if any(slot in handler.frame for slot in self.gpr_names):
            raise StateIntegrityError(
                f"returning handler {handler.name!r} still has sealed GPRs"
            )
        # Vector groups the handler owns die with it; any copies sealed in
        # the handler's own frame are discarded as well.
        for group in range(self.num_groups):
            if self._group_owner[group] is handler:
                self._zero_group(group)
                self._group_owner[group] = None
        below = self._stack[-1]
        for slot in self.gpr_names:
            self._gprs[slot] = below.frame.unseal(slot)
        self.audit()
        return below.name

    def schedule(self, thread: str) -> None:
        """Switch to another thread, possibly in another protection domain.

        Only allowed at thread level (no active handler).  The current
        thread's GPRs and its live vector groups are sealed into its
        private frame; the target thread's GPRs are restored and its
        vector groups are restored lazily on first use.
        """
        if len(self._stack) != 1:
            raise StackError("schedule is only allowed at thread level")
        target = self._threads.get(thread)
        if target is None:
            raise UnknownContextError(f"unknown thread {thread!r}")
        current = self._stack[-1]
        if target is current:
            raise StackError(f"thread {thread!r} is already current")
        for slot in self.gpr_names:
            current.frame.seal(slot, self._gprs[slot])
        for group in range(self.num_groups):
            owner = self._group_owner[group]
            if owner is None:
                continue
            # At thread level the live owner can only be the current
            # thread; seal its copy and leave the register file zeroed.
            owner.frame.seal(_vslot(group), self._dump_group(group))
            self._zero_group(group)
            self._group_owner[group] = None
        for slot in self.gpr_names:
            self._gprs[slot] = target.frame.unseal(slot)
        self._stack = [target]
        self.audit()

    def exit_thread(self) -> None:
        """Terminate the current thread and destroy its private frame.

        Only allowed at thread level.  The machine becomes idle; use
        :meth:`start` or :meth:`schedule` to run another thread.
        """
        if len(self._stack) != 1:
            raise StackError("thread exit is only allowed at thread level")
        ctx = self._stack.pop()
        for group in range(self.num_groups):
            if self._group_owner[group] is ctx:
                self._zero_group(group)
                self._group_owner[group] = None
        # The thread's whole private state (frame included) dies with it.
        del self._threads[ctx.name]
        self.audit()

    # ------------------------------------------------------------------ #
    # invariant
    # ------------------------------------------------------------------ #

    def audit(self) -> None:
        """Verify the single-valid-copy invariant; raise on violation.

        * GPRs: every context except the current one holds exactly one
          sealed copy of every GPR slot in its private frame; the current
          context holds none (its authoritative copy is the register
          file).
        * Vector groups: the live owner of a group (if any) is a context
          of the current interrupt stack and holds no sealed copy of that
          group — its copy is the register file.  Every context holds at
          most one sealed copy of each group in its private frame
          (enforced by :meth:`PrivateFrame.seal`).
        """
        contexts = list(self._threads.values())
        contexts += [c for c in self._stack if c.kind == "handler"]
        current = self._stack[-1] if self._stack else None
        problems = []
        for slot in self.gpr_names:
            holders = {c.name for c in contexts if slot in c.frame}
            expected = {c.name for c in contexts if c is not current}
            if holders != expected:
                problems.append(
                    f"GPR {slot}: sealed in {sorted(holders)}, "
                    f"expected exactly {sorted(expected)}"
                )
        for group in range(self.num_groups):
            owner = self._group_owner[group]
            if owner is None:
                continue
            if all(owner is not c for c in self._stack):
                problems.append(
                    f"vector group {group}: live owner {owner.name!r} is "
                    f"not in the current interrupt stack"
                )
            if _vslot(group) in owner.frame:
                problems.append(
                    f"vector group {group}: live owner {owner.name!r} "
                    f"also holds a sealed copy"
                )
        if problems:
            raise StateIntegrityError(
                "single-valid-copy invariant violated: " + "; ".join(problems)
            )

    # ------------------------------------------------------------------ #
    # inspection helpers (for tests and debugging)
    # ------------------------------------------------------------------ #

    @property
    def current_name(self):
        """Name of the current context, or ``None`` when idle."""
        return self._stack[-1].name if self._stack else None

    def frame_slots(self, context_name: str) -> frozenset:
        """Slot names currently sealed in ``context_name``'s private frame."""
        return self._find_context(context_name).frame.slots()

    def frame_snapshot(self, context_name: str) -> dict:
        """Copy of ``context_name``'s sealed slots (inspection only)."""
        return self._find_context(context_name).frame.snapshot()

    def group_owner_name(self, group: int):
        """Name of the live owner of a lazy vector group, or ``None``."""
        self._check_group(group)
        owner = self._group_owner[group]
        return owner.name if owner is not None else None

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _require_current(self):
        if not self._stack:
            raise StackError("machine is idle (no current context)")
        return self._stack[-1]

    def _find_context(self, name):
        for ctx in list(self._threads.values()) + list(self._stack):
            if ctx.name == name:
                return ctx
        raise UnknownContextError(f"unknown context {name!r}")

    def _check_gpr(self, name):
        if name not in self._gprs:
            raise UnknownRegisterError(f"unknown GPR {name!r}")

    def _check_vreg(self, index):
        if not 0 <= index < self.num_vector_pairs * 2:
            raise UnknownRegisterError(f"unknown vector register v{index}")

    def _check_pair(self, pair):
        if not 0 <= pair < self.num_vector_pairs:
            raise UnknownRegisterError(f"unknown vector pair {pair}")

    def _check_group(self, group):
        if not 0 <= group < self.num_groups:
            raise UnknownRegisterError(f"unknown vector group {group}")

    def _check_vector_value(self, values):
        vec = tuple(int(v) & MASK64 for v in values)
        if len(vec) != self.vector_lanes:
            raise ValueError(
                f"vector value must have {self.vector_lanes} lanes, "
                f"got {len(vec)}"
            )
        return vec

    def _group_of_vreg(self, index):
        pair = index // 2
        return pair // self.pairs_per_group

    def _group_pairs(self, group):
        base = group * self.pairs_per_group
        return frozenset(range(base, base + self.pairs_per_group))

    def _group_vregs(self, group):
        base = group * self.pairs_per_group * 2
        return range(base, base + self.pairs_per_group * 2)

    def _dump_group(self, group):
        return tuple(self._vregs[i] for i in self._group_vregs(group))

    def _load_group(self, group, values):
        regs = list(self._group_vregs(group))
        if len(values) != len(regs):
            raise StateIntegrityError(
                f"vector group {group}: expected {len(regs)} saved "
                f"registers, got {len(values)}"
            )
        for i, vec in zip(regs, values):
            self._vregs[i] = vec

    def _zero_group(self, group):
        for i in self._group_vregs(group):
            self._vregs[i] = self._zero_vec

    def _acquire_group(self, group):
        """Lazy first-use ownership transfer of a vector group."""
        cur = self._require_current()
        owner = self._group_owner[group]
        if owner is cur:
            return
        slot = _vslot(group)
        if slot in cur.frame:
            # The current context used this group before and its copy was
            # sealed (by a preempting handler or a schedule).  Resume it.
            if owner is not None:
                raise StateIntegrityError(
                    f"vector group {group}: {cur.name!r} holds a sealed "
                    f"copy while {owner.name!r} is the live owner"
                )
            self._load_group(group, cur.frame.unseal(slot))
            self._group_owner[group] = cur
            return
        if owner is not None:
            # First use while another context's copy is still live: seal
            # the owner's copy into its private frame now (this is the
            # deferred lazy save), then hand the register file over.
            owner.frame.seal(slot, self._dump_group(group))
            inherit = (
                cur.policy is not None
                and cur.trust >= owner.trust
                and self._group_pairs(group) <= cur.policy.keep_vector_pairs
            )
            if not inherit:
                self._zero_group(group)
        else:
            # No live owner: the register-file contents are dead.
            self._zero_group(group)
        self._group_owner[group] = cur
