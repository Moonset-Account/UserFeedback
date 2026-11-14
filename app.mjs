#!/usr/bin/env node
// Trusted Types 策略覆盖审计 CLI。
// 用法：
//   node app.mjs --snapshot <dir> [--out report.json] [--fail-on-findings]
//   node app.mjs --snapshot-a <dir> --snapshot-b <dir> [--out report.json] [--fail-on-findings]
// 退出码：0 成功；1 参数/读取错误；2 存在 findings 且指定 --fail-on-findings。

import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  loadSnapshot,
  analyzeSnapshot,
  diffSnapshots,
  buildReport,
} from './lib/analyzer.mjs';

const USAGE = `用法:
  node app.mjs --snapshot <dir>                 审计单份构建快照
  node app.mjs --snapshot-a <dir> --snapshot-b <dir>  对照两版构建快照
选项:
  --out <file>            将 JSON 报告写入文件（默认输出到 stdout）
  --fail-on-findings      存在任何 finding 时以退出码 2 结束（供 CI 门禁）
  --help                  显示本说明
快照目录需包含: entries.json, csp.json, policies.json, sinks.json, dynamic-deps.json`;

export function parseArgs(argv) {
  const opts = { snapshot: null, a: null, b: null, out: null, failOnFindings: false, help: false };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    switch (arg) {
      case '--snapshot': opts.snapshot = argv[++i]; break;
      case '--snapshot-a': opts.a = argv[++i]; break;
      case '--snapshot-b': opts.b = argv[++i]; break;
      case '--out': opts.out = argv[++i]; break;
      case '--fail-on-findings': opts.failOnFindings = true; break;
      case '--help': case '-h': opts.help = true; break;
      default: throw new Error(`未知参数: ${arg}`);
    }
  }
  return opts;
}

export async function run(argv, { stdout = process.stdout, stderr = process.stderr } = {}) {
  let opts;
  try {
    opts = parseArgs(argv);
  } catch (err) {
    stderr.write(`${err.message}\n${USAGE}\n`);
    return 1;
  }
  if (opts.help) {
    stdout.write(`${USAGE}\n`);
    return 0;
  }
  const currentDir = opts.b ?? opts.snapshot;
  if (!currentDir) {
    stderr.write(`缺少快照目录参数。\n${USAGE}\n`);
    return 1;
  }
  if (opts.a && !opts.b) {
    stderr.write(`指定 --snapshot-a 时必须同时指定 --snapshot-b。\n${USAGE}\n`);
    return 1;
  }

  let report;
  try {
    const current = await loadSnapshot(currentDir);
    const anaCurrent = analyzeSnapshot(current);
    let diff = null;
    let baseLabel = null;
    if (opts.a) {
      const base = await loadSnapshot(opts.a);
      const anaBase = analyzeSnapshot(base);
      diff = diffSnapshots(base, current, anaBase, anaCurrent);
      baseLabel = base.label;
    }
    report = buildReport({ analysis: anaCurrent, diff, baseLabel });
  } catch (err) {
    stderr.write(`读取或分析快照失败: ${err.message}\n`);
    return 1;
  }

  const json = `${JSON.stringify(report, null, 2)}\n`;
  if (opts.out) {
    await writeFile(path.resolve(opts.out), json, 'utf8');
    stderr.write(`报告已写入 ${opts.out}（findings: ${report.summary.findings}）\n`);
  } else {
    stdout.write(json);
  }
  if (opts.failOnFindings && report.findings.length > 0) return 2;
  return 0;
}

const invokedAs = process.argv[1] ? path.resolve(process.argv[1]) : null;
if (invokedAs && invokedAs === fileURLToPath(import.meta.url)) {
  run(process.argv.slice(2)).then((code) => {
    process.exitCode = code;
  }).catch((err) => {
    process.stderr.write(`未处理错误: ${err?.stack ?? err}\n`);
    process.exitCode = 1;
  });
}
