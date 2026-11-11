#!/usr/bin/env node
/**
 * 审计器 CLI。
 *
 * 用法：
 *   node bin/audit.js --dockerfile <file> --target <stage> \
 *                     [--params <json>] [--approvals <json>] [--json]
 *
 * 退出码：
 *   0 PASS / 2 FAIL / 3 INCONCLUSIVE（待核实） / 1 用法或输入错误
 */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { audit } from '../src/auditor.js';
import { formatTextReport } from '../src/report.js';

function parseFlags(argv) {
  const flags = {};
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--json') {
      flags.json = true;
    } else if (arg.startsWith('--')) {
      const key = arg.slice(2);
      flags[key] = argv[i + 1];
      i += 1;
    }
  }
  return flags;
}

function readJson(path) {
  if (!path) return undefined;
  return JSON.parse(readFileSync(resolve(path), 'utf8'));
}

function main() {
  const flags = parseFlags(process.argv.slice(2));
  if (!flags.dockerfile || !flags.target) {
    process.stderr.write(
      '用法: audit.js --dockerfile <file> --target <stage> [--params <json>] [--approvals <json>] [--json]\n',
    );
    process.exit(1);
  }

  let dockerfile;
  let params;
  let approvals;
  try {
    dockerfile = readFileSync(resolve(flags.dockerfile), 'utf8');
    params = readJson(flags.params) || {};
    approvals = readJson(flags.approvals) || [];
  } catch (err) {
    process.stderr.write(`输入读取失败: ${err.message}\n`);
    process.exit(1);
  }

  let report;
  try {
    report = audit({
      dockerfile,
      target: flags.target,
      buildParams: params,
      approvals,
    });
  } catch (err) {
    process.stderr.write(`审计失败: ${err.message}\n`);
    process.exit(1);
  }

  process.stdout.write(flags.json ? `${JSON.stringify(report, null, 2)}\n` : `${formatTextReport(report)}\n`);
  process.exit(report.decision === 'PASS' ? 0 : report.decision === 'INCONCLUSIVE' ? 3 : 2);
}

main();
