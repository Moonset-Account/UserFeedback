#!/usr/bin/env node
// SEC-VULN-048 扫描告警豁免有效性审计器（离线 CLI）。
// 用法：
//   node app.mjs --alerts alerts.json --exemptions exemptions.json --at 2026-11-14T00:00:00Z [--out report.json]
//
// 不读取系统时钟（核验时间必须显式传入），不发起任何网络请求。

import { readFile, writeFile } from 'node:fs/promises';
import { parseArgs } from 'node:util';
import {
  loadJsonFile,
  parseAuditTime,
  validateAlerts,
  validateExemptions,
} from './src/parse.mjs';
import { buildReport } from './src/audit.mjs';

function usage() {
  return [
    '用法：node app.mjs --alerts <告警JSON> --exemptions <豁免JSON> --at <ISO8601核验时间> [--out <报告路径>]',
    '',
    '  --alerts       已归并的合成告警 JSON（数组或 { alerts: [...] }）',
    '  --exemptions   人工豁免记录 JSON（数组或 { exemptions: [...] }）',
    '  --at           核验时间，ISO 8601；为保证可重跑，不使用系统时钟',
    '  --out          报告输出路径；缺省写到标准输出',
  ].join('\n');
}

async function main(argv) {
  let parsed;
  try {
    parsed = parseArgs({
      args: argv,
      options: {
        alerts: { type: 'string' },
        exemptions: { type: 'string' },
        at: { type: 'string' },
        out: { type: 'string' },
        help: { type: 'boolean', default: false },
      },
      strict: true,
      allowPositionals: false,
    });
  } catch (err) {
    process.stderr.write(`参数错误：${err.message}\n\n${usage()}\n`);
    return 2;
  }

  const { values } = parsed;
  if (values.help) {
    process.stdout.write(`${usage()}\n`);
    return 0;
  }
  if (!values.alerts || !values.exemptions || !values.at) {
    process.stderr.write(`参数错误：--alerts、--exemptions、--at 均为必填项\n\n${usage()}\n`);
    return 2;
  }

  const fs = { readFile };
  const auditTimeMs = parseAuditTime(values.at);
  const alertInput = await loadJsonFile(values.alerts, fs);
  const exemptionInput = await loadJsonFile(values.exemptions, fs);

  const alerts = validateAlerts(alertInput);
  const exemptions = validateExemptions(exemptionInput);

  const report = buildReport(alerts, exemptions, auditTimeMs, values.at);
  const rendered = `${JSON.stringify(report, null, 2)}\n`;

  if (values.out) {
    await writeFile(values.out, rendered, 'utf8');
  } else {
    process.stdout.write(rendered);
  }
  return 0;
}

main(process.argv.slice(2))
  .then((code) => {
    process.exitCode = code;
  })
  .catch((err) => {
    if (err?.code === 'INPUT_INVALID') {
      process.stderr.write(`${err.message}\n`);
      process.exitCode = 1;
    } else {
      process.stderr.write(`内部错误：${err?.stack ?? err}\n`);
      process.exitCode = 70;
    }
  });
