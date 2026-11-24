import { evaluateCondition, createVariantContext } from './conditions.mjs';
import { normalizeSiteScope } from './scopes.mjs';

const TIMING_INSTALL = 'install';
const TIMING_ENABLE = 'enable';
const TIMING_OPTIONAL = 'optional';

const SEVERITY_ERROR = 'error';
const SEVERITY_WARNING = 'warning';

function asArray(value) {
  if (value === undefined || value === null) return [];
  if (Array.isArray(value)) return value;
  if (value instanceof Set) return [...value];
  return [value];
}

function normalizeTiming(value, { optional = false, fallback = TIMING_INSTALL } = {}) {
  if (optional && (value === undefined || value === null || value === '')) return TIMING_OPTIONAL;
  if (value === undefined || value === null || value === '') return fallback;
  const text = String(value)
    .trim()
    .toLowerCase()
    .replace(/[\s-]+/g, '_');
  const map = {
    install: TIMING_INSTALL,
    installation: TIMING_INSTALL,
    at_install: TIMING_INSTALL,
    on_install: TIMING_INSTALL,
    install_time: TIMING_INSTALL,
    installed: TIMING_INSTALL,
    安装时: TIMING_INSTALL,
    安装: TIMING_INSTALL,
    enable: TIMING_ENABLE,
    enabled: TIMING_ENABLE,
    on_enable: TIMING_ENABLE,
    after_enable: TIMING_ENABLE,
    post_enable: TIMING_ENABLE,
    enablement: TIMING_ENABLE,
    activation: TIMING_ENABLE,
    runtime: TIMING_ENABLE,
    启用后: TIMING_ENABLE,
    启用时: TIMING_ENABLE,
    optional: TIMING_OPTIONAL,
    optionally: TIMING_OPTIONAL,
    on_request: TIMING_OPTIONAL,
    request: TIMING_OPTIONAL,
    可选: TIMING_OPTIONAL,
    可选申请: TIMING_OPTIONAL,
  };
  return map[text] ?? `unknown:${String(value)}`;
}

function addIssue(issues, issue) {
  issues.push({
    severity: SEVERITY_ERROR,
    ...issue,
  });
}

function isPermissionObject(value) {
  return value && typeof value === 'object' && !Array.isArray(value) &&
    (value.id !== undefined || value.permission !== undefined || value.name !== undefined || value.key !== undefined);
}

function isSiteObject(value) {
  return value && typeof value === 'object' && !Array.isArray(value) &&
    (value.pattern !== undefined || value.scope !== undefined || value.url !== undefined ||
      value.matchPattern !== undefined || value.host !== undefined || value.site !== undefined);
}

function catalogToEntries(resources) {
  if (!resources) return [];
  if (Array.isArray(resources)) {
    return resources.map((resource, index) => {
      if (typeof resource === 'string') return { name: resource, __index: index };
      return { ...resource, __index: index };
    });
  }
  if (typeof resources === 'object') {
    return Object.entries(resources).map(([name, resource]) => {
      if (typeof resource === 'string') return { name };
      return { name, ...resource };
    });
  }
  return [];
}

const CONDITION_SHORTCUT_KEYS = [
  'browser', 'browsers', 'excludeBrowsers', 'variant', 'variants', 'excludeVariants',
  'os', 'oses', 'channel', 'channels', 'manifestVersion', 'manifestVersions',
  'version', 'minVersion', 'maxVersion', 'flag', 'flags',
  'enabledFeature', 'enabledFeatures', 'all', 'any', 'not', 'when', 'context',
];

function conditionFromEntry(entry) {
  const explicit = entry.conditions ??
    entry.condition ??
    entry.loadConditions ??
    entry.loadCondition ??
    entry.loadWhen ??
    entry.when ??
    entry.loading ??
    entry.load ??
    entry.includeIf ??
    entry.if;
  if (explicit !== undefined) return explicit;

  const shortcut = {};
  for (const key of CONDITION_SHORTCUT_KEYS) {
    if (entry[key] !== undefined) shortcut[key] = entry[key];
  }
  return Object.keys(shortcut).length ? shortcut : true;
}

function combineConditions(a, b) {
  const parts = [a, b].filter((part) => part !== undefined);
  if (parts.length === 0) return true;
  if (parts.length === 1) return parts[0];
  return { all: parts };
}

function findCatalogResource(catalogByName, reference) {
  if (reference === undefined || reference === null) return undefined;
  if (typeof reference === 'string') return catalogByName.get(reference);
  const name = reference.resource ?? reference.name ?? reference.path ?? reference.file ?? reference.id;
  return catalogByName.get(name);
}

/**
 * Build the raw resource entries relevant to a variant, including references
 * into a package-level shared resource catalog.
 */
export function buildVariantResources(variant = {}, sharedResources) {
  const catalogEntries = catalogToEntries(sharedResources);
  const catalogByName = new Map(catalogEntries.map((entry) => [entry.name ?? entry.path ?? entry.id, entry]));
  const entries = [];

  for (const resource of catalogToEntries(variant.resources ?? variant.files ?? variant.entries)) {
    entries.push(resource);
  }

  const loads = asArray(variant.loads ?? variant.load ?? variant.resourceLoads ?? variant.includes);
  for (const load of loads) {
    const reference = typeof load === 'string' ? { resource: load } : load;
    const catalogResource = findCatalogResource(catalogByName, reference);
    const loadCondition = reference.conditions ?? reference.condition ?? reference.when ?? reference.loadWhen;
    const entry = {
      ...(catalogResource ? { ...catalogResource } : {}),
      ...reference,
      name: reference.name ?? reference.resource ?? reference.path ?? reference.file ?? catalogResource?.name,
    };
    entry.conditions = combineConditions(catalogResource ? conditionFromEntry(catalogResource) : true, loadCondition);
    entries.push(entry);
  }

  // A package-level catalog applies to every variant when the variant does not
  // declare an explicit resource list/load list. Conditions then decide.
  if (entries.length === 0 && catalogEntries.length > 0 && variant.useSharedCatalog !== false) {
    entries.push(...catalogEntries.map((entry) => ({ ...entry })));
  }

  return entries.map((entry, index) => ({
    ...entry,
    name: entry.name ?? entry.path ?? entry.file ?? entry.filename ?? entry.id ?? `resource-${index + 1}`,
    __index: index,
  }));
}

/**
 * Evaluate loading conditions and split resources into loaded/skipped.
 */
