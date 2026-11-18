#!/usr/bin/env node
// analyze.mjs — 对单个 CFG JSON 文件运行分析并打印结果。
// 用法： node tools/analyze.mjs <cfg.json>

import { readFile } from 'node:fs/promises';
import { analyze } from '../src/index.mjs';

const file = process.argv[2];
if (!file) {
  console.error('usage: node tools/analyze.mjs <cfg.json>');
  process.exit(2);
}
const cfg = JSON.parse(await readFile(file, 'utf8'));
console.log(JSON.stringify(analyze(cfg), null, 2));
