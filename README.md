# Extension release package reviewer

A dependency-free Node.js module for release review of browser-extension
packages. It expands each browser variant by its own loading conditions,
collects the features, site scopes, and permission-request timing declared by
the resources that variant actually loads, and compares them with that
variant's approved scope.

The implementation uses only Node.js 24 built-ins. It performs no network
access, reads no credentials, installs no dependencies, and ships only local
synthetic review fixtures.

## Run tests

```bash
node --version # v24.15.0
node --test tests/*.test.mjs
```

CLI example:

```bash
node src/cli.mjs examples/synthetic-package.mjs
node src/cli.mjs examples/synthetic-package.mjs --json
node src/cli.mjs examples/synthetic-package.mjs --strict
```

The normal CLI exits `1` when there are blocking errors. Residual optional
requests are warnings by default; `--strict` or `--strict-residual` makes them
blocking.

## Public API

```js
import {
  reviewPackage,
  reviewVariant,
  reviewManifest,
  expandResources,
  evaluateCondition,
  siteCovers,
  normalizeSiteScope,
  formatTextReport,
} from './src/index.mjs';
```

The default export is also callable: `import review from './src/index.mjs';
const report = review(pkg, options)`. `review`, `auditPackage`, and
`auditManifest` aliases are exported as well. Compatibility ESM entry points
are available at `src/index.js` and the package-root `index.js`.

### `reviewPackage(packageInput, options?)`

Reviews all variants. `packageInput.variants` may be an array or an object map.
Approvals may be embedded in each variant or supplied in
`packageInput.approvals[variantId]`.

`options`:

- `strict` — release approval requires zero warnings.
- `strictResidual` — give residual optional requests `error` severity.
- `featureAttributionApprovesScopes` — default `true`; an approved feature
  without an explicit permission/site scope list approves its declared request
  bundle. Set to `false` to require explicit grants.
- `sharedResources` — additional shared resource catalog.

The report contains `ok`, `releaseApproved`, `summary`, `issues`, `variants`,
and `getVariant(id)`.

### `reviewVariant(variant, approval?, options?)`

Reviews one variant and returns its loaded/skipped resources, feature set,
request buckets, comparisons, issues, and residual lists.

### `reviewManifest(manifest, approval?, options?)`

Convenience alias. If `manifest.variants` exists it behaves like
`reviewPackage`; otherwise it reviews a single manifest/variant.

`auditPackage` and `auditManifest` aliases are also exported.

## Input model

### Loading conditions

A condition may be:

```js
{ browser: 'chrome', minVersion: '120', flag: { ai: true } }
{ any: [{ browser: 'chrome' }, { browser: 'edge' }] }
{ all: [{ browser: 'firefox' }, { not: { channel: 'esr' } }] }
{ enabledFeature: 'pdf-export' }
'firefox' // shorthand for { browser: 'firefox' }
```

Supported keys include `browser(s)`, `excludeBrowsers`, `variant(s)`,
`version`, `minVersion`, `maxVersion`, `channel(s)`, `os(es)`,
`manifestVersion(s)`, `flag(s)`, `enabledFeature(s)`, `all`, `any`, `not`,
`context`, and a predicate `when(context)`. Unknown condition keys are
reported as `INVALID_LOAD_CONDITION` and fail closed by treating the resource
as not safely loaded.

### Resources and requests

Resources may be supplied directly under `variant.resources` or in a
package-level `resources` catalog referenced by `variant.loads`:

```js
const pkg = {
  name: 'example-extension',
  version: '1.0.0',
  resources: {
    'features/export.js': {
      optionalFeatures: [{ id: 'pdf-export' }],
      permissions: [
        { id: 'downloads', timing: 'optional', feature: 'pdf-export' },
      ],
      sites: [
        {
          pattern: 'https://files.example.com/export/*',
          timing: 'optional',
          feature: 'pdf-export',
        },
      ],
    },
  },
  variants: {
    chrome: {
      browser: 'chrome',
      version: '130.0',
      loads: [
        {
          resource: 'features/export.js',
          when: { browser: 'chrome', minVersion: '120' },
        },
      ],
      approval: {
        features: [
          {
            id: 'pdf-export',
            optional: true,
            optionalPermissions: ['downloads'],
            sites: ['https://files.example.com/export/*'],
          },
        ],
      },
    },
  },
};
```

Timing aliases normalize to:

- `install` — requested with installation;
- `enable` — requested after a feature is enabled;
- `optional` — an optional authorization prompt/request.

Common manifest aliases are also accepted, including `permissions`,
`optional_permissions`, `host_permissions`, and
`optional_host_permissions`.

### Approval shape