export function expandResources(variant = {}, sharedResources) {
  const context = createVariantContext(variant);
  const rawResources = buildVariantResources(variant, sharedResources);
  /** @type {any[]} */
  const loaded = [];
  /** @type {any[]} */
  const skipped = [];
  /** @type {any[]} */
  const conditionErrors = [];

  for (const raw of rawResources) {
    const condition = conditionFromEntry(raw);
    let matched;
    try {
      matched = evaluateCondition(condition, context);
    } catch (error) {
      matched = false;
      conditionErrors.push({
        code: 'INVALID_LOAD_CONDITION',
        severity: SEVERITY_ERROR,
        resourceIndex: raw.__index,
        resource: raw.name,
        message: error instanceof Error ? error.message : String(error),
        condition,
      });
    }

    const resource = {
      name: raw.name,
      path: raw.path ?? raw.file ?? raw.filename ?? raw.name,
      condition,
      matched,
      raw,
    };
    if (matched) loaded.push(resource);
    else {
      const invalid = conditionErrors.some((error) => error.resourceIndex === raw.__index);
      skipped.push({ ...resource, reason: invalid ? 'invalid-condition' : 'condition-not-matched' });
    }
  }

  return { context, loaded, skipped, conditionErrors, all: [...loaded, ...skipped] };
}

function mergeFeature(map, feature) {
  const id = feature.id;
  if (!map.has(id)) {
    map.set(id, {
      id,
      optional: false,
      implemented: false,
      resources: new Set(),
      loadedResources: new Set(),
      declarations: [],
    });
  }
  const existing = map.get(id);
  if (feature.optional) existing.optional = true;
  if (feature.implemented) existing.implemented = true;
  if (feature.resource) {
    existing.resources.add(feature.resource);
    if (feature.loaded) existing.loadedResources.add(feature.resource);
  }
  existing.declarations.push(feature.declaration);
  return existing;
}

function makeRequestRecord(kind, identity, fields, base) {
  const timing = normalizeTiming(fields.timing ?? fields.phase ?? fields.when ?? fields.requestAt, {
    optional: fields.optional === true,
  });
  return {
    kind,
    identity,
    timing,
    feature: fields.feature ?? fields.featureId ?? fields.owner ?? base.feature ?? null,
    optional: fields.optional === true || timing === TIMING_OPTIONAL || base.featureOptional === true,
    resource: base.resource ?? null,
    source: base.source ?? 'resource',
    loaded: Boolean(base.loaded),
    sources: [
      {
        resource: base.resource ?? null,
        source: base.source ?? 'resource',
        loaded: Boolean(base.loaded),
      },
    ],
    raw: fields.rawItem ?? fields,
  };
}

function collectPermissionItem(item, base, requests, extractionErrors) {
  if (typeof item === 'string' || typeof item === 'number') {
    const id = String(item);
    requests.push(makeRequestRecord('permission', id, { id }, base));
    return;
  }
  if (!isPermissionObject(item)) return;
  const id = item.id ?? item.permission ?? item.name ?? item.key;
  if (!id) {
    extractionErrors.push({ code: 'INVALID_PERMISSION_DECLARATION', message: 'Permission declaration has no id/permission/name', item });
    return;
  }
  const record = makeRequestRecord('permission', String(id), item, base);
  if (record.timing.startsWith('unknown:')) {
    extractionErrors.push({ code: 'INVALID_REQUEST_TIMING', permission: record.identity, message: `Unknown authorization timing ${record.timing}` });
  }
  requests.push(record);
}

function collectSiteItem(item, base, requests, extractionErrors) {
  const rawPattern = typeof item === 'string' ? item : (item.pattern ?? item.scope ?? item.url ?? item.matchPattern ?? item.host ?? item.site);
  try {
    const scope = normalizeSiteScope(rawPattern);
    const fields = typeof item === 'string' ? { pattern: rawPattern } : item;
    const record = makeRequestRecord('site', scope.canonical, { ...fields, canonical: scope.canonical }, base);
    record.pattern = scope.canonical;
    record.scopeParts = scope;
    if (record.timing.startsWith('unknown:')) {
      extractionErrors.push({ code: 'INVALID_REQUEST_TIMING', site: record.identity, message: `Unknown authorization timing ${record.timing}` });
    }
    requests.push(record);
  } catch (error) {
    extractionErrors.push({
      code: 'INVALID_SITE_SCOPE',
      severity: SEVERITY_ERROR,
      site: typeof rawPattern === 'string' ? rawPattern : String(rawPattern),
      resource: base.resource ?? undefined,
      message: error instanceof Error ? error.message : String(error),
    });
  }
}

function collectPermissionBucket(value, base, requests, errors, forcedTiming) {
  if (value === undefined || value === null) return;

  // Grouped form: permissions: { install: [...], enable: [...], optional: [...] }.
  if (!Array.isArray(value) && !isPermissionObject(value) && typeof value === 'object') {
    for (const [bucket, items] of Object.entries(value)) {
      if (['install', 'installation', 'enable', 'enabled', 'runtime', 'optional'].includes(bucket.toLowerCase())) {
        const timing = normalizeTiming(bucket);
        for (const item of asArray(items)) {
          collectPermissionItem(item, { ...base, forcedTiming: timing }, requests, errors);
          const last = requests[requests.length - 1];
          const hasExplicitTiming = item && typeof item === 'object' &&
            (item.timing ?? item.phase ?? item.when ?? item.requestAt) !== undefined;
          if (last && (typeof item === 'string' || !hasExplicitTiming)) {
            last.timing = timing;
            if (timing === TIMING_OPTIONAL) last.optional = true;
          }
        }
      }
    }
    return;
  }

  for (const item of asArray(value)) {
    collectPermissionItem(item, base, requests, errors);
    const last = requests[requests.length - 1];
    if (last && forcedTiming && (typeof item === 'string' || (!item.timing && !item.phase && !item.when))) {
      last.timing = forcedTiming;
      if (forcedTiming === TIMING_OPTIONAL) last.optional = true;
    }
  }
}

function collectSiteBucket(value, base, requests, errors, forcedTiming) {
  if (value === undefined || value === null) return;
  if (!Array.isArray(value) && !isSiteObject(value) && typeof value === 'object') {
    for (const [bucket, items] of Object.entries(value)) {
      if (['install', 'installation', 'enable', 'enabled', 'runtime', 'optional'].includes(bucket.toLowerCase())) {
        const timing = normalizeTiming(bucket);
        for (const item of asArray(items)) {
          collectSiteItem(item, { ...base, forcedTiming: timing }, requests, errors);
          const last = requests[requests.length - 1];
          const hasExplicitTiming = item && typeof item === 'object' &&
            (item.timing ?? item.phase ?? item.when ?? item.requestAt) !== undefined;
          if (last && (typeof item === 'string' || !hasExplicitTiming)) {
            last.timing = timing;
            if (timing === TIMING_OPTIONAL) last.optional = true;
          }
        }
      }
    }
    return;
  }

  for (const item of asArray(value)) {
    collectSiteItem(item, base, requests, errors);
    const last = requests[requests.length - 1];
    if (last && forcedTiming && (typeof item === 'string' || (!item.timing && !item.phase && !item.when))) {
      last.timing = forcedTiming;
      if (forcedTiming === TIMING_OPTIONAL) last.optional = true;
    }
  }
}

