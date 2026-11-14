#!/usr/bin/env python3
"""CLI entry point for the outbound alert JSONL converter.

Reads gateway or endpoint alert JSONL, applies the per-source field
policy (keep / replace-with-codename / required), and writes an outbound
JSONL plus an error report JSONL whose entries contain only
{source, line, reason}.

Trusted context (tenant, key_version) comes from the outbound task file;
the actual HMAC key material is looked up in a local keystore keyed by
(tenant, key_version).  All inputs here are local synthetic fixtures.

Usage:
    python3 app.py --source gateway --task samples/task.json \
        --input samples/gateway.jsonl --output out.jsonl \
        --errors errors.jsonl
"""

import argparse
import json
import os
import sys

import transform


def load_json_file(path, what):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except OSError as exc:
        raise SystemExit("cannot read %s %s: %s" % (what, path, exc))
    except json.JSONDecodeError as exc:
        raise SystemExit("invalid JSON in %s %s: %s" % (what, path, exc))


def load_task(task):
    if not isinstance(task, dict):
        raise SystemExit("task file must contain a JSON object")
    tenant = task.get("tenant")
    key_version = task.get("key_version")
    if not isinstance(tenant, str) or not tenant:
        raise SystemExit("task file missing non-empty string field 'tenant'")
    if not isinstance(key_version, str) or not key_version:
        raise SystemExit("task file missing non-empty string field 'key_version'")
    return tenant, key_version


def load_key(keystore, tenant, key_version):
    try:
        hexkey = keystore[tenant][key_version]
    except (KeyError, TypeError):
        raise SystemExit(
            "no key material for tenant=%r key_version=%r in keystore"
            % (tenant, key_version))
    if not isinstance(hexkey, str):
        raise SystemExit("keystore entry for tenant=%r key_version=%r is not "
                         "a hex string" % (tenant, key_version))
    try:
        return bytes.fromhex(hexkey)
    except ValueError:
        raise SystemExit("keystore entry for tenant=%r key_version=%r is not "
                         "valid hex" % (tenant, key_version))


def run(source, policy, key_bytes, tenant, key_version,
        input_fh, output_fh, errors_fh):
    """Convert every line; skipped lines go to the error report.

    Error entries contain exactly three keys: source, line, reason.
    """
    for lineno, line in enumerate(input_fh, 1):
        text = line.strip()
        if not text:
            continue  # blank lines are ignored, not errors
        record = None
        reason = None
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            reason = "invalid JSON"
        else:
            try:
                record = transform.transform_record(
                    obj, policy, key_bytes, tenant, key_version)
            except transform.LineError as exc:
                reason = str(exc)
        if record is None:
            errors_fh.write(json.dumps(
                {"source": source, "line": lineno, "reason": reason},
                sort_keys=True) + "\n")
        else:
            output_fh.write(json.dumps(
                record, sort_keys=True, separators=(",", ":")) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Convert gateway/endpoint alert JSONL for outbound "
                    "sharing with per-source field policies and HMAC "
                    "codenames.")
    parser.add_argument("--source", required=True,
                        help="source name; selects policies/<source>.json "
                             "unless --policy is given")
    parser.add_argument("--policy",
                        help="explicit policy file path (overrides --source "
                             "lookup)")
    parser.add_argument("--task", required=True,
                        help="outbound task JSON with tenant and key_version")
    parser.add_argument("--input", required=True, help="input JSONL path")
    parser.add_argument("--output", required=True, help="output JSONL path")
    parser.add_argument("--errors", required=True,
                        help="error report JSONL path")
    parser.add_argument("--keystore", default="keys.json",
                        help="local keystore JSON (default: keys.json)")
    parser.add_argument("--policies-dir", default="policies",
                        help="policy directory (default: policies)")
    args = parser.parse_args(argv)

    policy_path = args.policy or os.path.join(args.policies_dir,
                                              args.source + ".json")
    try:
        policy = transform.load_policy(
            load_json_file(policy_path, "policy file"))
    except transform.PolicyError as exc:
        raise SystemExit("invalid policy %s: %s" % (policy_path, exc))

    tenant, key_version = load_task(load_json_file(args.task, "task file"))
    key_bytes = load_key(load_json_file(args.keystore, "keystore"),
                         tenant, key_version)

    try:
        input_fh = open(args.input, "r", encoding="utf-8")
    except OSError as exc:
        raise SystemExit("cannot read input %s: %s" % (args.input, exc))
    with input_fh, \
            open(args.output, "w", encoding="utf-8") as output_fh, \
            open(args.errors, "w", encoding="utf-8") as errors_fh:
        run(args.source, policy, key_bytes, tenant, key_version,
            input_fh, output_fh, errors_fh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
