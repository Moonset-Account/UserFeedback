/**
 * Row-local evaluation of the expression tree.
 *
 * Evaluation is continuation-passing so a scalar subquery can suspend the
 * current row without losing the continuation. The engine catches Suspension,
 * resolves the request in a batch, then calls `resume(value)` to continue the
 * very same traversal. This is what makes IF branch pruning exact: only nodes
 * the traversal actually reaches can register a request or raise an error.
 */
import { CODES, EvalError } from './errors.js';
import { subqueryKey } from './key.js';

const hasOwn = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);

/** Thrown when traversal needs a scalar-subquery result to continue. */
export class Suspension {
  /**
   * @param {object} demand
   * @param {(value: unknown) => void} resume
   */
  constructor(demand, resume) {
    this.name = 'Suspension';
    this.demand = demand;
    this.resume = resume;
  }
}

export class RowContext {
  /**
   * @param {object} opts
   * @param {number} opts.rowIndex
   * @param {Record<string, unknown>} opts.params
   * @param {'mark-missing'|'error'} [opts.missingCorrelation]
   */
  constructor({ rowIndex, params, missingCorrelation = 'mark-missing' }) {
    this.rowIndex = rowIndex;
    this.params = params;
    this.missingCorrelation = missingCorrelation;
  }

  lookup(name, path) {
    if (!hasOwn(this.params, name)) {
      throw new EvalError({
        code: CODES.MISSING_PARAM,
        reason: `missing parameter: ${name}`,
        path,
        detail: { name },
      });
    }
    const value = this.params[name];
    if (value === undefined) {
      throw new EvalError({
        code: CODES.TYPE,
        reason: `parameter ${name} is undefined`,
        path,
        detail: { name },
      });
    }
    return value;
  }

  /**
   * Evaluate a value node; `k` receives the final value.
   * May throw EvalError or Suspension.
   */
  evalNode(node, path, k) {
    if (node === undefined || node === null) {
      k(null);
      return;
    }
    if (typeof node !== 'object' || Array.isArray(node) || typeof node.kind !== 'string') {
      throw new EvalError({
        code: CODES.UNKNOWN_NODE,
        reason: 'expression node must be an object with a string kind',
        path,
      });
    }

    switch (node.kind) {
      case 'const':
        k(node.value ?? null);
        return;
      case 'param':
        k(this.lookup(node.name, path));
        return;
      case 'if': {
        const condPath = [...path, 'cond'];
        const cond = this.evalCond(node.cond, condPath);
        if (cond === null) {
          throw new EvalError({
            code: CODES.NULL_CONDITION,
            reason: 'IF condition evaluated to UNKNOWN (NULL)',
            path: condPath,
          });
        }
        const branch = cond ? 'then' : 'else';
        const child = node[branch];
        if (child === undefined || child === null) {
          k(null);
          return;
        }
        this.evalNode(child, [...path, branch], k);
        return;
      }
      case 'scalar': {
        const args = this.resolveArgs(node.args, [...path, 'args']);
        const key = subqueryKey(node.sql, args);
        const demand = {
          rowIndex: this.rowIndex,
          path: [...path],
          nodeId: node.id ?? null,
          sql: node.sql,
          args,
          key,
        };
        throw new Suspension(demand, (value) => k(value));
      }
      case 'error':
        throw new EvalError({
          code: node.code ?? CODES.TYPE,
          reason: node.reason ?? 'explicit error node',
          path,
          detail: node.detail ?? null,
        });
      case 'add':
      case 'sub':
      case 'mul':
      case 'div':
        this.evalBinary(node, path, k);
        return;
      case 'neg':
        this.evalNode(node.arg, [...path, 'arg'], (v) => {
          k(v === null ? null : -this.requireNumber(v, path));
        });
        return;
      case 'coalesce': {
        if (!Array.isArray(node.items)) {
          throw new EvalError({
            code: CODES.UNKNOWN_NODE,
            reason: 'coalesce.items must be an array',
            path,
          });
        }
        const items = node.items;
        const step = (i) => {
          if (i >= items.length) {
            k(null);
            return;
          }
          this.evalNode(items[i], [...path, 'items', String(i)], (v) => {
            if (v === null) step(i + 1);
            else k(v);
          });
        };
        step(0);
        return;
      }
      default:
        throw new EvalError({
          code: CODES.UNKNOWN_NODE,
          reason: `unknown expression node kind: ${node.kind}`,
          path,
        });
    }
  }