function collectUnifiedRequests(value, base, requests, errors) {
  for (const item of asArray(value)) {
    if (typeof item === 'string') {
      // Strings in a unified list are treated as permissions unless prefixed.
      if (item.startsWith('site:') || item.startsWith('host:')) {
        collectSiteItem(item.slice(item.indexOf(':') + 1), base, requests, errors);
      } else {
        collectPermissionItem(item, base, requests, errors);
      }
      continue;
    }
    if (!item || typeof item !== 'object') continue;
    const kind = item.kind ?? item.type ?? (item.pattern || item.site || item.host || item.scope ? 'site' : 'permission');
    const itemBase = { ...base, feature: item.feature ?? item.featureId ?? base.feature };
    if (kind === 'site' || kind === 'host') collectSiteItem(item, itemBase, requests, errors);
    else collectPermissionItem(item, itemBase, requests, errors);
  }
}

function collectFromDeclaration(declaration, base, featuresMap, rawRequests, extractionErrors, forceOptionalFeatures = false) {
  if (!declaration || typeof declaration !== 'object') return;

  const featureValues = [
    ...asArray(declaration.features),
    ...asArray(declaration.featureIds),
    ...asArray(declaration.providedFeatures),
    ...asArray(declaration.provides),
    ...asArray(declaration.feature),
  ];
  if (forceOptionalFeatures) {
    featureValues.push(...asArray(declaration.optionalFeatures), ...asArray(declaration.optional_features));
  }

  for (const featureValue of featureValues) {
    const feature = typeof featureValue === 'string' ? { id: featureValue } : featureValue;
    if (!feature?.id) {
      extractionErrors.push({ code: 'INVALID_FEATURE_DECLARATION', message: 'Feature declaration has no id', feature: featureValue });
      continue;
    }
    const optional = forceOptionalFeatures || feature.optional === true || feature.isOptional === true;
    const featureBase = {
      ...base,
      feature: String(feature.id),
      featureOptional: optional,
    };
    mergeFeature(featuresMap, {
      id: String(feature.id),
      optional,
      implemented: base.loaded !== false && feature.implemented !== false && feature.declarationOnly !== true,
      resource: base.resource,
      loaded: base.loaded !== false,
      declaration: feature,
    });

    collectPermissionBucket(feature.permissions ?? feature.permission, featureBase, rawRequests, extractionErrors);
    collectPermissionBucket(feature.installPermissions ?? feature.install_permissions, { ...featureBase, forcedTiming: TIMING_INSTALL }, rawRequests, extractionErrors, TIMING_INSTALL);
    collectPermissionBucket(feature.enablePermissions ?? feature.runtimePermissions, { ...featureBase, forcedTiming: TIMING_ENABLE }, rawRequests, extractionErrors, TIMING_ENABLE);
    collectPermissionBucket(feature.optionalPermissions ?? feature.optional_permissions, { ...featureBase, forcedTiming: TIMING_OPTIONAL }, rawRequests, extractionErrors, TIMING_OPTIONAL);
    collectSiteBucket(feature.sites ?? feature.siteScopes ?? feature.hosts, featureBase, rawRequests, extractionErrors);
    collectSiteBucket(feature.optionalSites ?? feature.optionalHosts, { ...featureBase, forcedTiming: TIMING_OPTIONAL }, rawRequests, extractionErrors, TIMING_OPTIONAL);
    collectUnifiedRequests(feature.requests, featureBase, rawRequests, extractionErrors);
  }

  // Resource-level default feature when there is exactly one obvious owner.
  const defaultFeature = declaration.featureId ?? declaration.owner ??
    (typeof declaration.feature === 'string' ? declaration.feature : undefined) ??
    (featureValues.length === 1 && typeof featureValues[0] === 'string' ? featureValues[0] : undefined);
  const resourceBase = defaultFeature ? { ...base, feature: defaultFeature } : base;

  collectUnifiedRequests(declaration.requests, resourceBase, rawRequests, extractionErrors);
  collectPermissionBucket(declaration.permissions ?? declaration.permissionScopes ?? declaration.requestedPermissions, resourceBase, rawRequests, extractionErrors);
  collectPermissionBucket(declaration.installPermissions ?? declaration.install_permissions ?? declaration.requiredPermissions, { ...resourceBase, forcedTiming: TIMING_INSTALL }, rawRequests, extractionErrors, TIMING_INSTALL);
  collectPermissionBucket(declaration.enablePermissions ?? declaration.enabledPermissions ?? declaration.postEnablePermissions ?? declaration.runtimePermissions, { ...resourceBase, forcedTiming: TIMING_ENABLE }, rawRequests, extractionErrors, TIMING_ENABLE);
  collectPermissionBucket(declaration.optionalPermissions ?? declaration.optional_permissions, { ...resourceBase, forcedTiming: TIMING_OPTIONAL }, rawRequests, extractionErrors, TIMING_OPTIONAL);

  collectSiteBucket(declaration.sites ?? declaration.siteScopes ?? declaration.hosts, resourceBase, rawRequests, extractionErrors);
  collectSiteBucket(declaration.hostPermissions ?? declaration.host_permissions ?? declaration.matches, { ...resourceBase, forcedTiming: TIMING_INSTALL }, rawRequests, extractionErrors, TIMING_INSTALL);
  collectSiteBucket(declaration.installSites ?? declaration.installHosts, { ...resourceBase, forcedTiming: TIMING_INSTALL }, rawRequests, extractionErrors, TIMING_INSTALL);
  collectSiteBucket(declaration.enableSites ?? declaration.runtimeSites, { ...resourceBase, forcedTiming: TIMING_ENABLE }, rawRequests, extractionErrors, TIMING_ENABLE);
  collectSiteBucket(declaration.optionalSites ?? declaration.optionalHosts ?? declaration.optional_host_permissions, { ...resourceBase, forcedTiming: TIMING_OPTIONAL }, rawRequests, extractionErrors, TIMING_OPTIONAL);
}

