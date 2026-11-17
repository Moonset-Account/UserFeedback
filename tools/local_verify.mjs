#!/usr/bin/env node
// Local verification entry point.
//
// Usage:
//   node tools/local_verify.mjs --list
//   node tools/local_verify.mjs --case C01
//   node tools/local_verify.mjs --case C01,C02
//
// Exit code: 0 only when every requested case ran and every real assertion
// passed. Missing entry, unimplemented case, zero assertions or any failed
// assertion exits non-zero. Output is one JSON record per case.

import { CASES } from './cases.mjs';

// ---------------------------------------------------------------------------
// Redaction: seeds/samples are synthetic, but nonce/hash values and block
// references are still masked so verifier output never carries usable
// policy secrets or raw sensitive references.
// ---------------------------------------------------------------------------

const NONCE_KEYS = new Set(['nonce']);

function redact(value, key = null) {
  if (value === null || value === undefined) return value;
  if (Array.isArray(value)) return value.map((v) => redact(v, key));
  if (typeof value === 'string') {
    if (key && NONCE_KEYS.has(key)) return '***';
    return value
      .replace(/'nonce-[^']+'/gi, "'nonce-***'")
      .replace(/(nonce\s+)[A-Za-z0-9+/=_-]+/gi, '$1***');
  }
  if (typeof value === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(value)) out[k] = redact(v, k);
    return out;
  }
  return value;
}

function runCase(testCase) {
  const checks = [];
  const assert = (condition, label, detail = {}) => {
    checks.push({
      label,
      passed: Boolean(condition),
      expected: detail.expected ?? null,
      observed: detail.observed ?? null,
    });
  };

  let output;
  let fatalError = null;

  if (typeof testCase?.run !== 'function') {
    fatalError = 'case entry missing or unimplemented';
  } else {
    try {
      output = testCase.run(assert);
    } catch (error) {
      fatalError = error?.stack ? String(error.stack).split('\n').slice(0, 4).join(' | ')
        : String(error);
    }
  }

  const passed = checks.filter((c) => c.passed).length;
  const failed = checks.filter((c) => !c.passed).length;
  const skipped = 0; // this harness never skips assertions

  const record = {
    case: testCase?.id ?? null,
    title: testCase?.title ?? null,
    assertionCount: checks.length,
    passed,
    failed,
    skipped,
    status: fatalError
      ? 'error'
      : (checks.length === 0
          ? 'no-assertions'
          : (failed === 0 ? 'passed' : 'failed')),
    seed: redact(output?.seed ?? null),
    sampleSummary: output?.sample ? {
      origin: output.sample?.origin ?? output.seed?.origin ?? null,
      policyCount: output.sample.policies?.length ?? null,
      scriptCount: output.sample.scripts?.length ?? null,
      policies: (output.sample.policies ?? []).map((p) => ({
        id: p.id,
        mode: p.mode,
        directiveText: redact(p.text),
      })),
      scripts: (output.sample.scripts ?? []).map((s) => ({
        id: s.id,
        kind: s.kind,
        parserInserted: s.parserInserted,
        hasNonce: s.nonce != null && s.nonce !== '',
        creator: s.creator ?? null,
        reference: s.kind === 'external' ? s.url : `inline:${s.id}`,
      })),
    } : null,
    expectationSummary: checks.map((c) => ({
      label: c.label,
      passed: c.passed,
      expected: redact(c.expected),
      observed: redact(c.observed),
    })),
    resultSummary: output?.summary ? {
      policies: output.summary.policies.map((p) => ({
        id: p.id,
        mode: p.mode,
        effectiveDirective: p.effectiveDirective,
        directiveText: redact(p.effectiveDirectiveText),
        duplicateDirectives: p.duplicates.map((d) => d.directive),
        unsupportedReasons: p.unsupported.map((u) => u.reason),
      })),
      scripts: output.summary.scripts.map((s) => ({
        scriptId: s.scriptId,
        executes: s.executes,
        perPolicy: s.decisions.map((d) => ({
          policyId: d.policyId,
          mode: d.mode,
          directive: d.directive,
          allowed: d.allowed,
          basis: redact(d.basis),
          reason: d.reason,
        })),
      })),
    } : null,
    error: fatalError,
  };

  return record;
}

function main(argv) {
  const args = argv.slice(2);
  const listIdx = args.indexOf('--list');
  const caseIdx = args.indexOf('--case');

  if (listIdx !== -1) {
    process.stdout.write(JSON.stringify(CASES.map((c) => ({ id: c.id, title: c.title }))));
    process.stdout.write('\n');
    return 0;
  }

  if (caseIdx === -1 || caseIdx + 1 >= args.length) {
    process.stderr.write('usage: node tools/local_verify.mjs --list | --case C01[,C02,...]\n');
    return 2;
  }

  const wanted = args[caseIdx + 1].split(',').map((x) => x.trim()).filter(Boolean);
  const records = [];
  let ok = true;

  for (const id of wanted) {
    const testCase = CASES.find((c) => c.id === id);
    if (!testCase) {
      records.push({
        case: id,
        assertionCount: 0,
        passed: 0,
        failed: 0,
        skipped: 0,
        status: 'error',
        error: 'unknown case id',
      });
      ok = false;
      continue;
    }
    const record = runCase(testCase);
    records.push(record);
    if (record.status !== 'passed' || record.assertionCount === 0) ok = false;
  }

  process.stdout.write(JSON.stringify(records, null, 2));
  process.stdout.write('\n');
  return ok ? 0 : 1;
}

process.exit(main(process.argv));