```js
approval: {
  features: [
    'core',
    {
      id: 'pdf-export',
      optional: true,
      permissions: ['downloads'],
      sites: ['https://files.example.com/*'],
    },
  ],
  installPermissions: ['storage'],
  enablePermissions: [{ id: 'activeTab', feature: 'ai-summary' }],
  optionalPermissions: ['geolocation'],
  sites: ['https://docs.example.com/*'],
}
```

A plain top-level permission/site grant applies regardless of timing. Timing
buckets match only that timing. Feature-scoped grants are attributed to that
feature. If a request omits `feature`, the reviewer can infer ownership from a
unique feature-scoped approval grant; multiple possible owners produce
`OPTIONAL_FEATURE_ATTRIBUTION_AMBIGUOUS`.

## Output highlights

Each variant report includes:

- `loadedResources` and `skippedResources` — the expanded feature combination;
- `features`, `approvedFeatures`, and `unapprovedFeatures`;
- `permissions.install`, `permissions.enable`, `permissions.optional`;
- `sites.install`, `sites.enable`, `sites.optional`;
- `extraInstallPermissions` — install-time requests outside approval;
- `extraPermissions` and `unapprovedSites`;
- `optionalRequestReviews` with `attributionStatus` and `scopeApproved`;
- `unattributedOptionalRequests`;
- `unimplementedOptionalFeatures`;
- `residualOptionalRequests` and `residualOptionalFeatures`;
- `issues` with stable codes and severity; `violations` and `warnings` are
  severity-filtered aliases.

Important issue codes:

| Code | Meaning |
| --- | --- |
| `EXTRA_INSTALL_PERMISSION` | An active install-time permission is not approved. |
| `UNAPPROVED_ENABLE_PERMISSION` | An active post-enable permission is outside scope. |
| `UNAPPROVED_OPTIONAL_PERMISSION` | An optional permission is outside scope. |
| `UNAPPROVED_SITE_SCOPE` | A requested site scope is not covered by approval. |
| `OPTIONAL_REQUEST_WITHOUT_FEATURE` | Optional request has no feature attribution. |
| `OPTIONAL_FEATURE_NOT_APPROVED` | Its feature is not approved for this variant. |
| `OPTIONAL_FEATURE_ATTRIBUTION_AMBIGUOUS` | Approval maps the request to multiple features. |
| `RESIDUAL_OPTIONAL_REQUEST` | Optional feature has no loaded implementation but its request remains. |
| `INVALID_LOAD_CONDITION` | Condition cannot be evaluated safely. |
| `INVALID_SITE_SCOPE` | Site pattern cannot be parsed. |
| `NO_VARIANTS` | Package input does not declare a reviewable variant. |

## Site matching

`siteCovers(approved, requested)` understands `<all_urls>`, wildcard schemes,
`*.example.com` subdomains including the apex, explicit ports, and common path
prefix globs. For example, `https://*.example.com/api/*` covers
`https://api.example.com/api/v1/users`, but not
`https://evil-example.com/api`.

Complex glob containment that cannot be proven by the implemented rules is not
treated as approved; the request remains visible for review.

## Shared file names and residual requests

Every variant is reviewed from its own expansion. The same `shared.js` or
`features/export.js` file name can be loaded in Chrome and skipped in Firefox,
or vice versa. Results are never deduplicated across variants.

For optional features, the reviewer builds the implemented set from resources
that matched the variant's conditions. Requests belonging to optional feature
IDs with no loaded implementation are placed in
`residualOptionalRequests`. If such a request is also actively declared at the
variant/package level (for example, still present in install permissions), it
remains in the normal approval comparison as well as the residual list.

## Verification scope

The local `node:test` suite uses only synthesized package data. On Node
v24.15.0, `node --test tests/*.test.mjs` reports 40 tests / 40 passing. It
verifies:

- condition composition, browser/version/flag/feature gates, string shortcuts,
  and fail-closed unknown keys;
- site normalization and scheme/host/port/path boundaries;
- a two-browser synthetic package with shared file names;
- correct loaded versus skipped resources for each variant;
- extra install permission reporting;
- approved and unapproved site scopes;
- optional requests with approved ownership, missing ownership, outside scope,
  inferred ownership, and ambiguous ownership;
- skipped-resource permissions not becoming active extras;
- residual optional requests, including a request still physically declared at
  install time;
- strict residual mode;
- malformed variant entries and duplicate variant IDs;
- variant-map/approval-map/shared-catalog inputs;
- Chinese timing aliases;
- CLI text and parseable JSON output.

It does **not** execute or parse real extension code, contact browser stores or
external services, use credentials, or prove that a real browser's loader
accepts every custom condition schema. The schema is a review interchange
model with WebExtension-like aliases; production adoption should add a manifest
adapter for the exact browser manifest versions in scope.
