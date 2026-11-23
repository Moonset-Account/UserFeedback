/**
 * Schema definition and validation.
 *
 * defineSchema({
 *   entities: {
 *     User: {
 *       key: ['id'],                    // identity key fields (default ['id'])
 *       services: {
 *         profiles: {                   // service name
 *           key: ['id'],                // composite extraction key fields
 *           provides: ['name', 'region'],
 *           cost: 4,                    // cost per batch call (default 1)
 *           batchLimit: 2,              // max keys per call (default 100)
 *         },
 *       },
 *       links: {
 *         bestFriend: { target: 'User', many: false },
 *       },
 *     },
 *   },
 *   coReadable: [ { profiles: 2, contacts: 2 } ],   // optional
 * })
 *
 * Link fields must appear in some service's `provides` and be declared in
 * `links`; every provided field must map to exactly one service.
 */
export function defineSchema(def) {
  if (!def || typeof def !== 'object') throw new TypeError('schema definition required');
  const entities = {};
  for (const [typeName, entityDef] of Object.entries(def.entities ?? {})) {
    const key = entityDef.key ?? ['id'];
    const services = {};
    const fieldService = new Map();
    for (const [serviceName, svc] of Object.entries(entityDef.services ?? {})) {
      if (!Array.isArray(svc.key) || svc.key.length === 0) {
        throw new Error(`service ${serviceName} of ${typeName}: key must be a non-empty array`);
      }
      if (!Array.isArray(svc.provides) || svc.provides.length === 0) {
        throw new Error(`service ${serviceName} of ${typeName}: provides must be a non-empty array`);
      }
      const normalized = {
        key: [...svc.key],
        provides: [...svc.provides],
        cost: svc.cost ?? 1,
        batchLimit: svc.batchLimit ?? 100,
      };
      services[serviceName] = normalized;
      for (const field of normalized.provides) {
        // Several services may offer the same field as alternatives; the
        // planner picks the cheapest feasible one. fieldService keeps the
        // first provider (used to attribute link-key revisions).
        if (!fieldService.has(field)) fieldService.set(field, serviceName);
      }
    }
    const links = {};
    for (const [linkField, linkDef] of Object.entries(entityDef.links ?? {})) {
      if (!fieldService.has(linkField)) {
        throw new Error(`link ${typeName}.${linkField} is not provided by any service`);
      }
      if (!def.entities[linkDef.target]) {
        throw new Error(`link ${typeName}.${linkField} targets unknown entity ${linkDef.target}`);
      }
      links[linkField] = { target: linkDef.target, many: linkDef.many === true };
    }
    entities[typeName] = {
      name: typeName,
      key,
      services,
      links,
      fieldService: Object.fromEntries(fieldService),
    };
  }
  return { entities, coReadable: def.coReadable ?? null };
}