  evalBinary(node, path, k) {
    this.evalNode(node.left, [...path, 'left'], (left) => {
      this.evalNode(node.right, [...path, 'right'], (right) => {
        if (left === null || right === null) {
          k(null);
          return;
        }
        const l = this.requireNumber(left, path);
        const r = this.requireNumber(right, path);
        switch (node.kind) {
          case 'add':
            k(l + r);
            return;
          case 'sub':
            k(l - r);
            return;
          case 'mul':
            k(l * r);
            return;
          case 'div':
            if (r === 0) {
              throw new EvalError({
                code: CODES.DIV_BY_ZERO,
                reason: 'division by zero',
                path,
                detail: { left: l, right: r },
              });
            }
            k(l / r);
            return;
          default:
            throw new EvalError({
              code: CODES.UNKNOWN_NODE,
              reason: `unexpected binary kind: ${node.kind}`,
              path,
            });
        }
      });
    });
  }

  requireNumber(value, path) {
    if (typeof value !== 'number' || Number.isNaN(value)) {
      throw new EvalError({
        code: CODES.TYPE,
        reason: `arithmetic requires a number, got ${describe(value)}`,
        path,
      });
    }
    return value;
  }

  /** Conditions are SQL-style three-valued logic. */
  evalCond(node, path) {
    if (typeof node !== 'object' || node === null || Array.isArray(node)) {
      throw new EvalError({
        code: CODES.UNKNOWN_NODE,
        reason: 'condition node must be an object',
        path,
      });
    }
    switch (node.kind) {
      case 'const':
      case 'param': {
        const value = this.evalOperand(node, path);
        if (value === null) return null;
        if (typeof value !== 'boolean') {
          throw new EvalError({
            code: CODES.TYPE,
            reason: `condition requires a boolean, got ${describe(value)}`,
            path,
          });
        }
        return value;
      }
      case 'not': {
        const value = this.evalCond(node.arg, [...path, 'arg']);
        return value === null ? null : !value;
      }
      case 'and':
        return this.logic(node.items, path, true);
      case 'or':
        return this.logic(node.items, path, false);
      case 'eq':
      case 'ne': {
        const left = this.evalOperand(node.left, [...path, 'left']);
        const right = this.evalOperand(node.right, [...path, 'right']);
        if (left === null || right === null) return null;
        const same = Object.is(left, right);
        return node.kind === 'eq' ? same : !same;
      }
      case 'lt':
      case 'lte':
      case 'gt':
      case 'gte': {
        const left = this.evalOperand(node.left, [...path, 'left']);
        const right = this.evalOperand(node.right, [...path, 'right']);
        if (left === null || right === null) return null;
        const comparable =
          (typeof left === 'number' && typeof right === 'number') ||
          (typeof left === 'string' && typeof right === 'string');
        if (!comparable) {
          throw new EvalError({
            code: CODES.TYPE,
            reason: 'comparison requires two numbers or two strings',
            path,
            detail: { left: describe(left), right: describe(right) },
          });
        }
        switch (node.kind) {
          case 'lt':
            return left < right;
          case 'lte':
            return left <= right;
          case 'gt':
            return left > right;
          default:
            return left >= right;
        }
      }
      default:
        throw new EvalError({
          code: CODES.UNKNOWN_NODE,
          reason: `unknown condition kind: ${node.kind}`,
          path,
        });
    }
  }

  logic(items, path, isAnd) {
    if (!Array.isArray(items)) {
      throw new EvalError({
        code: CODES.UNKNOWN_NODE,
        reason: `${isAnd ? 'and' : 'or'}.items must be an array`,
        path,
      });
    }
    let sawUnknown = false;
    for (let i = 0; i < items.length; i += 1) {
      const value = this.evalCond(items[i], [...path, 'items', String(i)]);
      if (value === null) {
        sawUnknown = true;
      } else if (isAnd && value === false) {
        // Decisive result: later operands cannot change it, so they are not
        // evaluated (matching the reference interpreter and SQL short-circuit).
        return false;
      } else if (!isAnd && value === true) {
        return true;
      }
    }
    if (sawUnknown) return null;
    return isAnd;
  }

