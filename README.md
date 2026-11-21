# Co-owned resource model

A small, dependency-free resource model for two (or more) controllers that
each declare a *part* of the same resource. Declarations can be re-applied any
number of times. Ownership is tracked per leaf field, shared when values
agree, and a rejected declaration never leaves a partial write behind.

- Runtime: Node.js 24.15.0 (ES modules, `node:test`)
- Entry point: `src/resource-model.mjs`
- Tests: `tests/resource-model.test.mjs` (`*.test.mjs`)

## Running

```sh
node --test            # discovers tests/*.test.mjs
# or, equivalently
npm test
```

No dependencies, no network access, no build step.

## API

```js
import { createResourceStore, OwnershipConflictError } from './src/resource-model.mjs';

const store = createResourceStore({});            // optional initial snapshot
const result = store.apply('controller-a', { spec: { replicas: 3 } });
```

- `createResourceStore(initialResource?)` → `ResourceStore`
- `store.apply(owner, declaration, options?)` → `{ resource, owners, changedPaths }`
  - `owner` — non-empty string identifying the applicant
  - `declaration` — plain object; re-appliable
  - `options.force` — `true` (all conflicts), a path string, or an array of
    path strings. A forced path also covers conflicts strictly above or below
    the same path (a shape change is one collision reported at one path).
  - throws `OwnershipConflictError` with `.paths` (sorted conflict paths) and
    `.conflicts` (`{ path, reason, currentValue, requestedValue, owners }`)
- `store.resource` / `store.get()` — deep copy of the current resource
- `store.snapshot()` → `{ resource, owners }` where `owners` is a sorted array
  of `{ path, owners: string[] }`
- `store.ownersOf(path)` → sorted owner names for one field path
- `store.ownedBy(owner)` → sorted field paths held by one controller
- `OwnershipConflictError` — rejected apply, listing every blocking path

## Field paths

```
spec.replicas
spec.config.level
spec.containers[name=web].image
spec.containers[name=web].ports[name=http].port
```

Object members join with `.`; container-list elements are identified by
`[name=<element name>]`. Names are escaped: `\` before `.`, `[`, `]` and `\`.
Every path segment is matched as a whole segment, so an element named `a.b`
is never confused with an element named `a`.

## Rules

1. **Ordinary objects are managed leaf by leaf.** Owning `spec.config` never
   happens; owning `spec.config.level` does. A declaration that only partly
   repeats a live object never takes (or releases) the other members.

2. **Container lists are identified by `name`, not position.** A list is a
   container list when every element is a plain object with a unique,
   non-empty string `name` (and an empty array is an empty container list).
   Anything else — scalars, `null`, mixed, unnamed or duplicate-named
   elements — is a plain leaf value and is owned as one field.

3. **Reordering cannot transfer ownership.** List order never appears in a
   path, so moving elements around in a declaration is a no-op on the stored
   value and on ownership. New elements are appended; existing elements keep
   their live order.

4. **Equal values are co-owned.** If two controllers declare the same value
   for the same leaf, both become owners of that leaf.

5. **Omission releases only the applicant's share.** A later declaration that
   leaves a doubly owned field out removes exactly one owner. The field value
   stays while at least one owner remains. The field is deleted only when the
   last owner leaves (empty parent objects and empty list elements are pruned
   with it).

6. **Changing another controller's value is rejected as a whole.** An apply
   that would write a different value into a field owned by someone else
   writes nothing at all and throws `OwnershipConflictError`, naming every
   conflicting path. This includes shape collisions (leaf ↔ object, leaf ↔
   list, object ↔ list) and applies to the entire declaration: if an apply
   both adds new fields and collides somewhere, both the resource value and
   the owner table are left byte-for-byte identical to their pre-apply state.

7. **Explicit force transfers the field.** `apply(owner, decl, { force: true })`
   (or a path / path list) accepts the conflicting write; the applicant becomes
   the sole owner of the written field and the displaced owners lose it. Force
   never reaches unrelated sibling paths.

8. **A controller may freely rewrite what it alone owns** — same leaf, new
   value, or even a different shape/type — because there is no other owner to
   displace.

9. **Empty declarations are no-ops.** `undefined`, `{}` and `[]` declare
   nothing, so they never materialize or erase structure.

## Validation behaviours covered by the tests

Every scenario below is asserted in `tests/resource-model.test.mjs`; the tests
are the executable specification.

Acceptable results:

- idempotent re-application; `changedPaths: []` on a repeat
- leaf-level ownership of objects
- co-ownership on equal values, and after reordering a container list
- releasing one share while another owner keeps the value
- last-owner deletion with parent/element pruning
- force transfer, including scoped force and force across a shape change
- sole-owner value/shape changes without force
- nested `[name=...]` paths, escaped names containing `.`/`[`/`]`
- unowned pre-existing values being claimed

Rejected results (throws, state unchanged):

- different value for a field owned by another controller
- a co-owner silently changing a shared value
- a conflicting field nested inside a container list
- shape collisions in both directions (leaf → object, object → leaf)
- a leaf list replaced by a container list
- an apply mixing a conflict with new fields (nothing is written)

Boundary / interleaving scenarios:

- reorder by a controller that owns no element fields: no ownership change,
  and its later omission still cannot delete anything
- reorder plus new field plus release in one apply, then a multi-conflict
  apply that must roll back both the leaf and ownership of the new field
- scoped force where another conflict is not covered (still rejected)
- forced transfer where the displaced owner later omits the field
- names `a` and `a.b` as distinct list elements, with force on `a` not
  reaching `a.b`
- removal of one leaf preserving an adjacent field owned by another controller

## Files

```
package.json                  # type: module, node --test entry
src/resource-model.mjs        # implementation
tests/resource-model.test.mjs # node:test acceptance suite
README.md
```

## Scope

Local infrastructure component only. No network calls, no external targets, no
credential access: inputs are synthetic objects and the suite uses ordinary
`node:test` assertions.
