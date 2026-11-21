/**
 * Dependency graph validation and helpers for activation DAGs.
 *
 * A plan is a plain object:
 *
 *   {
 *     steps: [{ id, service, dependsOn?: string[] }],
 *     services: { [name]: { forward(input, ctx), compensate(input, ctx) } }
 *   }
 *
 * Every step has at most one forward effect and therefore at most one
 * compensation.  A prerequisite required by two branches (a diamond)
 * appears exactly once in the graph; its compensation therefore runs once.
 */

export class PlanError extends Error {
  constructor(message, details = []) {
    super(message)
    this.name = 'PlanError'
    this.details = details
  }
}

/**
 * @param {{steps: Array<{id: string, service: string, dependsOn?: string[]}>, services: object}} plan
 * @returns {{ids: string[], order: string[], dependents: Map<string,string[]>, prerequisites: Map<string,string[]>}}
 */
export function validatePlan(plan) {
  if (!plan || typeof plan !== 'object') {
    throw new PlanError('plan must be an object')
  }
  if (!Array.isArray(plan.steps) || plan.steps.length === 0) {
    throw new PlanError('plan.steps must be a non-empty array')
  }
  if (!plan.services || typeof plan.services !== 'object') {
    throw new PlanError('plan.services must be an object')
  }

  const ids = []
  const seen = new Set()
  const prerequisites = new Map()

  for (const [index, step] of plan.steps.entries()) {
    if (!step || typeof step !== 'object') {
      throw new PlanError(`plan.steps[${index}] must be an object`)
    }
    const { id, service } = step
    if (typeof id !== 'string' || id.length === 0) {
      throw new PlanError(`plan.steps[${index}].id must be a non-empty string`)
    }
    if (seen.has(id)) {
      throw new PlanError(`duplicate step id: ${id}`)
    }
    seen.add(id)
    ids.push(id)
    if (typeof service !== 'string' || service.length === 0) {
      throw new PlanError(`step ${id}: service must be a non-empty string`)
    }
    const implementation = plan.services[service]
    if (!implementation || typeof implementation.forward !== 'function') {
      throw new PlanError(`step ${id}: service "${service}" has no forward() implementation`)
    }
    if (typeof implementation.compensate !== 'function') {
      throw new PlanError(`step ${id}: service "${service}" has no compensate() implementation`)
    }
    const deps = step.dependsOn ?? []
    if (!Array.isArray(deps)) {
      throw new PlanError(`step ${id}: dependsOn must be an array`)
    }
    const unique = new Set()
    for (const dep of deps) {
      if (typeof dep !== 'string') {
        throw new PlanError(`step ${id}: dependsOn entries must be strings`)
      }
      if (dep === id) {
        throw new PlanError(`step ${id}: a step cannot depend on itself`)
      }
      unique.add(dep)
    }
    prerequisites.set(id, [...unique])
  }

  for (const id of ids) {
    for (const dep of prerequisites.get(id)) {
      if (!seen.has(dep)) {
        throw new PlanError(`step ${id}: unknown prerequisite "${dep}"`)
      }
    }
  }

  const dependents = new Map(ids.map((id) => [id, []]))
  for (const id of ids) {
    for (const dep of prerequisites.get(id)) {
      dependents.get(dep).push(id)
    }
  }

  const order = topologicalOrder(ids, prerequisites, dependents)
  return { ids, order, dependents, prerequisites }
}

/**
 * Deterministic Kahn topological order (declaration order used as tie-break).
 */
export function topologicalOrder(ids, prerequisites, dependents) {
  const indegree = new Map(ids.map((id) => [id, prerequisites.get(id).length]))
  const queue = ids.filter((id) => indegree.get(id) === 0)
  const order = []

  while (queue.length > 0) {
    const id = queue.shift()
    order.push(id)
    for (const next of dependents.get(id) ?? []) {
      const remaining = indegree.get(next) - 1
      indegree.set(next, remaining)
      if (remaining === 0) queue.push(next)
    }
  }

  if (order.length !== ids.length) {
    const stuck = ids.filter((id) => !order.includes(id))
    throw new PlanError(`plan contains a dependency cycle involving: ${stuck.join(', ')}`, stuck)
  }
  return order
}

/**
 * All transitive prerequisites of `id`.
 * @returns {Set<string>}
 */
export function ancestorsOf(id, prerequisites) {
  const result = new Set()
  const stack = [...(prerequisites.get(id) ?? [])]
  while (stack.length > 0) {
    const next = stack.pop()
    if (result.has(next)) continue
    result.add(next)
    for (const up of prerequisites.get(next) ?? []) stack.push(up)
  }
  return result
}
