/**
 * Independent, deliberately naive reference interpreter.
 *
 * Purpose: an oracle for the batched engine. It does NOT deduplicate and does
 * NOT batch: it walks rows one by one in outer order and, each time it reaches
 * a scalar subquery, issues exactly one runner call for that row. It records
 * every call (row index, tree path, sql, args, returned rows) so a test can
 * compare the batched run's issue count, request contents, and error position
 * against a second, simple implementation.
 *
 * This file intentionally shares no evaluation code with src/interpreter.js.
 */
import { CODES, EvalError } from './errors.js';

const own = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);

function argKey(sql, args) {
  const encoded = args
    .map((arg) =>
      arg.missing === true ? `${arg.name}:missing` : `${arg.name}:${JSON.stringify(arg.value)}`,
    )
    .sort();
  return JSON.stringify([sql, encoded]);
}

export class ReferenceInterpreter {
  /**
   * @param {{resolve: Function}} runner runner used for single-request calls
   */
  constructor(runner) {
    this.runner = runner;
    this.calls = [];
  }

  /**
   * Evaluate the whole batch strictly sequentially.
   * @returns {Promise<{ok: true, values: unknown[]} | {ok: false, error: object}>}
   */
  async run({ tree, rows, missingCorrelation = 'mark-missing' }) {
    const values = [];
    for (let rowIndex = 0; rowIndex < rows.length; rowIndex += 1) {
      try {
        const value = await this.evalValue(tree, rows[rowIndex].params, rowIndex, [], missingCorrelation);
        values.push(value);
      } catch (error) {
        const evalError = this.normalize(error, rowIndex);
        return { ok: false, error: evalError };
      }
    }
    return { ok: true, values };
  }

  normalize(error, rowIndex) {
    if (error instanceof EvalError) {
      return error.toJSON();
    }
    return new EvalError({
      code: CODES.RUNNER,
      reason: error instanceof Error ? error.message : String(error),
      rowIndex,
      path: [],
    }).toJSON();
  }

  lookup(params, name, rowIndex, path) {
    if (!own(params, name)) {
      throw new EvalError({
        code: CODES.MISSING_PARAM,
        reason: `missing parameter: ${name}`,
        rowIndex,
        path,
      });
    }
    const value = params[name];
    if (value === undefined) {
      throw new EvalError({
        code: CODES.TYPE,
        reason: `parameter ${name} is undefined`,
        rowIndex,
        path,
      });
    }
    return value;
  }

  async evalValue(node, params, rowIndex, path, missingCorrelation) {
    if (node === null || node === undefined) return null;
    switch (node.kind) {
      case 'const':
        return node.value ?? null;
      case 'param':
        return this.lookup(params, node.name, rowIndex, path);
      case 'if': {
        const cond = this.evalCond(node.cond, params, rowIndex, [...path, 'cond']);
        if (cond === null) {
          throw new EvalError({
            code: CODES.NULL_CONDITION,
            reason: 'IF condition evaluated to UNKNOWN (NULL)',
            rowIndex,
            path: [...path, 'cond'],
          });
        }
        const branch = cond ? 'then' : 'else';
        return this.evalValue(node[branch], params, rowIndex, [...path, branch], missingCorrelation);
      }
      case 'scalar':
        return this.evalScalar(node, params, rowIndex, path, missingCorrelation);
      case 'error':
        throw new EvalError({
          code: node.code ?? CODES.TYPE,
          reason: node.reason ?? 'explicit error node',
          rowIndex,
          path,
        });
      case 'add':
      case 'sub':
      case 'mul':
      case 'div': {
        const left = await this.evalValue(node.left, params, rowIndex, [...path, 'left'], missingCorrelation);
        const right = await this.evalValue(node.right, params, rowIndex, [...path, 'right'], missingCorrelation);
        return this.applyBinary(node.kind, left, right, rowIndex, path);
      }
      case 'neg': {
        const value = await this.evalValue(node.arg, params, rowIndex, [...path, 'arg'], missingCorrelation);
        if (value === null) return null;
        return -this.number(value, rowIndex, path);
      }
      case 'coalesce': {
        for (let i = 0; i < node.items.length; i += 1) {
          const value = await this.evalValue(
            node.items[i],
            params,
            rowIndex,
            [...path, 'items', String(i)],
            missingCorrelation,
          );
          if (value !== null) return value;
        }
        return null;
      }
      default:
        throw new EvalError({
          code: CODES.UNKNOWN_NODE,
          reason: `unknown node kind: ${node.kind}`,
          rowIndex,
          path,
        });
    }
  }

  applyBinary(kind, left, right, rowIndex, path) {
    if (left === null || right === null) return null;
    const l = this.number(left, rowIndex, path);
    const r = this.number(right, rowIndex, path);
    switch (kind) {
      case 'add':
        return l + r;
      case 'sub':
        return l - r;
      case 'mul':
        return l * r;
      default:
        if (r === 0) {
          throw new EvalError({
            code: CODES.DIV_BY_ZERO,
            reason: 'division by zero',
            rowIndex,
            path,
          });
        }
        return l / r;
    }
  }