function aggregateRequests(rawRequests) {
  const map = new Map();
  for (const request of rawRequests) {
    const key = [request.kind, request.timing, request.feature ?? '', request.identity].join('\u0000');
    if (!map.has(key)) {
      map.set(key, { ...request, key, sources: [...request.sources] });
    } else {
      const existing = map.get(key);
      existing.sources.push(...request.sources);
      existing.loaded = existing.loaded || request.loaded;
      existing.optional = existing.optional || request.optional;
      if (!existing.resource && request.resource) existing.resource = request.resource;
    }
  }

  return [...map.values()].map((request) => {
    const resources = [...new Set(request.sources.map((source) => source.resource).filter(Boolean))];
    const activeSources = request.sources.filter((source) => source.loaded);
    return {
      ...request,
      resources,
      active: activeSources.length > 0,
      activeSources,
      sources: request.sources,
    };
  });
}

function normalizePermissionGrant(value, defaults = {}) {
  if (typeof value === 'string' || typeof value === 'number') {
    return { identity: String(value), timing: defaults.timing ?? 'any', feature: defaults.feature ?? null };
  }
  if (!value || typeof value !== 'object') return null;
  const identity = value.id ?? value.permission ?? value.name ?? value.key;
  if (!identity) return null;
  return {
    identity: String(identity),
    timing: value.timing ? normalizeTiming(value.timing, { optional: value.optional === true }) : (defaults.timing ?? 'any'),
    feature: value.feature ?? value.featureId ?? defaults.feature ?? null,
  };
}

function normalizeSiteGrant(value, defaults = {}, approvalErrors = []) {
  const raw = typeof value === 'string' ? value : (value.pattern ?? value.scope ?? value.url ?? value.matchPattern ?? value.host ?? value.site);
  try {
    const scope = normalizeSiteScope(raw);
    return {
      identity: scope.canonical,
      scope,
      timing: typeof value === 'object' && value.timing ? normalizeTiming(value.timing) : (defaults.timing ?? 'any'),
      feature: (typeof value === 'object' && (value.feature ?? value.featureId)) ?? defaults.feature ?? null,
    };
  } catch (error) {
    approvalErrors.push({
      code: 'INVALID_APPROVED_SITE_SCOPE',
      severity: SEVERITY_ERROR,
      site: String(raw),
      message: error instanceof Error ? error.message : String(error),
    });
    return null;
  }
}

function normalizeApproval(approvalInput) {
  const approvalErrors = [];
  const input = Array.isArray(approvalInput) ? { features: approvalInput } : (approvalInput ?? {});
  const approvedFeatureIds = new Set();
  const featureApprovals = new Map();
  const permissionGrants = [];
  const siteGrants = [];

  function addFeatureApproval(id, featureInput = {}) {
    if (!id) return;
    if (featureInput === false || featureInput.approved === false || featureInput.allowed === false) return;
    const featureId = String(id);
    approvedFeatureIds.add(featureId);
    const feature = typeof featureInput === 'object' && featureInput !== null ? featureInput : { id: featureId };
    const record = featureApprovals.get(featureId) ?? {
      id: featureId,
      optional: feature.optional === true,
      permissionScopes: new Set(),
      siteScopes: new Set(),
      hasPermissionScope: false,
      hasSiteScope: false,
      permissionScopeSpecified: false,
      siteScopeSpecified: false,
      raw: feature,
    };
    if (feature.optional) record.optional = true;

    const permValues = [
      ...asArray(feature.permissions),
      ...asArray(feature.approvedPermissions),
      ...asArray(feature.installPermissions).map((x) => ({ ...(typeof x === 'string' ? { id: x } : x), timing: TIMING_INSTALL })),
      ...asArray(feature.enablePermissions).map((x) => ({ ...(typeof x === 'string' ? { id: x } : x), timing: TIMING_ENABLE })),
      ...asArray(feature.optionalPermissions).map((x) => ({ ...(typeof x === 'string' ? { id: x } : x), timing: TIMING_OPTIONAL })),
    ];
    if (permValues.length) record.hasPermissionScope = true;
    record.permissionScopeSpecified ||= [
      'permissions', 'permission', 'approvedPermissions', 'allowedPermissions',
      'installPermissions', 'enablePermissions', 'optionalPermissions',
    ].some((key) => Object.prototype.hasOwnProperty.call(feature, key));
    for (const value of permValues) {
      const grant = normalizePermissionGrant(value, { feature: featureId, timing: 'any' });
      if (grant) {
        grant.feature = featureId;
        permissionGrants.push(grant);
        record.permissionScopes.add(grant.identity);
      }
    }

    const siteValues = [
      ...asArray(feature.sites),
      ...asArray(feature.siteScopes),
      ...asArray(feature.hosts),
      ...asArray(feature.installSites).map((x) => ({ ...(typeof x === 'string' ? { pattern: x } : x), timing: TIMING_INSTALL })),
      ...asArray(feature.enableSites).map((x) => ({ ...(typeof x === 'string' ? { pattern: x } : x), timing: TIMING_ENABLE })),
      ...asArray(feature.optionalSites).map((x) => ({ ...(typeof x === 'string' ? { pattern: x } : x), timing: TIMING_OPTIONAL })),
    ];
    if (siteValues.length) record.hasSiteScope = true;
    record.siteScopeSpecified ||= [
      'sites', 'siteScopes', 'hosts', 'approvedSites',
      'installSites', 'enableSites', 'optionalSites',
    ].some((key) => Object.prototype.hasOwnProperty.call(feature, key));
    for (const value of siteValues) {
      const grant = normalizeSiteGrant(value, { feature: featureId, timing: 'any' }, approvalErrors);
      if (grant) {
        grant.feature = featureId;
        siteGrants.push(grant);
        record.siteScopes.add(grant.identity);
      }
    }
    featureApprovals.set(featureId, record);
  }

  const featureInput = input.features ?? input.approvedFeatures ?? input.allowedFeatures ?? input.featureApprovals;
  if (Array.isArray(featureInput)) {
    for (const value of featureInput) {
      if (typeof value === 'string') addFeatureApproval(value, {});
      else addFeatureApproval(value.id ?? value.featureId ?? value.name, value);
    }
  } else if (featureInput instanceof Map || (featureInput && typeof featureInput === 'object')) {
    const entries = featureInput instanceof Map ? [...featureInput.entries()] : Object.entries(featureInput);
    for (const [id, value] of entries) {
      if (value === true) addFeatureApproval(id, {});
      else if (Array.isArray(value)) addFeatureApproval(id, { permissions: value });
      else addFeatureApproval(id, value ?? {});
    }
  }

  function addPermissionValues(values, timing) {
    for (const value of asArray(values)) {
      if (!Array.isArray(value) && typeof value === 'object' && !isPermissionObject(value) && value && ('install' in value || 'enable' in value || 'optional' in value)) {
        addPermissionValues(value.install, TIMING_INSTALL);
        addPermissionValues(value.enable, TIMING_ENABLE);
        addPermissionValues(value.optional, TIMING_OPTIONAL);
        continue;
      }
      const grant = normalizePermissionGrant(value, { timing });
      if (grant) permissionGrants.push(grant);
    }
  }

  function addSiteValues(values, timing) {
    for (const value of asArray(values)) {
      if (!Array.isArray(value) && typeof value === 'object' && !isSiteObject(value) && value && ('install' in value || 'enable' in value || 'optional' in value)) {
        addSiteValues(value.install, TIMING_INSTALL);
        addSiteValues(value.enable, TIMING_ENABLE);
        addSiteValues(value.optional, TIMING_OPTIONAL);
        continue;
      }
      const grant = normalizeSiteGrant(value, { timing }, approvalErrors);
      if (grant) siteGrants.push(grant);
    }
  }

  addPermissionValues(input.permissions ?? input.approvedPermissions ?? input.allowedPermissions ?? input.permissionGrants, 'any');
  addPermissionValues(input.installPermissions ?? input.approvedInstallPermissions ?? input.install, TIMING_INSTALL);
  addPermissionValues(input.enablePermissions ?? input.approvedEnablePermissions ?? input.enable, TIMING_ENABLE);
  addPermissionValues(input.optionalPermissions ?? input.approvedOptionalPermissions ?? input.optional, TIMING_OPTIONAL);
  addPermissionValues(input.scopes?.permissions, 'any');

  addSiteValues(input.sites ?? input.approvedSites ?? input.allowedSites ?? input.hosts ?? input.approvedHosts ?? input.hostPermissions, 'any');
  addSiteValues(input.installSites ?? input.approvedInstallSites, TIMING_INSTALL);
  addSiteValues(input.enableSites ?? input.approvedEnableSites, TIMING_ENABLE);
  addSiteValues(input.optionalSites ?? input.approvedOptionalSites, TIMING_OPTIONAL);
  addSiteValues(input.scopes?.sites, 'any');

  return { approvedFeatureIds, featureApprovals, permissionGrants, siteGrants, approvalErrors };
}

