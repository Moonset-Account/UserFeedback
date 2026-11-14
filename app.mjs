#!/usr/bin/env node
// SEC-VULN-031 — JWT 验签密钥轮换离线审计工具 CLI 入口。
// 仅使用 Node.js 标准模块，全部输入为本地合成 JSON 文件，不访问任何外部系统。
import { readFile, writeFile } from 'node:fs/promises';
import { audit } from './src/audit.mjs';

const USAGE = `用法:
  node app.mjs --schedule <轮换时间表.json> --cache <缓存记录.json> --decisions <验签决策.json> [--out <报告.json>] [--at <毫秒时间戳>]

参数:
  --schedule   颁发方轮换时间表（issuers[].keys[]: kid/published_at/retired_at/grace_ms）
  --cache      验证实例缓存版本与刷新记录（instances[]: snapshots[] + refresh_events[]）
  --decisions  验签决策摘要（decisions[]: instance_id/cache_version/decided_at/issuer_id/kid/result[/case_id]）
  --out        报告输出路径；缺省打印到 stdout
  --at         额外推导该时刻各实例/颁发方的应信密钥集合
  --help       显示本说明

退出码: 0 = 审计完成（是否有发现见报告 summary）；2 = 参数或输入错误。`;

function parseArgs(argv) {
  const opts = { schedule: null, cache: null, decisions: null, out: null, at: null, help: false };
  for (let i = 2; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === '--help' || a === '-h') { opts.help = true; continue; }
    if (!a.startsWith('--')) throw new Error(`未知参数: ${a}`);
    const key = a.slice(2);
    if (!(key in opts) || key === 'help') throw new Error(`未知参数: ${a}`);
    const val = argv[i + 1];
    if (val === undefined) throw new Error(`缺少参数值: ${a}`);
    opts[key] = val;
    i += 1;
  }
  return opts;
}

async function readJson(path) {
  const text = await readFile(path, 'utf8');
  return JSON.parse(text);
}

async function main() {
  let opts;
  try {
    opts = parseArgs(process.argv);
  } catch (e) {
    console.error(`参数错误: ${e.message}`);
    console.error(USAGE);
    process.exit(2);
  }
  if (opts.help) {
    console.log(USAGE);
    return;
  }
  if (!opts.schedule || !opts.cache || !opts.decisions) {
    console.error('错误: 必须同时提供 --schedule、--cache、--decisions');
    console.error(USAGE);
    process.exit(2);
  }
  let at = null;
  if (opts.at !== null) {
    at = Number(opts.at);
    if (!Number.isFinite(at)) {
      console.error(`错误: --at 必须是数值毫秒时间戳，收到: ${opts.at}`);
      process.exit(2);
    }
  }

  let schedule; let cache; let decisions;
  try {
    schedule = await readJson(opts.schedule);
    cache = await readJson(opts.cache);
    decisions = await readJson(opts.decisions);
  } catch (e) {
    console.error(`读取输入失败: ${e.message}`);
    process.exit(2);
  }

  const report = audit({ schedule, cache, decisions, at });
  report.generated_at = new Date().toISOString();
  report.inputs = { schedule: opts.schedule, cache: opts.cache, decisions: opts.decisions };
  const json = `${JSON.stringify(report, null, 2)}\n`;

  if (opts.out) {
    await writeFile(opts.out, json);
    const s = report.summary;
    console.log(`审计完成: ${s.decisions_total} 条决策，${s.findings_total} 项发现，${s.anomalies_total} 项异常。报告已写入 ${opts.out}`);
  } else {
    process.stdout.write(json);
  }
}

main().catch((e) => {
  console.error(`审计失败: ${e.message}`);
  process.exit(2);
});
