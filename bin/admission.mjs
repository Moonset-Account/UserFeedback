#!/usr/bin/env node
/**
 * Command line entry point.
 *
 *   node bin/admission.mjs analyze  <spec.json> <initial.json> [--max-states=N] [--json]
 *   node bin/admission.mjs round    <spec.json> <initial.json> <order.json> [--json]
 *   node bin/admission.mjs verify   <spec.json> <evidence.json> [--json]
 *
 * Spec = { domain, hooks, accept }. Order = JSON array of hook ids.
 * The command prints a JSON result; exit code 0 when the interpreter ran
 * (regardless of verdict), 1 on invalid input, 2 on usage error.
 */
import { readFileSync } from 'node:fs';
import process from 'node:process';

import {
  AdmissionError,
  analyze,
  defineProgram,
  replayEvidence,
  runRound,
} from '../src/index.mjs';

function usage() {
  return [
    'usage:',
    '  node bin/admission.mjs analyze <spec.json> <initial.json> [--max-states=N] [--json]',
    '  node bin/admission.mjs round   <spec.json> <initial.json> <order.json> [--json]',
    '  node bin/admission.mjs verify  <spec.json> <evidence.json> [--json]',
  ].join('\n');
}

function readJson(path) {
  let text;
  try {
    text = readFileSync(path, 'utf8');
  } catch (error) {
    throw new AdmissionError('FILE_READ', `cannot read ${path}: ${error.message}`, {
      path,
    });
  }
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new AdmissionError('BAD_JSON', `${path} is not valid JSON: ${error.message}`, {
      path,
    });
  }
}

function parseOptions(argv) {
  const positional = [];
  const options = { json: false, maxStates: undefined };
  for (const argument of argv) {
    if (argument === '--json') {
      options.json = true;
    } else if (argument.startsWith('--max-states=')) {
      const raw = argument.slice('--max-states='.length);
      const value = Number(raw);
      if (!Number.isInteger(value) || value < 1) {
        throw new AdmissionError('BAD_OPTION', `invalid --max-states ${raw}`);
      }
      options.maxStates = value;
    } else if (argument.startsWith('--')) {
      throw new AdmissionError('BAD_OPTION', `unknown option ${argument}`);
    } else {
      positional.push(argument);
    }
  }
  return { positional, options };
}

function summarizeAnalysis(result) {
  const lines = [];
  lines.push(`verdict: ${result.verdict}`);
  lines.push(`admissible: ${result.admissible}`);
  lines.push(`allRunsTerminate: ${result.allRunsTerminate}`);
  lines.push(`uniformTermination: ${result.uniformTermination}`);
  lines.push(
    `reachableStates: ${result.stats.reachableStates} (permutations if enumerated: ${result.stats.allPermutations})`,
  );
  if (result.terminationOutcomes.length > 0) {
    lines.push('terminating outcomes:');
    for (const outcome of result.terminationOutcomes) {
      lines.push(`  ${outcome.decision} ${JSON.stringify(outcome.object)}`);
    }
  }
  if (result.nonterminationWitnesses.length > 0) {
    lines.push(`nontermination witnesses: ${result.nonterminationWitnesses.length}`);
  }
  for (const finding of result.findings) {
    lines.push(`finding ${finding.kind}: ${finding.message}`);
    for (const witness of finding.witnesses) {
      lines.push(`  evidence ${witness.kind}: ${JSON.stringify(witness)}`);
    }
  }
  return lines.join('\n');
}

function main(argv) {
  const [command, ...rest] = argv;
  if (command === undefined || command === '--help' || command === '-h') {
    console.log(usage());
    return 0;
  }
  const { positional, options } = parseOptions(rest);
  if (command === 'analyze') {
    if (positional.length !== 2) {
      console.error(usage());
      return 2;
    }
    const program = defineProgram(readJson(positional[0]));
    const result = analyze(program, readJson(positional[1]), {
      maxStates: options.maxStates,
    });
    console.log(options.json ? JSON.stringify(result, null, 2) : summarizeAnalysis(result));
    return 0;
  }
  if (command === 'round') {
    if (positional.length !== 3) {
      console.error(usage());
      return 2;
    }
    const program = defineProgram(readJson(positional[0]));
    const result = runRound(program, readJson(positional[1]), readJson(positional[2]));
    console.log(JSON.stringify(result, null, 2));
    return 0;
  }
  if (command === 'verify') {
    if (positional.length !== 2) {
      console.error(usage());
      return 2;
    }
    const program = defineProgram(readJson(positional[0]));
    const result = replayEvidence(program, readJson(positional[1]));
    console.log(JSON.stringify(result, null, 2));
    return result.ok ? 0 : 1;
  }
  console.error(`unknown command ${command}\n${usage()}`);
  return 2;
}

try {
  process.exitCode = main(process.argv.slice(2));
} catch (error) {
  if (error instanceof AdmissionError) {
    console.error(`${error.code}: ${error.message}`);
    if (Object.keys(error.details).length > 0) {
      console.error(JSON.stringify(error.details, null, 2));
    }
    process.exitCode = 1;
  } else {
    throw error;
  }
}
