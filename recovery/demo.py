"""恢复会话模块的可运行演示入口。

运行方式（在工作区根目录）：

    python3 -m recovery.demo

演示使用可控时钟 :class:`ManualClock`，不需要真实等待冷静期，覆盖三条
主线：完整恢复流程、撤回作废、客户端超时后同请求编号重试取回原决定。
"""

from __future__ import annotations

from recovery import (
    EventType,
    Factor,
    FactorRegistry,
    ManualClock,
    RecoveryManager,
    SessionStatus,
)


def _print_events(session) -> None:
    print("  事件流：")
    for event in session.events:
        detail = f" {event.detail}" if event.detail else ""
        print(f"    {event.seq}. {event.event_type.value}@{event.timestamp}{detail}")


def demo_happy_path(
    manager: RecoveryManager, clock: ManualClock, registry: FactorRegistry
) -> None:
    print("== 1. 完整恢复流程 ==")
    session = manager.create_application(
        "user-1",
        "factor-totp",
        "ONE-TIME-RECOVERY-CODE",
        cooldown_seconds=3600,
        validity_seconds=86400,
    )
    print(f"  会话 {session.session_id[:8]} 创建，状态：{session.status.value}")

    session.confirm_old_device()
    print(f"  旧设备确认完成，状态：{session.status.value}")

    clock.advance(1800)
    early = session.submit_recovery(
        "req-early", "ONE-TIME-RECOVERY-CODE",
        Factor("factor-totp", "webauthn", "新通行密钥"),
    )
    print(f"  冷静期内提交 -> accepted={early.accepted} reason={early.reason}")

    clock.advance(1800)
    decision = session.submit_recovery(
        "req-final", "ONE-TIME-RECOVERY-CODE",
        Factor("factor-totp", "webauthn", "新通行密钥"),
    )
    print(f"  冷静期结束后提交 -> accepted={decision.accepted}")
    print(f"  最终状态：{session.status.value}")
    enabled = registry.enabled_factor_ids("user-1")
    print(f"  当前可用因素：{enabled}")
    _print_events(session)
    print()


def demo_withdrawal(manager: RecoveryManager, clock: ManualClock) -> None:
    print("== 2. 重新登记前撤回，会话立即作废 ==")
    session = manager.create_application(
        "user-2", "factor-totp", "CODE-2",
        cooldown_seconds=10, validity_seconds=100,
    )
    session.confirm_old_device()
    clock.advance(10)
    session.withdraw(reason="user_changed_mind")
    decision = session.submit_recovery(
        "req-w", "CODE-2",
        Factor("factor-totp", "webauthn", "新通行密钥"),
    )
    print(f"  撤回后提交 -> accepted={decision.accepted} reason={decision.reason}")
    print(f"  最终状态：{session.status.value}（原有因素未改动）")
    print()


def demo_idempotent_retry(manager: RecoveryManager, clock: ManualClock) -> None:
    print("== 3. 客户端超时重试，取回原决定 ==")
    session = manager.create_application(
        "user-3", "factor-totp", "CODE-3",
        cooldown_seconds=10, validity_seconds=100,
    )
    session.confirm_old_device()
    first = session.submit_recovery(
        "req-retry", "CODE-3",
        Factor("factor-totp", "webauthn", "新通行密钥"),
    )
    print(f"  第一次提交（冷静期内）-> accepted={first.accepted} "
          f"reason={first.reason}")

    clock.advance(20)
    retry = session.submit_recovery(
        "req-retry", "CODE-3",
        Factor("factor-totp", "webauthn", "新通行密钥"),
    )
    same = first is retry
    print(f"  同请求编号重试 -> accepted={retry.accepted} "
          f"reason={retry.reason}，与原决定同对象：{same}")
    print()


def main() -> None:
    clock = ManualClock()
    registry = FactorRegistry()
    for user in ("user-1", "user-2", "user-3"):
        registry.add_factor(
            user, Factor("factor-totp", "totp", "旧验证器", secret="old")
        )
        registry.add_factor(
            user, Factor("factor-sms", "sms", "短信因素", secret="+10000")
        )
    manager = RecoveryManager(registry, clock=clock)

    demo_happy_path(manager, clock, registry)
    demo_withdrawal(manager, clock)
    demo_idempotent_retry(manager, clock)

    print("演示结束。会话状态汇总：")
    for session in manager.list_sessions():
        print(f"  - {session.session_id[:8]} user={session.user_id} "
              f"status={session.status.value}")


if __name__ == "__main__":
    main()