function permissionGrantIdentityMatches(grant, request) {
  if (grant.identity !== request.identity) return false;
  if (grant.timing !== 'any' && grant.timing !== request.timing) return false;
  return true;
}

function permissionGrantMatches(grant, request, resolvedFeature = request.feature) {
  if (!permissionGrantIdentityMatches(grant, request)) return false;
  if (grant.feature !== null && grant.feature !== resolvedFeature) return false;
  return true;
}

function siteGrantIdentityMatches(grant, request) {
  if (grant.timing !== 'any' && grant.timing !== request.timing) return false;
  try {
    return request.scopeParts ? siteCoversGrant(grant.scope, request.scopeParts) : false;
  } catch {
    return false;
  }
}

function siteGrantMatches(grant, request, resolvedFeature = request.feature) {
  if (!siteGrantIdentityMatches(grant, request)) return false;
  if (grant.feature !== null && grant.feature !== resolvedFeature) return false;
  return true;
}

function siteCoversGrant(approved, requested) {
  // Reuse the already parsed containment rules by canonical values.
  const { scheme, host, path } = approved;
  if (scheme !== requested.scheme && !(scheme === '*' && ['http', 'https', 'ws', 'wss'].includes(requested.scheme))) return false;
  if (host !== '*') {
    if (host.startsWith('*.')) {
      const suffix = host.slice(2);
      if (requested.hostname !== suffix && !requested.hostname.endsWith(`.${suffix}`)) return false;
    } else if (host !== requested.host || (approved.port === null ? requested.port !== null : approved.port !== requested.port)) {
      return false;
    }
  }
  if (path === requested.path || path === '/*' || path === '*') return true;
  if (!requested.path.includes('*')) {
    // Avoid importing glob regex again by dynamic local implementation.
    const re = new RegExp(`^${[...path].map((c) => (c === '*' ? '.*' : c.replace(/[.?+^${}()|[\]\\]/g, '\\$&'))).join('')}$`, 's');
    return re.test(requested.path);
  }
  if (path.endsWith('*') && requested.path.endsWith('*')) {
    const ap = path.slice(0, -1);
    const rp = requested.path.slice(0, -1);
    if (!ap.includes('*') && rp.startsWith(ap)) return true;
  }
  return false;
}

/**
 * Review one browser variant.
 *
 * @param {object} variant
 * @param {object|string[]} [approvalInput]
 * @param {object} [options]
 */