  number(value, rowIndex, path) {
    if (typeof value !== 'number' || Number.isNaN(value)) {
      throw new EvalError({
        code: CODES.TYPE,
        reason: `arithmetic requires a number, got ${typeof value}`,
        rowIndex,
        path,
      });
    }
    return value;
  }

  evalCond(node, params, rowIndex, path) {
    switch (node.kind) {
      case 'const':
        return this.asBool(node.value ?? null, rowIndex, path);
      case 'param':
        return this.asBool(this.lookup(params, node.name, rowIndex, path), rowIndex, path);
      case 'not': {
        const value = this.evalCond(node.arg, params, rowIndex, [...path, 'arg']);
        return value === null ? null : !value;
      }
      case 'and':
      case 'or': {
        let unknown = false;
        for (let i = 0; i < node.items.length; i += 1) {
          const value = this.evalCond(node.items[i], params, rowIndex, [...path, 'items', String(i)]);
          if (value === null) unknown = true;
          else if (node.kind === 'and' && value === false) return false;
          else if (node.kind === 'or' && value === true) return true;
        }
        return unknown ? null : node.kind === 'and';
      }
      case 'eq':
      case 'ne': {
        const left = this.operand(node.left, params, rowIndex, [...path, 'left']);
        const right = this.operand(node.right, params, rowIndex, [...path, 'right']);
        if (left === null || right === null) return null;
        return node.kind === 'eq' ? Object.is(left, right) : !Object.is(left, right);
      }
      case 'lt':
      case 'lte':
      case 'gt':
      case 'gte': {
        const left = this.operand(node.left, params, rowIndex, [...path, 'left']);
        const right = this.operand(node.right, params, rowIndex, [...path, 'right']);
        if (left === null || right === null) return null;
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
          rowIndex,
          path,
        });
    }
  }

  asBool(value, rowIndex, path) {
    if (value === null) return null;
    if (typeof value !== 'boolean') {
      throw new EvalError({
        code: CODES.TYPE,
        reason: `condition requires a boolean, got ${typeof value}`,
        rowIndex,
        path,
      });
    }
    return value;
  }

  operand(node, params, rowIndex, path) {
    switch (node.kind) {
      case 'const':
        return node.value ?? null;
      case 'param':
        return this.lookup(params, node.name, rowIndex, path);
      case 'error':
        throw new EvalError({
          code: node.code ?? CODES.TYPE,
          reason: node.reason ?? 'explicit error node',
          rowIndex,
          path,
        });
      default:
        throw new EvalError({
          code: CODES.UNKNOWN_NODE,
          reason: `unsupported condition operand kind: ${node.kind}`,
          rowIndex,
          path,
        });
    }
  }

  async evalScalar(node, params, rowIndex, path, missingCorrelation) {
    const args = [];
    for (let i = 0; i < node.args.length; i += 1) {
      const spec = node.args[i];
      const argPath = [...path, 'args', String(i)];
      if (own(spec, 'missing')) {
        args.push({ name: spec.name, missing: true });
      } else if (own(spec, 'param')) {
        if (own(params, spec.param)) {
          args.push({ name: spec.name, value: params[spec.param] });
        } else if (missingCorrelation === 'error') {
          throw new EvalError({
            code: CODES.MISSING_PARAM,
            reason: `missing correlated parameter: ${spec.param}`,
            rowIndex,
            path: argPath,
          });
        } else {
          args.push({ name: spec.name, missing: true });
        }
      } else {
        args.push({ name: spec.name, value: spec.value });
      }
    }

    const requestId = this.calls.length + 1;
    const response = await this.runner.resolve({
      readView: `ref-${rowIndex}`,
      requests: [
        {
          requestId,
          sql: node.sql,
          args: args.map((arg) =>
            arg.missing === true
              ? { name: arg.name, missing: true }
              : { name: arg.name, value: arg.value },
          ),
        },
      ],
    });
    const answer = response.find((item) => item.requestId === requestId);
    if (!answer) {
      throw new EvalError({
        code: CODES.BAD_RESPONSE,
        reason: 'reference runner did not answer the request',
        rowIndex,
        path,
      });
    }

    this.calls.push({
      ordinal: this.calls.length + 1,
      rowIndex,
      path: [...path],
      sql: node.sql,
      args,
      key: argKey(node.sql, args),
      rows: answer.error ? null : answer.rows,
      error: answer.error ?? null,
    });

    if (answer.error) {
      throw new EvalError({
        code: answer.error.code ?? CODES.RUNNER,
        reason: answer.error.reason ?? 'runner reported a query error',
        rowIndex,
        path,
      });
    }
    if (answer.rows.length > 1) {
      throw new EvalError({
        code: CODES.SCALAR_CARDINALITY,
        reason: `scalar subquery returned ${answer.rows.length} rows (at most 1 allowed)`,
        rowIndex,
        path,
        detail: { rowCount: answer.rows.length },
      });
    }
    return answer.rows.length === 0 ? null : answer.rows[0][0];
  }
}
