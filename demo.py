"""命令行演示入口。

运行（在仓库根目录）：
    python3 -I demo.py

场景：付款终端的大额转账操作要求 password + fido2，且至少 2 个相互独立
证明；登记因素覆盖"只接企业安全密钥的终端"与"手机 + 硬件凭据"两种形态，
并演示同设备共存、恢复渠道关联、因素停用后重新规划。
"""

from __future__ import annotations

import os
import sys

# 支持 python3 -I 隔离运行：显式把仓库根目录加入模块搜索路径。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auth_planner import (
    CAPABILITY_FIDO2,
    CAPABILITY_HARDWARE_OTP,
    CAPABILITY_MOBILE_PUSH,
    CAPABILITY_PASSWORD,
    CAPABILITY_TOTP,
    AuthFactor,
    AuthenticationPlanner,
    OperationRequirement,
)


def _print_result(title: str, result) -> None:
    print(f"\n=== {title} ===")
    if result.feasible:
        print(
            "可行方案: "
            + ", ".join(result.factor_ids)
            + f" | 总步骤={result.total_steps}"
            + f" | 独立证明数={result.independent_proofs}"
            + f" | 覆盖能力={sorted(result.covered_capabilities)}"
        )
    else:
        print("无可行方案: " + result.reason)
        for item in result.missing:
            print(
                f"  - 能力 {item.capability}: 可用={item.available}, "
                f"最大独立来源={item.max_independent_sources}, "
                f"单一来源={item.single_source}"
            )
        print(f"  独立性缺口={result.independence_deficit}")


def build_demo_planner() -> AuthenticationPlanner:
    factors = [
        # 终端 A：只接了企业安全密钥（FIDO2），密码也在这台终端上输入。
        AuthFactor(
            factor_id="terminal_a_pwd",
            capabilities={CAPABILITY_PASSWORD},
            device_id="terminal_a",
            recovery_channel_id="it-helpdesk",
            step_count=2,
        ),
        AuthFactor(
            factor_id="security_key",
            capabilities={CAPABILITY_FIDO2, CAPABILITY_HARDWARE_OTP},
            device_id="security_key_01",
            recovery_channel_id=None,
            step_count=1,
        ),
        # 终端 B：手机推送 + 手机 TOTP（同一物理设备的两种确认，可共存但
        # 不构成两项独立证明），手机与硬件令牌共用同一恢复渠道。
        AuthFactor(
            factor_id="phone_push",
            capabilities={CAPABILITY_MOBILE_PUSH, CAPABILITY_PASSWORD},
            device_id="phone_01",
            recovery_channel_id="sms-recovery",
            step_count=1,
        ),
        AuthFactor(
            factor_id="phone_totp",
            capabilities={CAPABILITY_TOTP},
            device_id="phone_01",
            recovery_channel_id="sms-recovery",
            step_count=3,
        ),
        AuthFactor(
            factor_id="hw_otp_token",
            capabilities={CAPABILITY_HARDWARE_OTP, CAPABILITY_FIDO2},
            device_id="hw_token_01",
            recovery_channel_id="sms-recovery",
            step_count=2,
        ),
    ]
    return AuthenticationPlanner(factors)


def main() -> None:
    planner = build_demo_planner()
    requirement = OperationRequirement(
        operation_id="large_payment",
        required_capabilities={CAPABILITY_PASSWORD, CAPABILITY_FIDO2},
        min_independent_proofs=2,
    )

    _print_result("初始规划（要求 password+fido2 且 2 个独立证明）", planner.plan(requirement))

    planner.disable_factor("security_key")
    _print_result("停用企业安全密钥后重新规划", planner.plan(requirement))

    planner.enable_factor("security_key")
    planner.disable_factor("phone_push")
    _print_result("停用手机推送后重新规划", planner.plan(requirement))

    # 全部启用因素最多只有 3 个相互独立来源（终端 A、安全密钥、手机/令牌组），
    # 要求 4 个独立证明必然被阻止，并列出每项能力的独立来源缺口。
    strict = OperationRequirement(
        operation_id="strict_payment",
        required_capabilities={CAPABILITY_PASSWORD, CAPABILITY_FIDO2, CAPABILITY_TOTP},
        min_independent_proofs=4,
    )
    _print_result("提高到 4 个独立证明（预期被阻止并列出缺失能力）", planner.plan(strict))


if __name__ == "__main__":
    main()
