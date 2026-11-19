"""Login-name migration planning.

Model
-----
An account has an immutable numeric id, a current primary login name, and
legacy aliases that stay reserved until an expiry date.  Two normalisation
functions are injectable:

``old_normalize``
    The legacy rule.  Used to validate the pre-migration data: existing
    primaries must already be unique under it.

``new_normalize``
    The rule being migrated to.  Every occupation key after the switch is
    computed with it (primary names *and* still-valid aliases).

Planning rules
--------------
* Within one group, accounts may swap primary names that are not reserved by
  a still-valid alias.
* A still-valid alias always occupies its key.  A target name may fold onto an
  alias key only when the alias belongs to the same account (that is the
  "spellings of one account merge" case).
* Groups that want to occupy the same key, or that swap names, are connected.
  Connectivity is transitive.
* A key claimed by two different accounts defers the whole connected rename
  component, not just the group that raised it.
* The plan/preview lists every conflict together with the account ids that own
  the contended key.  Applying a plan only publishes the renames of components
  without conflicts, so independent groups switch normally.

Example
-------
>>> from datetime import date
>>> acct = Account(1, "Alice")
>>> plan = plan_migration([acct], [Group("g", (1,), {1: "alice"})],
...                       old_normalize=str.lower, new_normalize=str.casefold,
...                       on=date(2026, 11, 20))
>>> plan.renames
{1: 'alice'}
>>> apply_plan(plan).find("ALICE").account_id
1
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

Normalizer = Callable[[str], str]

# Conflict reason codes (stable strings, safe to compare in tests/UI).
DUPLICATE_TARGET_KEY = "duplicate-target-key"
RESERVED_BY_ALIAS = "reserved-by-live-alias"
OWNED_BY_OTHER_ACCOUNT = "owned-by-another-account"
COMPONENT_DEFERRED = "connected-group-deferred"
PREEXISTING_COLLISION = "pre-existing-collision"


class MigrationError(ValueError):
    """Raised when input data is structurally invalid or a lookup is ambiguous."""


@dataclass(frozen=True)
class Alias:
    """A legacy login name kept reserved through ``expires_on`` (inclusive)."""

    name: str
    expires_on: date

    def is_live(self, on: date) -> bool:
        return self.expires_on >= on


@dataclass(frozen=True)
class Account:
    """An account with an immutable numeric id."""

    account_id: int
    primary: str
    aliases: Tuple[Alias, ...] = ()

    def live_aliases(self, on: date) -> Tuple[Alias, ...]:
        return tuple(a for a in self.aliases if a.is_live(on))


@dataclass(frozen=True)
class Group:
    """Accounts proposed to be renamed together.

    ``targets`` maps an account id to the primary name it should take.  An
    account listed in the group but absent from ``targets`` keeps its current
    primary; it is still part of the connected rename set.
    """

    name: str
    account_ids: Tuple[int, ...]
    targets: Mapping[int, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise MigrationError("group name must not be empty")
        if not self.account_ids:
            raise MigrationError(f"group {self.name!r} has no accounts")
        if len(set(self.account_ids)) != len(self.account_ids):
            raise MigrationError(f"group {self.name!r} repeats an account id")
        unknown = set(self.targets) - set(self.account_ids)
        if unknown:
            raise MigrationError(
                f"group {self.name!r} targets unknown member ids {sorted(unknown)}"
            )
        for account_id, target in self.targets.items():
            if not isinstance(target, str) or not target:
                raise MigrationError(
                    f"group {self.name!r} has an empty target for account {account_id}"
                )


@dataclass(frozen=True)
class Conflict:
    """A blocked rename, including the account ids that own the key."""

    reason: str
    key: str
    account_ids: Tuple[int, ...]
    group_names: Tuple[str, ...] = ()

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        owners = ", ".join(str(i) for i in self.account_ids)
        groups = ", ".join(self.group_names) or "-"
        return f"{self.reason}: key={self.key!r} owners=[{owners}] groups=[{groups}]"


@dataclass
class MigratedState:
    """Post-migration lookup state, indexed by the new rule."""

    new_normalize: Normalizer
    _accounts: Dict[int, Account] = field(default_factory=dict, repr=False)
    _primary_by_key: Dict[str, List[int]] = field(default_factory=dict, repr=False)
    _alias_by_key: Dict[str, List[int]] = field(default_factory=dict, repr=False)

    def _register(self, account: Account, on: date) -> None:
        self._accounts[account.account_id] = account
        self._primary_by_key.setdefault(
            self.new_normalize(account.primary), []
        ).append(account.account_id)
        for alias in account.live_aliases(on):
            self._alias_by_key.setdefault(
                self.new_normalize(alias.name), []
            ).append(account.account_id)

    def find(self, name: str) -> Optional[Account]:
        """Resolve ``name`` under the new rule, or return ``None``.

        A live alias keeps occupying its key, so if primaries and aliases
        resolve the key to more than one distinct account the lookup is
        ambiguous and :class:`MigrationError` is raised rather than silently
        preferring one owner.
        """
        key = self.new_normalize(name)
        owners = set(self._primary_by_key.get(key, ()))
        owners.update(self._alias_by_key.get(key, ()))
        if len(owners) == 1:
            return self._accounts[next(iter(owners))]
        if len(owners) > 1:
            raise MigrationError(
                f"login name {name!r} (key {key!r}) is ambiguous between accounts "
                f"{sorted(owners)}"
            )
        return None

    def primary_of(self, account_id: int) -> str:
        return self._accounts[account_id].primary

    def account_ids(self) -> Tuple[int, ...]:
        return tuple(sorted(self._accounts))


@dataclass
class MigrationPlan:
    """Preview of one migration run."""

    on: date
    old_normalize: Normalizer = field(repr=False)
    new_normalize: Normalizer = field(repr=False)
    accounts: Mapping[int, Account] = field(repr=False)
    groups: Tuple[Group, ...] = field(repr=False)
    renames: Dict[int, str] = field(default_factory=dict)
    conflicts: Tuple[Conflict, ...] = ()
    deferred: Tuple[Conflict, ...] = ()

    @property
    def deferred_account_ids(self) -> Tuple[int, ...]:
        ids = {i for c in self.deferred for i in c.account_ids}
        return tuple(sorted(ids))

    @property
    def deferred_group_names(self) -> Tuple[str, ...]:
        names = {n for c in self.deferred for n in c.group_names}
        return tuple(sorted(names))

    def is_deferred(self, group_name: str) -> bool:
        return group_name in self.deferred_group_names

    def final_primary(self, account_id: int) -> str:
        account = self.accounts[account_id]
        return self.renames.get(account_id, account.primary)

    def apply(self) -> MigratedState:
        state = MigratedState(self.new_normalize)
        for account in self.accounts.values():
            target = self.renames.get(account.account_id, account.primary)
            state._register(
                Account(account.account_id, target, account.aliases), self.on
            )
        return state

    def find(self, name: str) -> Optional[Account]:
        """Convenience: apply the plan and resolve ``name``."""
        return self.apply().find(name)


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------


def _validate_accounts(accounts: Iterable[Account]) -> Dict[int, Account]:
    by_id: Dict[int, Account] = {}
    legacy_primaries: Dict[str, int] = {}
    for account in accounts:
        if not isinstance(account.account_id, int) or isinstance(account.account_id, bool):
            raise MigrationError(f"invalid account id {account.account_id!r}")
        if account.account_id < 0:
            raise MigrationError(f"invalid account id {account.account_id!r}")
        if account.account_id in by_id:
            raise MigrationError(f"duplicate account id {account.account_id}")
        if not isinstance(account.primary, str) or not account.primary:
            raise MigrationError(f"account {account.account_id} has an empty primary name")
        for alias in account.aliases:
            if not isinstance(alias.name, str) or not alias.name:
                raise MigrationError(
                    f"account {account.account_id} has an empty alias name"
                )
        by_id[account.account_id] = account
    return by_id


def _validate_legacy_uniqueness(
    accounts: Mapping[int, Account], old_normalize: Normalizer
) -> None:
    seen: Dict[str, int] = {}
    for account in accounts.values():
        key = old_normalize(account.primary)
        if key in seen:
            raise MigrationError(
                f"legacy rule maps {account.primary!r} and account "
                f"{seen[key]}'s primary to the same key {key!r}"
            )
        seen[key] = account.account_id


def _live_alias_index(
    accounts: Mapping[int, Account], on: date, new_normalize: Normalizer
) -> Dict[str, List[Tuple[int, Alias]]]:
    index: Dict[str, List[Tuple[int, Alias]]] = defaultdict(list)
    for account in accounts.values():
        for alias in account.live_aliases(on):
            index[new_normalize(alias.name)].append((account.account_id, alias))
    for key, entries in index.items():
        owners = {owner for owner, _ in entries}
        if len(owners) > 1:
            raise MigrationError(
                f"live aliases of accounts {sorted(owners)} collide on "
                f"new-rule key {key!r}"
            )
    return index


def _resolve_targets(
    accounts: Mapping[int, Account], groups: Sequence[Group]
) -> Tuple[Dict[int, str], Dict[int, str]]:
    targets: Dict[int, str] = {}
    group_of: Dict[int, str] = {}
    for group in groups:
        for account_id in group.account_ids:
            if account_id not in accounts:
                raise MigrationError(
                    f"group {group.name!r} references unknown account {account_id}"
                )
            if account_id in group_of:
                raise MigrationError(
                    f"account {account_id} appears in groups "
                    f"{group_of[account_id]!r} and {group.name!r}"
                )
            group_of[account_id] = group.name
            targets[account_id] = group.targets.get(
                account_id, accounts[account_id].primary
            )
    return targets, group_of


def _components(
    groups: Sequence[Group],
    targets: Mapping[int, str],
    accounts: Mapping[int, Account],
    new_normalize: Normalizer,
) -> List[List[Group]]:
    """Connect groups whose proposed names touch the same key.

    A group touches the new-rule key of every member's current primary and of
    every member's target.  Two groups touching the same key are connected;
    this covers both "want the same name" and "swap names" (one group's target
    is the other's source).  Connectivity is transitive.
    """
    parent = list(range(len(groups)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    keysets: List[set] = []
    for group in groups:
        keys = set()
        for account_id in group.account_ids:
            keys.add(new_normalize(accounts[account_id].primary))
            keys.add(new_normalize(targets[account_id]))
        keysets.append(keys)

    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            if keysets[i] & keysets[j]:
                union(i, j)

    buckets: Dict[int, List[Group]] = defaultdict(list)
    for i, group in enumerate(groups):
        buckets[find(i)].append(group)
    return list(buckets.values())


def _component_conflicts(
    component: Sequence[Group],
    accounts: Mapping[int, Account],
    targets: Mapping[int, str],
    alias_index: Mapping[str, Sequence[Tuple[int, Alias]]],
    group_of: Mapping[int, str],
    new_normalize: Normalizer,
) -> List[Conflict]:
    conflicts: List[Conflict] = []
    member_ids = {i for g in component for i in g.account_ids}

    def groups_for(account_ids: Iterable[int]) -> Tuple[str, ...]:
        names = {group_of[i] for i in account_ids if i in group_of}
        return tuple(sorted(names))

    # 1. Two different accounts proposing the same key.
    by_target_key: Dict[str, List[int]] = defaultdict(list)
    for account_id in member_ids:
        by_target_key[new_normalize(targets[account_id])].append(account_id)
    for key, ids in by_target_key.items():
        if len(ids) > 1:
            conflicts.append(
                Conflict(
                    reason=DUPLICATE_TARGET_KEY,
                    key=key,
                    account_ids=tuple(sorted(ids)),
                    group_names=groups_for(ids),
                )
            )

    # 2. A live alias reserves its key against every other account.
    # 3. An account outside the component keeps its current primary key.
    # 4. A primary that is being vacated by a swap must not itself be
    #    reserved by another account's live alias: only *non-reserved*
    #    primaries may be exchanged.
    outside = [
        account
        for account in accounts.values()
        if account.account_id not in member_ids
    ]
    for account_id in sorted(member_ids):
        key = new_normalize(targets[account_id])
        for owner_id, _alias in alias_index.get(key, ()):
            if owner_id != account_id:
                conflicts.append(
                    Conflict(
                        reason=RESERVED_BY_ALIAS,
                        key=key,
                        account_ids=tuple(sorted({owner_id, account_id})),
                        group_names=groups_for({owner_id, account_id}),
                    )
                )
        for other in outside:
            if new_normalize(other.primary) == key:
                conflicts.append(
                    Conflict(
                        reason=OWNED_BY_OTHER_ACCOUNT,
                        key=key,
                        account_ids=tuple(sorted({other.account_id, account_id})),
                        group_names=groups_for({account_id}),
                    )
                )

    # 4. A handoff (a target equal to another member's current primary) may
    #    only exchange a *non-reserved* primary.  A live alias anywhere --
    #    including the receiving account's own -- reserves the name, so such
    #    a swap is deferred rather than allowed to relocate a reserved name.
    target_key_owner: Dict[str, List[int]] = defaultdict(list)
    for account_id in member_ids:
        target_key_owner[new_normalize(targets[account_id])].append(account_id)
    for account_id in sorted(member_ids):
        source_key = new_normalize(accounts[account_id].primary)
        receivers = [
            other
            for other in target_key_owner.get(source_key, ())
            if other != account_id
        ]
        if not receivers:
            continue
        for owner_id, _alias in alias_index.get(source_key, ()):
            conflicts.append(
                Conflict(
                    reason=RESERVED_BY_ALIAS,
                    key=source_key,
                    account_ids=tuple(sorted({owner_id, account_id, *receivers})),
                    group_names=groups_for({account_id, *receivers}),
                )
            )

    return _dedupe(conflicts)


def _dedupe(conflicts: Iterable[Conflict]) -> List[Conflict]:
    """Keep one conflict per contested (key, ownership set).

    The same pair of accounts can be reported by more than one rule (for
    example a live alias that also collides in the pre-existing data).  A
    preview should show that key once, attributed to the accounts that hold
    it, so the first -- most specific -- reason wins.
    """
    seen = set()
    result = []
    for conflict in conflicts:
        signature = (conflict.key, conflict.account_ids)
        if signature in seen:
            continue
        seen.add(signature)
        result.append(conflict)
    return result


def _preexisting_conflicts(
    accounts: Mapping[int, Account],
    final_primaries: Mapping[int, str],
    alias_index: Mapping[str, Sequence[Tuple[int, Alias]]],
    group_of: Mapping[int, str],
    new_normalize: Normalizer,
) -> List[Conflict]:
    """Collisions that survive the planned renames (bad legacy data)."""
    conflicts: List[Conflict] = []

    def groups_for(account_ids: Iterable[int]) -> Tuple[str, ...]:
        return tuple(sorted({group_of[i] for i in account_ids if i in group_of}))

    by_key: Dict[str, List[int]] = defaultdict(list)
    for account_id, primary in final_primaries.items():
        by_key[new_normalize(primary)].append(account_id)
    for key, ids in by_key.items():
        if len(ids) > 1:
            conflicts.append(
                Conflict(
                    reason=PREEXISTING_COLLISION,
                    key=key,
                    account_ids=tuple(sorted(ids)),
                    group_names=groups_for(ids),
                )
            )
    for account_id, primary in final_primaries.items():
        key = new_normalize(primary)
        for owner_id, _alias in alias_index.get(key, ()):
            if owner_id != account_id:
                conflicts.append(
                    Conflict(
                        reason=PREEXISTING_COLLISION,
                        key=key,
                        account_ids=tuple(sorted({owner_id, account_id})),
                        group_names=groups_for({owner_id, account_id}),
                    )
                )
    return _dedupe(conflicts)


def plan_migration(
    accounts: Iterable[Account],
    groups: Sequence[Group] = (),
    *,
    old_normalize: Normalizer,
    new_normalize: Normalizer,
    on: Optional[date] = None,
) -> MigrationPlan:
    """Build a :class:`MigrationPlan` without changing any state.

    ``on`` is the migration date; aliases expiring on that date are still
    live (they are kept *through* their expiry date).
    """
    if not callable(old_normalize) or not callable(new_normalize):
        raise MigrationError("normalisation functions must be callable")
    on = on or date.today()

    by_id = _validate_accounts(accounts)
    _validate_legacy_uniqueness(by_id, old_normalize)
    alias_index = _live_alias_index(by_id, on, new_normalize)
    targets, group_of = _resolve_targets(by_id, groups)

    plan = MigrationPlan(
        on=on,
        old_normalize=old_normalize,
        new_normalize=new_normalize,
        accounts=by_id,
        groups=tuple(groups),
    )

    all_conflicts: List[Conflict] = []
    deferred: List[Conflict] = []
    components = _components(tuple(groups), targets, by_id, new_normalize)
    for component in components:
        component_conflicts = _component_conflicts(
            component, by_id, targets, alias_index, group_of, new_normalize
        )
        if component_conflicts:
            all_conflicts.extend(component_conflicts)
            deferred.append(
                Conflict(
                    reason=COMPONENT_DEFERRED,
                    key=component_conflicts[0].key,
                    account_ids=tuple(
                        sorted({i for c in component_conflicts for i in c.account_ids})
                    ),
                    group_names=tuple(sorted(g.name for g in component)),
                )
            )
            continue
        for group in component:
            for account_id in group.account_ids:
                target = targets[account_id]
                if target != by_id[account_id].primary:
                    plan.renames[account_id] = target

    final_primaries = {
        account_id: plan.renames.get(account_id, account.primary)
        for account_id, account in by_id.items()
    }
    all_conflicts.extend(
        _preexisting_conflicts(
            by_id, final_primaries, alias_index, group_of, new_normalize
        )
    )

    plan.conflicts = tuple(_dedupe(all_conflicts))
    plan.deferred = tuple(deferred)
    return plan


def apply_plan(plan: MigrationPlan) -> MigratedState:
    """Apply a plan and return the resulting lookup state."""
    return plan.apply()


def build_plan(
    accounts: Iterable[Account], groups: Sequence[Group] = (), **kwargs
) -> MigrationPlan:
    """Alias for :func:`plan_migration`."""
    return plan_migration(accounts, groups, **kwargs)
