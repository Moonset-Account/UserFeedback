#!/usr/bin/env python3
"""SEC-20261114-055 CLI 主入口：MFA 挑战与一次性导出许可兑换。

用法：
    python3 app.py scenarios           列出可重现场景
    python3 app.py demo [场景名|all]   运行场景并输出 JSON（结果 + 审计记录）

输入：场景名（命令行参数）。
输出：stdout 上的 JSON，包含每个步骤的判定结果与完整审计轨迹。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading

from mfa_export import (
    AuditLog,
    ExportLicenseService,
    FakeMFAAdapter,
    FakeSSOAdapter,
    ManualClock,
    Reason,
)

SCOPE = "key-access-audit"
TOKEN_S1 = "token-alice-s1"  # alice 在会话 sess-1 的 SSO token
TOKEN_S2 = "token-alice-s2"  # alice 在会话 sess-2 的 SSO token（会话切换用）
PROOF_OK = "correct-proof"
PROOF_BAD = "wrong-proof"


def build_env(start: float = 1000.0):
    """搭建一套可控环境：时钟、身份、权限、MFA 期望值。"""
    clock = ManualClock(start)
    sso = FakeSSOAdapter()
    sso.register_identity(TOKEN_S1, "tenant-acme", "alice", "sess-1")
    sso.register_identity(TOKEN_S2, "tenant-acme", "alice", "sess-2")
    sso.set_export_permission("tenant-acme", "alice", SCOPE, True)
    mfa = FakeMFAAdapter(default_proof=PROOF_OK)
    audit = AuditLog()
    service = ExportLicenseService(sso, mfa, clock, audit)
    return service, clock, sso, mfa, audit


def _result(scenario: str, steps: dict, audit: AuditLog) -> dict:
    return {"scenario": scenario, "steps": steps, "audit": audit.to_list()}


def scenario_happy_path() -> dict:
    """完整链路：发起 -> 确认 -> 兑换成功，拿到密钥访问审计记录。"""
    svc, clock, sso, mfa, audit = build_env()
    init = svc.initiate_mfa(TOKEN_S1, SCOPE)
    confirm = svc.confirm_challenge(init["challenge_id"], PROOF_OK)
    redeem = svc.redeem_license(confirm["license_id"], TOKEN_S1)
    return _result("happy_path", {"initiate": init, "confirm": confirm,
                                  "redeem": redeem}, audit)


def scenario_challenge_timeout() -> dict:
    """挑战超时：两分钟后确认被拒绝。"""
    svc, clock, sso, mfa, audit = build_env()
    init = svc.initiate_mfa(TOKEN_S1, SCOPE)
    clock.advance(121)
    confirm = svc.confirm_challenge(init["challenge_id"], PROOF_OK)
    return _result("challenge_timeout", {"initiate": init, "confirm": confirm}, audit)


def scenario_mfa_failure() -> dict:
    """MFA 验证失败：错误凭证被拒，且凭证不进入审计。"""
    svc, clock, sso, mfa, audit = build_env()
    init = svc.initiate_mfa(TOKEN_S1, SCOPE)
    confirm = svc.confirm_challenge(init["challenge_id"], PROOF_BAD)
    return _result("mfa_failure", {"initiate": init, "confirm": confirm}, audit)


def scenario_one_license_per_challenge() -> dict:
    """每个挑战最多签发一张许可：第二次确认被拒。"""
    svc, clock, sso, mfa, audit = build_env()
    init = svc.initiate_mfa(TOKEN_S1, SCOPE)
    first = svc.confirm_challenge(init["challenge_id"], PROOF_OK)
    second = svc.confirm_challenge(init["challenge_id"], PROOF_OK)
    return _result("one_license_per_challenge",
                   {"initiate": init, "first": first, "second": second}, audit)


def scenario_license_expired() -> dict:
    """许可过期：签发五分钟后兑换被拒。"""
    svc, clock, sso, mfa, audit = build_env()
    init = svc.initiate_mfa(TOKEN_S1, SCOPE)
    confirm = svc.confirm_challenge(init["challenge_id"], PROOF_OK)
    clock.advance(301)
    redeem = svc.redeem_license(confirm["license_id"], TOKEN_S1)
    return _result("license_expired",
                   {"initiate": init, "confirm": confirm, "redeem": redeem}, audit)


def scenario_session_switch() -> dict:
    """会话切换：同一用户换会话后兑换被拒。"""
    svc, clock, sso, mfa, audit = build_env()
    init = svc.initiate_mfa(TOKEN_S1, SCOPE)
    confirm = svc.confirm_challenge(init["challenge_id"], PROOF_OK)
    redeem = svc.redeem_license(confirm["license_id"], TOKEN_S2)
    return _result("session_switch",
                   {"initiate": init, "confirm": confirm, "redeem": redeem}, audit)


def scenario_permission_revoked() -> dict:
    """权限撤回：许可签发后权限被撤回，兑换时被拒。"""
    svc, clock, sso, mfa, audit = build_env()
    init = svc.initiate_mfa(TOKEN_S1, SCOPE)
    confirm = svc.confirm_challenge(init["challenge_id"], PROOF_OK)
    sso.set_export_permission("tenant-acme", "alice", SCOPE, False)
    redeem = svc.redeem_license(confirm["license_id"], TOKEN_S1)
    return _result("permission_revoked",
                   {"initiate": init, "confirm": confirm, "redeem": redeem}, audit)


def scenario_license_reused() -> dict:
    """许可已使用：同一许可顺序兑换两次，第二次被拒。"""
    svc, clock, sso, mfa, audit = build_env()
    init = svc.initiate_mfa(TOKEN_S1, SCOPE)
    confirm = svc.confirm_challenge(init["challenge_id"], PROOF_OK)
    first = svc.redeem_license(confirm["license_id"], TOKEN_S1)
    second = svc.redeem_license(confirm["license_id"], TOKEN_S1)
    return _result("license_reused",
                   {"initiate": init, "confirm": confirm,
                    "first": first, "second": second}, audit)


def scenario_concurrent_redeem(threads: int = 8) -> dict:
    """并发兑换：N 个请求同时兑换同一许可，只有一个拿到审计记录。"""
    svc, clock, sso, mfa, audit = build_env()
    init = svc.initiate_mfa(TOKEN_S1, SCOPE)
    confirm = svc.confirm_challenge(init["challenge_id"], PROOF_OK)
    license_id = confirm["license_id"]

    barrier = threading.Barrier(threads)
    results = [None] * threads

    def worker(i: int) -> None:
        barrier.wait()
        results[i] = svc.redeem_license(license_id, TOKEN_S1)

    pool = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()

    winners = [r for r in results if r["ok"]]
    steps = {
        "initiate": init,
        "confirm": confirm,
        "threads": threads,
        "winners": len(winners),
        "payloads_delivered": sum(1 for r in results if r["ok"]),
        "deny_reasons": sorted(r["reason"] for r in results if not r["ok"]),
    }
    return _result("concurrent_redeem", steps, audit)


SCENARIOS = {
    "happy_path": scenario_happy_path,
    "challenge_timeout": scenario_challenge_timeout,
    "mfa_failure": scenario_mfa_failure,
    "one_license_per_challenge": scenario_one_license_per_challenge,
    "license_expired": scenario_license_expired,
    "session_switch": scenario_session_switch,
    "permission_revoked": scenario_permission_revoked,
    "license_reused": scenario_license_reused,
    "concurrent_redeem": scenario_concurrent_redeem,
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scenarios", help="列出可重现场景")
    demo = sub.add_parser("demo", help="运行场景并输出 JSON")
    demo.add_argument("scenario", nargs="?", default="all",
                      choices=["all"] + sorted(SCENARIOS))
    args = parser.parse_args(argv)

    if args.command == "scenarios":
        json.dump(sorted(SCENARIOS), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    names = sorted(SCENARIOS) if args.scenario == "all" else [args.scenario]
    out = [SCENARIOS[name]() for name in names]
    json.dump(out[0] if len(out) == 1 else out, sys.stdout, indent=2,
              sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