export function reviewVariant(variantInput = {}, approvalInput, options = {}) {
  const invalidVariant = !variantInput || typeof variantInput !== 'object' || Array.isArray(variantInput);
  const variant = invalidVariant ? { id: typeof variantInput === 'string' ? variantInput : 'invalid-variant' } : variantInput;
  const variantId = String(variant.id ?? variant.name ?? variant.browser ?? 'default');
  const browser = variant.browser ?? variant.id ?? variant.name ?? null;
  const expansion = expandResources(variant, options.sharedResources);
  const issues = [];
  const extractionErrors = [];

  if (invalidVariant) {
    addIssue(issues, {
      code: 'INVALID_VARIANT',
      variantId,
      severity: SEVERITY_ERROR,
      message: 'Browser variant must be an object',
    });
  }

  for (const error of expansion.conditionErrors) addIssue(issues, { ...error, variantId });

  const featuresMap = new Map();
  const rawRequests = [];

  for (const resource of expansion.all) {
    const declaration = resource.raw.declares ?? resource.raw.declaration ?? resource.raw.declarations ?? resource.raw;
    collectFromDeclaration(
      declaration,
      { resource: resource.name, loaded: resource.matched, source: 'resource' },
      featuresMap,
      rawRequests,
      extractionErrors,
      false,
    );
    if (Array.isArray(resource.raw.optionalFeatures) || resource.raw.optional_features) {
      collectFromDeclaration(
        { optionalFeatures: resource.raw.optionalFeatures ?? resource.raw.optional_features },
        { resource: resource.name, loaded: resource.matched, source: 'resource' },
        featuresMap,
        rawRequests,
        extractionErrors,
        true,
      );
    }
  }

  // Variant-level manifest/request declarations are actually presented to this
  // variant and therefore count as active even without a backing resource.
  const variantBase = { resource: null, loaded: true, source: 'variant' };
  collectFromDeclaration(variant, variantBase, featuresMap, rawRequests, extractionErrors);
  if (variant.manifest && typeof variant.manifest === 'object') {
    collectFromDeclaration(variant.manifest, { ...variantBase, source: 'manifest' }, featuresMap, rawRequests, extractionErrors);
  }

  // Optional-feature catalog entries may describe requests whose implementation
  // is absent for this variant.
  for (const catalogFeature of asArray(variant.optionalFeatures ?? variant.featureCatalog ?? variant.declaredOptionalFeatures)) {
    const feature = typeof catalogFeature === 'string' ? { id: catalogFeature } : catalogFeature;
    collectFromDeclaration(
      { features: [{ ...feature, optional: true }] },
      { resource: feature.resource ?? feature.implementation ?? null, loaded: false, source: 'optional-feature-catalog' },
      featuresMap,
      rawRequests,
      extractionErrors,
      true,
    );
  }

  for (const error of extractionErrors) {
    addIssue(issues, { ...error, variantId });
  }

  const approval = normalizeApproval(approvalInput ?? variant.approval ?? options.approval);
  for (const error of approval.approvalErrors) addIssue(issues, { ...error, variantId });

  // Mark IDs listed as optional by variant/approval even if declaration omitted it.
  for (const id of asArray(variant.optionalFeatureIds ?? variant.optional_features)) {
    if (!featuresMap.has(String(id))) {
      mergeFeature(featuresMap, { id: String(id), optional: true, implemented: false, loaded: false, declaration: { id } });
    } else {
      featuresMap.get(String(id)).optional = true;
    }
  }
  for (const id of approval.featureApprovals.keys()) {
    if (approval.featureApprovals.get(id).optional && featuresMap.has(id)) featuresMap.get(id).optional = true;
  }

  const requests = aggregateRequests(rawRequests);
  const features = [...featuresMap.values()].map((feature) => ({
    id: feature.id,
    optional: feature.optional,
    implemented: feature.implemented,
    resources: [...feature.resources],
    loadedResources: [...feature.loadedResources],
  }));

  const implementedFeatureIds = new Set(features.filter((f) => f.implemented).map((f) => f.id));
  const optionalFeatures = features.filter((f) => f.optional);
  const unimplementedOptionalFeatures = optionalFeatures.filter((f) => !f.implemented);
  const residualFeatureIds = new Set(unimplementedOptionalFeatures.map((f) => f.id));

  const unapprovedFeatures = features.filter(
    (feature) => feature.implemented && !approval.approvedFeatureIds.has(feature.id),
  );
  for (const feature of unapprovedFeatures) {
    addIssue(issues, {
      code: 'UNAPPROVED_FEATURE',
      variantId,
      severity: SEVERITY_ERROR,
      feature: feature.id,
      resources: feature.resources,
      message: `Loaded feature "${feature.id}" is not in the approved feature set`,
    });
  }

  const reviewedRequests = requests.map((request) => {
    const identityGrants = request.kind === 'permission'
      ? approval.permissionGrants.filter((grant) => permissionGrantIdentityMatches(grant, request))
      : approval.siteGrants.filter((grant) => siteGrantIdentityMatches(grant, request));

    const declaredFeature = request.feature ?? null;
    let resolvedFeature = declaredFeature;
    let attributionSource = declaredFeature ? 'declaration' : null;
    let featureCandidates = [];

    if (!declaredFeature) {
      featureCandidates = [...new Set(
        identityGrants.map((grant) => grant.feature).filter((feature) => feature !== null),
      )];
      if (featureCandidates.length === 1) {
        [resolvedFeature] = featureCandidates;
        attributionSource = 'approval-scope';
      } else if (featureCandidates.length > 1) {
        attributionSource = 'ambiguous-approval-scope';
      }
    }

    const matchingGrants = identityGrants.filter((grant) => (
      request.kind === 'permission'
        ? permissionGrantMatches(grant, request, resolvedFeature)
        : siteGrantMatches(grant, request, resolvedFeature)
    ));

    const hasFeature = Boolean(resolvedFeature);
    const featureApproved = hasFeature && approval.approvedFeatureIds.has(resolvedFeature);
    const featureApproval = hasFeature ? approval.featureApprovals.get(resolvedFeature) : undefined;
    const featureIsOptional = hasFeature && (featuresMap.get(resolvedFeature)?.optional || request.optional);

    let attributionStatus = 'not-required';
    if (request.timing === TIMING_OPTIONAL || request.optional) {
      if (declaredFeature && !featureApproved) attributionStatus = 'feature-not-approved';
      else if (declaredFeature && featureApproved) attributionStatus = 'approved-feature';
      else if (!declaredFeature && featureCandidates.length === 1 && featureApproved) attributionStatus = 'approved-feature';
      else if (featureCandidates.length > 1) attributionStatus = 'ambiguous-feature';
      else attributionStatus = 'missing-feature';
    } else if (hasFeature) {
      attributionStatus = featureApproved ? 'approved-feature' : 'feature-not-approved';
    }

    // An approved feature entry without an explicit scope list is an approval
    // of that feature's declared request bundle. Once an explicit list exists,
    // matchingGrants must contain the requested permission/site.
    let scopeApproved = matchingGrants.length > 0;
    if (!scopeApproved && featureApproved && options.featureAttributionApprovesScopes !== false && featureApproval) {
      const scopeSpecified = request.kind === 'permission'
        ? featureApproval.permissionScopeSpecified
        : featureApproval.siteScopeSpecified;
      if (!scopeSpecified) scopeApproved = true;
    }

    const residual = hasFeature && residualFeatureIds.has(resolvedFeature);
    const approved = request.active && scopeApproved &&
      (attributionStatus === 'approved-feature' || attributionStatus === 'not-required');

    return {
      kind: request.kind,
      identity: request.identity,
      ...(request.kind === 'site' ? { pattern: request.identity } : { permission: request.identity }),
      timing: request.timing,
      feature: resolvedFeature,
      declaredFeature,
      resolvedFeature,
      attributionSource,
      featureCandidates,
      optional: request.optional,
      active: request.active,
      resources: request.resources,
      sources: request.sources,
      scopeApproved,
      attributionStatus,
      featureApproved,
      featureIsOptional,
      residual,
      approved,
      approvedBy: matchingGrants.map((grant) => ({
        timing: grant.timing,
        feature: grant.feature,
      })),
    };
  });

  const activeRequests = reviewedRequests.filter((r) => r.active);
  const inactiveRequests = reviewedRequests.filter((r) => !r.active);

  for (const request of activeRequests) {
    if (!request.scopeApproved) {
      let code;
      let message;
      if (request.kind === 'permission') {
        if (request.timing === TIMING_INSTALL) code = 'EXTRA_INSTALL_PERMISSION';
        else if (request.timing === TIMING_ENABLE) code = 'UNAPPROVED_ENABLE_PERMISSION';
        else code = 'UNAPPROVED_OPTIONAL_PERMISSION';
        message = `${request.timing} permission "${request.permission}" is outside the approved scope`;
      } else {
        code = 'UNAPPROVED_SITE_SCOPE';
        message = `${request.timing} site scope "${request.pattern}" is outside the approved scope`;
      }
      addIssue(issues, {
        code,
        variantId,
        severity: SEVERITY_ERROR,
        timing: request.timing,
        ...(request.kind === 'permission' ? { permission: request.permission } : { site: request.pattern }),
        feature: request.feature ?? undefined,
        resources: request.resources,
        message,
      });
    }

    if (request.attributionStatus === 'ambiguous-feature') {
      addIssue(issues, {
        code: 'OPTIONAL_FEATURE_ATTRIBUTION_AMBIGUOUS',
        variantId,
        severity: SEVERITY_ERROR,
        timing: request.timing,
        candidates: request.featureCandidates,
        ...(request.kind === 'permission' ? { permission: request.permission } : { site: request.pattern }),
        message: `Optional authorization request could belong to multiple approved features: ${request.featureCandidates.join(', ')}`,
      });
    } else if (request.attributionStatus === 'missing-feature') {
      addIssue(issues, {
        code: 'OPTIONAL_REQUEST_WITHOUT_FEATURE',
        variantId,
        severity: SEVERITY_ERROR,
        timing: request.timing,
        ...(request.kind === 'permission' ? { permission: request.permission } : { site: request.pattern }),
        message: 'Optional authorization request is not attributed to an approved feature',
      });
    } else if (request.attributionStatus === 'feature-not-approved' && (request.timing === TIMING_OPTIONAL || request.optional)) {
      addIssue(issues, {
        code: 'OPTIONAL_FEATURE_NOT_APPROVED',
        variantId,
        severity: SEVERITY_ERROR,
        feature: request.feature,
        timing: request.timing,
        ...(request.kind === 'permission' ? { permission: request.permission } : { site: request.pattern }),
        message: `Optional authorization request belongs to unapproved feature "${request.feature}"`,
      });
    } else if (request.attributionStatus === 'feature-not-approved' && request.timing === TIMING_ENABLE) {
      addIssue(issues, {
        code: 'ENABLE_FEATURE_NOT_APPROVED',
        variantId,
        severity: SEVERITY_ERROR,
        feature: request.feature,
        timing: request.timing,
        ...(request.kind === 'permission' ? { permission: request.permission } : { site: request.pattern }),
        message: `Post-enable authorization request belongs to unapproved feature "${request.feature}"`,
      });
    } else if (request.timing === TIMING_ENABLE && !request.feature) {
      addIssue(issues, {
        code: 'ENABLE_REQUEST_WITHOUT_FEATURE',
        variantId,
        severity: SEVERITY_WARNING,
        ...(request.kind === 'permission' ? { permission: request.permission } : { site: request.pattern }),
        message: 'Post-enable authorization request has no feature attribution',
      });
    }
  }

  const residualRequests = reviewedRequests.filter((request) => request.residual);
  for (const request of residualRequests) {
    addIssue(issues, {
      code: 'RESIDUAL_OPTIONAL_REQUEST',
      variantId,
      severity: options.strictResidual ? SEVERITY_ERROR : SEVERITY_WARNING,
      feature: request.feature,
      timing: request.timing,
      ...(request.kind === 'permission' ? { permission: request.permission } : { site: request.pattern }),
      active: request.active,
      resources: request.resources,
      message: `Optional feature "${request.feature}" has no loaded implementation but still has a residual ${request.timing} request`,
    });
  }

  const byTiming = (kind, timing) => activeRequests.filter((r) => r.kind === kind && r.timing === timing);
  const permissions = {
    install: byTiming('permission', TIMING_INSTALL),
    enable: byTiming('permission', TIMING_ENABLE),
    optional: byTiming('permission', TIMING_OPTIONAL),
    all: activeRequests.filter((r) => r.kind === 'permission'),
  };
  const sites = {
    install: byTiming('site', TIMING_INSTALL),
    enable: byTiming('site', TIMING_ENABLE),
    optional: byTiming('site', TIMING_OPTIONAL),
    all: activeRequests.filter((r) => r.kind === 'site'),
  };

  const extraInstallPermissions = permissions.install.filter((r) => !r.scopeApproved);
  const extraPermissions = permissions.all.filter((r) => !r.scopeApproved);
  const unapprovedSites = sites.all.filter((r) => !r.scopeApproved);
  const optionalRequestReviews = activeRequests.filter((r) => r.timing === TIMING_OPTIONAL || r.optional);
  const unattributedOptionalRequests = optionalRequestReviews.filter((r) => r.attributionStatus !== 'approved-feature');

  const errorCount = issues.filter((i) => i.severity === SEVERITY_ERROR).length;
  const warningCount = issues.filter((i) => i.severity === SEVERITY_WARNING).length;
  const violations = issues.filter((i) => i.severity === SEVERITY_ERROR);
  const warningIssues = issues.filter((i) => i.severity === SEVERITY_WARNING);
  const ok = errorCount === 0;

  return {
    variantId,
    browser,
    context: expansion.context,
    ok,
    passed: ok,
    releaseApproved: ok && (!options.strict || warningCount === 0),
    strict: Boolean(options.strict),
    counts: {
      loadedResources: expansion.loaded.length,
      skippedResources: expansion.skipped.length,
      features: features.length,
      implementedFeatures: features.filter((f) => f.implemented).length,
      approvedFeatures: approval.approvedFeatureIds.size,
      activeRequests: activeRequests.length,
      residualOptionalRequests: residualRequests.length,
      issues: issues.length,
      errors: errorCount,
      warnings: warningCount,
    },
    loadedResources: expansion.loaded.map((r) => ({ name: r.name, path: r.path, condition: r.condition })),
    skippedResources: expansion.skipped.map((r) => ({ name: r.name, path: r.path, condition: r.condition, reason: r.reason })),
    features,
    loadedFeatures: features.filter((feature) => feature.implemented),
    featureCombination: features.filter((feature) => feature.implemented),
    approvedFeatures: [...approval.approvedFeatureIds],
    unapprovedFeatures: unapprovedFeatures.map((f) => f.id),
    unimplementedOptionalFeatures: unimplementedOptionalFeatures.map((f) => f.id),
    permissions,
    installPermissions: permissions.install,
    enablePermissions: permissions.enable,
    optionalPermissions: permissions.optional,
    sites,
    installSites: sites.install,
    enableSites: sites.enable,
    optionalSites: sites.optional,
    requests: reviewedRequests,
    inactiveRequests,
    extraInstallPermissions,
    extraPermissions,
    unapprovedSites,
    optionalRequestReviews,
    unattributedOptionalRequests,
    residualOptionalFeatures: unimplementedOptionalFeatures.map((feature) => ({
      id: feature.id,
      resources: feature.resources,
      requests: residualRequests.filter((r) => r.feature === feature.id),
    })),
    residualOptionalRequests: residualRequests,
    residualRequests,
    violations,
    warnings: warningIssues,
    warningIssues,
    issues,
  };
}

