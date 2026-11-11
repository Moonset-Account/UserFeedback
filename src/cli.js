#!/usr/bin/env node
/**
 * CLI 入口：
 *   node src/cli.js <manifest.json> [--json] [--exhaustive-limit N] [--build BUILD_ID ...]
 *
 * 退出码：
 *   0 全部构建可行且在预算内
 *   2 存在超预算或不可行的构建（报告中含冲突诊断）
 *   3 清单本身校验失败
 */

import { readFileSync } from 'node:fs';
import { loadManifest } from './manifest.js';
import { selectAll, selectForBuild } from './selector.js';
import { detectAllCycles } from './graph.js';
import { renderReport } from './report.js';

function parseArgs(argv) {
  const args = { json: false, exhaustiveLimit: undefined, builds: [], manifestPath: null };
  const rest = [...argv];
  while (rest.length > 0) {
    const token = rest.shift();
    if (token === '--json') {
      args.json = true;
    } else if (token === '--exhaustive-limit') {
      const value = Number(rest.shift());
      if (!Number.isInteger(value) || value < 0) {
        throw new Error('--exhaustive-limit 需要非负整数参数');
      }
      args.exhaustiveLimit = value;
    } else if (token === '--build') {
      args.builds.push(rest.shift());
    } else if (token.startsWith('--')) {
      throw new Error(`未知参数：${token}`);
    } else if (args.manifestPath === null) {
      args.manifestPath = token;
    } else {
      throw new Error(`多余的位置参数：${token}`);
    }
  }
  if (args.manifestPath === null) throw new Error('用法：node src/cli.js <manifest.json> [--json] [--exhaustive-limit N] [--build ID ...]');
  return args;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  let manifest;
  try {
    const raw = readFileSync(args.manifestPath, 'utf8');
    manifest = loadManifest(raw);
  } catch (err) {
    if (args.json) {
      console.log(JSON.stringify({ error: err }, null, 2));
    } else {
      console.error(`加载清单失败 [${err.code}]：${err.message}`);
      for (const d of err.details ?? []) {
        console.error(`  - [${d.code}] ${d.message}`);
      }
    }
    process.exitCode = 3;
    return;
  }

  const opts = args.exhaustiveLimit === undefined ? {} : { exhaustiveLimit: args.exhaustiveLimit };
  const cycleWarnings = detectAllCycles(manifest.cases, manifest.caseIndex, manifest.builds);

  let selection;
  if (args.builds.length > 0) {
    const builds = args.builds.map((id) => selectForBuild(manifest, id, opts));
    selection = {
      builds,
      summary: builds.reduce(
        (acc, r) => {
          if (r.status === 'FEASIBLE') acc.feasible += 1;
          if (r.status === 'OVER_BUDGET') acc.overBudget += 1;
          if (r.status === 'INFEASIBLE') acc.infeasible += 1;
          if (typeof r.totalMinutes === 'number') acc.minutes += r.totalMinutes;
          return acc;
        },
        { feasible: 0, overBudget: 0, infeasible: 0, minutes: 0 },
      ),
      globalBudgetMinutes: builds.reduce((acc, r) => acc + r.budgetMinutes, 0),
    };
  } else {
    selection = selectAll(manifest, opts);
  }

  if (args.json) {
    console.log(JSON.stringify({ cycleWarnings, ...selection }, null, 2));
  } else {
    console.log(renderReport(selection, cycleWarnings));
  }

  if (selection.summary.overBudget + selection.summary.infeasible > 0) {
    process.exitCode = 2;
  }
}

main();