  evalOperand(node, path) {
    if (typeof node !== 'object' || node === null || Array.isArray(node)) {
      throw new EvalError({
        code: CODES.UNKNOWN_NODE,
        reason: 'condition operand must be an object',
        path,
      });
    }
    if (node.kind === 'const') return node.value ?? null;
    if (node.kind === 'param') return this.lookup(node.name, path);
    if (node.kind === 'error') {
      throw new EvalError({
        code: node.code ?? CODES.TYPE,
        reason: node.reason ?? 'explicit error node',
        path,
      });
    }
    throw new EvalError({
      code: CODES.UNKNOWN_NODE,
      reason: `unsupported condition operand kind: ${node.kind}`,
      path,
    });
  }

  /**
   * Bind a scalar node's argument specifications against this row.
   * Produces either { name, value } (present, possibly NULL) or
   * { name, missing: true } (the correlated field does not exist).
   */
  resolveArgs(specs, path) {
    if (!Array.isArray(specs)) {
      throw new EvalError({
        code: CODES.BAD_KEY,
        reason: 'scalar.args must be an array',
        path,
      });
    }
    const out = [];
    for (let i = 0; i < specs.length; i += 1) {
      const spec = specs[i];
      const argPath = [...path, String(i)];
      if (typeof spec !== 'object' || spec === null || Array.isArray(spec) || typeof spec.name !== 'string') {
        throw new EvalError({
          code: CODES.BAD_KEY,
          reason: 'argument spec must be an object with a string name',
          path: argPath,
        });
      }
      const sources = ['param', 'value', 'missing'].filter((k) => hasOwn(spec, k) && spec[k] !== undefined);
      if (sources.length !== 1) {
        throw new EvalError({
          code: CODES.BAD_KEY,
          reason: `argument ${spec.name} must specify exactly one of param, value, or missing`,
          path: argPath,
          detail: { name: spec.name },
        });
      }
      const source = sources[0];
      if (source === 'missing') {
        if (spec.missing !== true) {
          throw new EvalError({
            code: CODES.BAD_KEY,
            reason: `argument ${spec.name}: missing must be true`,
            path: argPath,
            detail: { name: spec.name },
          });
        }
        out.push({ name: spec.name, missing: true });
        continue;
      }
      if (source === 'value') {
        out.push({ name: spec.name, value: spec.value });
        continue;
      }
      // source === 'param'
      if (typeof spec.param !== 'string' || spec.param.length === 0) {
        throw new EvalError({
          code: CODES.BAD_KEY,
          reason: `argument ${spec.name}: param must be a non-empty string`,
          path: argPath,
          detail: { name: spec.name },
        });
      }
      if (hasOwn(this.params, spec.param)) {
        const value = this.params[spec.param];
        if (value === undefined) {
          throw new EvalError({
            code: CODES.TYPE,
            reason: `parameter ${spec.param} is undefined`,
            path: argPath,
            detail: { name: spec.name, param: spec.param },
          });
        }
        out.push({ name: spec.name, value });
        continue;
      }
      if (this.missingCorrelation === 'error') {
        throw new EvalError({
          code: CODES.MISSING_PARAM,
          reason: `missing correlated parameter: ${spec.param}`,
          path: argPath,
          detail: { name: spec.name, param: spec.param },
        });
      }
      out.push({ name: spec.name, missing: true });
    }
    return out;
  }
}

function describe(value) {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  if (value instanceof Date) return 'Date';
  return typeof value;
}

/**
 * Begin evaluating a row. Returns nothing; the continuation `done` is called
 * with the row value, or a Suspension/EvalError is thrown.
 */
export function startRow({ rowIndex, params, tree, missingCorrelation }, done) {
  const ctx = new RowContext({ rowIndex, params, missingCorrelation });
  ctx.evalNode(tree, [], done);
}
