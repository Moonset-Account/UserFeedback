# Login-name migration planner

Plans and previews a login-name migration from a legacy normalisation rule to a
new, more aggressive case-folding rule, while legacy aliases stay reserved
until their expiry date.

## Entry points

| What | Where |
| --- | --- |
| Package | `login_migration/` |
| Public API | `login_migration/__init__.py` |
| Implementation | `login_migration/migrator.py` |
| Acceptance tests | `tests/test_acceptance.py` |

Run the real test suite from the workspace root:

```sh
python3 -m unittest discover -s tests -t .
```

Requires Python 3.11.6 (no third-party dependencies). The module docstring of
`login_migration/migrator.py` is also executable documentation:

```sh
python3 -m doctest login_migration/migrator.py
```

## Model

```python
Account(account_id: int, primary: str, aliases: tuple[Alias, ...] = ())
Alias(name: str, expires_on: date)          # live through expires_on, inclusive
Group(name: str, account_ids: tuple[int, ...], targets: Mapping[int, str] = {})
```

`account_id` is the immutable numeric id. `Group.targets` maps an account id to
the primary name it should take; a listed account with no target keeps its
current primary but still participates in the connected rename set.

Both normalisers are injected:

```python
plan = plan_migration(
    accounts,
    groups,
    old_normalize=legacy_rule,   # interprets existing data; must be unique
    new_normalize=new_rule,      # defines every post-migration key
    on=date(2026, 11, 20),       # aliases expiring on/after this date stay live
)
```

## Rules enforced

1. **Group rename / swap.** Within one group, two accounts may swap primary
   names that are *not reserved*. A name is reserved if any live alias folds
   onto it, including the receiving account's own alias.
2. **Aliases always occupy their key.** No primary may claim a key held by
   another account's live alias. An account may absorb one of its own alias
   spellings (the "same account's spellings merge" case).
3. **Connectivity.** Groups that want to occupy the same name, or that swap
   names (one group's target is another's source), are connected, transitively.
4. **Defer the whole component.** If two different accounts produce the same
   new-rule key, or any rename collides with reserved/outside ownership, the
   entire connected component is deferred. Independent groups still switch.

## Preview and apply

`plan_migration` performs no mutation. The returned `MigrationPlan` exposes:

- `renames` — `account_id -> new primary` for components that will switch.
- `conflicts` — every detected conflict, each naming `key` and the
  `account_ids` that own it.
- `deferred` — the components held back, with their group names and causes.
- `deferred_group_names` / `deferred_account_ids` / `is_deferred(...)`.
- `find(name)` — convenience for `apply_plan(plan).find(name)`.

`apply_plan(plan)` returns a `MigratedState`; `state.find(name)` resolves a name
under the **new** rule. If a key is owned by more than one distinct account
(pre-existing bad data), `find` raises `MigrationError` rather than silently
picking an owner — a live alias is never allowed to be shadowed.

Structurally invalid input (duplicate ids, unknown group members, one account
in two groups, legacy-rule collisions, cross-account live-alias collisions)
raises `MigrationError` during planning.

## Verification scope

The acceptance suite exercises, with real assertions:

- accepted paths: single rename, injectable rules, `ß`/`ss` merge under the
  new rule, two-account and three-way rotations, independent groups switching;
- rejected/deferred paths: reserved-name swaps, live alias occupancy, duplicate
  target keys, target owned by an ungrouped account, transitively deferred
  components;
- boundary/interleaving cases: alias expiring exactly on the migration date
  (still live) versus the day before (freed), the same key colliding in both
  deferred and pre-existing senses, and apply idempotence.

All checks use local synthetic fixtures only; nothing contacts a network or a
real account directory.