function normalizeVariants(input) {
  const variants = input?.variants ?? input?.browserVariants ?? input?.browsers ?? input?.targets;
  if (Array.isArray(variants)) return variants;
  if (variants && typeof variants === 'object') {
    return Object.entries(variants).map(([id, variant]) => ({ id, ...variant }));
  }
  if (Array.isArray(input)) return input;
  if (input && typeof input === 'object' && (
    input.resources || input.approval || input.approvedScope || input.approvalScope ||
    input.features || input.permissions || input.sites || input.manifest
  )) {
    return [{ id: input.id ?? input.browser ?? 'default', ...input }];
  }
  return [];
}

/**
 * Review every browser variant in a package.
 *
 * @param {object} pkg
 * @param {object} [options]
 */
export function reviewPackage(pkg = {}, options = {}) {
  if (!pkg || typeof pkg !== 'object') {
    throw new TypeError('reviewPackage expects a package object');
  }
  const variantsInput = normalizeVariants(pkg);
  const approvals = pkg.approvals ?? pkg.approvalsByVariant ?? pkg.approvedScopes ?? options.approvals ?? {};
  const sharedResources = pkg.resources ?? options.sharedResources;

  const variants = variantsInput.map((inputVariant) => {
    const safeVariant = inputVariant && typeof inputVariant === 'object' && !Array.isArray(inputVariant)
      ? inputVariant
      : { id: typeof inputVariant === 'string' ? inputVariant : 'invalid-variant' };
    const variantId = safeVariant.id ?? safeVariant.name ?? safeVariant.browser ?? 'default';
    const approval = safeVariant.approval ?? safeVariant.approvedScope ?? safeVariant.approvalScope ??
      approvals[variantId] ?? approvals[safeVariant.browser] ?? pkg.approval ?? pkg.approvedScope ?? options.approval;
    return reviewVariant(inputVariant, approval, { ...options, sharedResources });
  });

  const packageIssues = [];
  if (variantsInput.length === 0) {
    packageIssues.push({
      code: 'NO_VARIANTS',
      severity: SEVERITY_ERROR,
      message: 'Package does not declare any browser variant to review',
    });
  }
  const seenVariantIds = new Set();
  for (const variant of variants) {
    if (seenVariantIds.has(variant.variantId)) {
      packageIssues.push({
        code: 'DUPLICATE_VARIANT_ID',
        severity: SEVERITY_ERROR,
        variantId: variant.variantId,
        message: `Duplicate browser variant id "${variant.variantId}"`,
      });
    }
    seenVariantIds.add(variant.variantId);
  }

  const variantIssues = variants.flatMap((variant) => variant.issues);
  const issues = [...packageIssues, ...variantIssues];
  const errorCount = issues.filter((i) => i.severity === SEVERITY_ERROR).length;
  const warningCount = issues.filter((i) => i.severity === SEVERITY_WARNING).length;
  const violations = issues.filter((i) => i.severity === SEVERITY_ERROR);
  const warningIssues = issues.filter((i) => i.severity === SEVERITY_WARNING);

  const forVariant = (variant, item) => ({ variantId: variant.variantId, browser: variant.browser, ...item });
  const extraInstallPermissions = variants.flatMap((variant) =>
    variant.extraInstallPermissions.map((item) => forVariant(variant, item)));
  const unapprovedSites = variants.flatMap((variant) =>
    variant.unapprovedSites.map((item) => forVariant(variant, item)));
  const residualOptionalRequests = variants.flatMap((variant) =>
    variant.residualOptionalRequests.map((item) => forVariant(variant, item)));

  return {
    packageName: pkg.name ?? pkg.packageName ?? null,
    version: pkg.version ?? null,
    ok: errorCount === 0,
    passed: errorCount === 0,
    releaseApproved: errorCount === 0 && (!options.strict || warningCount === 0),
    variants,
    variantIds: variants.map((v) => v.variantId),
    summary: {
      variants: variants.length,
      loadedResources: variants.reduce((n, v) => n + v.counts.loadedResources, 0),
      skippedResources: variants.reduce((n, v) => n + v.counts.skippedResources, 0),
      extraInstallPermissions: variants.reduce((n, v) => n + v.extraInstallPermissions.length, 0),
      unapprovedSites: variants.reduce((n, v) => n + v.unapprovedSites.length, 0),
      unattributedOptionalRequests: variants.reduce((n, v) => n + v.unattributedOptionalRequests.length, 0),
      residualOptionalRequests: variants.reduce((n, v) => n + v.residualOptionalRequests.length, 0),
      errors: errorCount,
      warnings: warningCount,
    },
    packageIssues,
    extraInstallPermissions,
    unapprovedSites,
    residualOptionalRequests,
    residualRequests: residualOptionalRequests,
    violations,
    warnings: warningIssues,
    warningIssues,
    issues,
    getVariant(id) {
      return variants.find((variant) => variant.variantId === id || variant.browser === id);
    },
  };
}

/** Convenience alias for callers that think in terms of a manifest. */
export function reviewManifest(manifest, approval, options = {}) {
  if (manifest?.variants) return reviewPackage(manifest, { ...options, approvals: manifest.approvals ?? options.approvals });
  return reviewVariant(manifest, approval ?? manifest?.approval, options);
}

export const auditPackage = reviewPackage;
export const auditManifest = reviewManifest;
export const review = reviewPackage;
export const checkPackage = reviewPackage;
export const checkManifest = reviewManifest;
