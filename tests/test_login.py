"""登录模块验收测试（unittest）。

覆盖题面的交叉约束：

- 可接受结果：旧参数哈希登录成功并在一次验证后升级、升级后再次登录不再升级、
  升级中断后可重试、并发登录只升级一次、新实例可读取持久化记录。
- 应被阻止/拒绝：错误口令、停用账户（稳定拒绝）、验证期间改口令后旧口令无法
  建立会话且旧升级不覆盖新记录、CAS 版本不匹配拒绝写入。
- 边界/交错：验证中途改口令、升级落盘前中断、两线程并发登录、停用发生在
  验证之后、非法用户 ID。
"""

import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from login import (  # noqa: E402
    CURRENT_PARAM_VERSION,
    AccountDisabled,
    ConcurrentModification,
    CredentialModifiedDuringLogin,
    InvalidPassword,
    JsonFileCredentialStore,
    LoginService,
    LoginTracer,
    UpgradeInterrupted,
    UserNotFound,
)
from login.storage import CredentialRecord  # noqa: E402


class CountingTracer(LoginTracer):
    def __init__(self):
        self.before_upgrade_calls = 0
        self.committed_calls = 0
        self.skipped_calls = 0
        self.verified_calls = 0

    def password_verified(self, user_id, param_version):
        self.verified_calls += 1

    def before_upgrade_write(self, user_id, from_version, to_version):
        self.before_upgrade_calls += 1

    def after_upgrade_committed(self, user_id, new_record_version):
        self.committed_calls += 1

    def upgrade_skipped(self, user_id, reason):
        self.skipped_calls += 1


class FailingOnceTracer(LoginTracer):
    """第一次升级落盘前抛异常模拟中断，之后放行。"""

    def __init__(self):
        self.attempts = 0

    def before_upgrade_write(self, user_id, from_version, to_version):
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("模拟升级写入中断（磁盘故障/进程被杀）")


class ChangePasswordDuringVerifyTracer(LoginTracer):
    """口令验证通过后、升级临界区开始前，由另一线程改掉口令。"""

    def __init__(self, service, user_id, new_password):
        self.service = service
        self.user_id = user_id
        self.new_password = new_password
        self.fired = False

    def password_verified(self, user_id, param_version):
        if not self.fired:
            self.fired = True
            # 直接推进 record_version：模拟用户在验证期间修改了口令。
            self.service.change_password(self.user_id, self.new_password)


class LoginModuleTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = JsonFileCredentialStore(self.tmpdir.name)
        self.service = LoginService(self.store)

    def tearDown(self):
        self.tmpdir.cleanup()

    # ------------------------------------------------------------ 可接受结果

    def test_legacy_hash_login_succeeds_and_upgrades_once(self):
        """旧参数记录：成功验证后升级为当前参数并持久化算法/参数版本。"""
        tracer = CountingTracer()
        service = LoginService(self.store, tracer)
        record = service.register("alice", "old-secret", param_version="v1")
        self.assertEqual(record.param_version, "v1")
        self.assertEqual(record.algorithm, "pbkdf2_sha256")

        session = service.login("alice", "old-secret")

        self.assertEqual(session.user_id, "alice")
        self.assertEqual(session.param_version, CURRENT_PARAM_VERSION)
        self.assertEqual(session.record_version, 2)
        upgraded = self.store.get("alice")
        self.assertEqual(upgraded.param_version, CURRENT_PARAM_VERSION)
        self.assertEqual(upgraded.algorithm, "pbkdf2_sha256")
        self.assertEqual(upgraded.record_version, 2)
        self.assertNotEqual(upgraded.hash_hex, record.hash_hex)
        self.assertEqual(tracer.committed_calls, 1)
        self.assertEqual(tracer.before_upgrade_calls, 1)

        # 落盘 JSON 中确实持久化了算法与参数版本。
        with open(os.path.join(self.tmpdir.name, "alice.json"), encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertEqual(raw["param_version"], CURRENT_PARAM_VERSION)
        self.assertEqual(raw["algorithm"], "pbkdf2_sha256")
        self.assertEqual(raw["record_version"], 2)

        # 第二次登录：已是当前参数，不再产生升级写入。
        session2 = service.login("alice", "old-secret")
        self.assertEqual(session2.record_version, 2)
        self.assertEqual(tracer.committed_calls, 1)
        self.assertEqual(tracer.before_upgrade_calls, 1)
        self.assertEqual(tracer.skipped_calls, 1)

    def test_concurrent_logins_upgrade_exactly_once(self):
        """两个并发登录同一张旧记录：升级只落盘一次，两边都拿到会话。"""
        tracer = CountingTracer()
        service = LoginService(self.store, tracer)
        service.register("bob", "pw-123", param_version="v1")
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def worker():
            try:
                barrier.wait()
                results.append(service.login("bob", "pw-123"))
            except Exception as exc:  # noqa: BLE001 - 测试中记录全部失败
                errors.append(exc)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(s.param_version == CURRENT_PARAM_VERSION for s in results))
        self.assertEqual(tracer.committed_calls, 1, "并发登录只允许一次升级提交")
        self.assertEqual(tracer.before_upgrade_calls, 1)
        self.assertEqual(tracer.skipped_calls, 1)
        self.assertEqual(self.store.get("bob").record_version, 2)

    def test_upgrade_interruption_keeps_old_record_and_retry_succeeds(self):
        """升级中断：旧记录完好保留，重试时用有效记录完成升级。"""
        tracer = FailingOnceTracer()
        service = LoginService(self.store, tracer)
        service.register("carol", "retry-me", param_version="v1")
        before = self.store.get("carol")

        with self.assertRaises(UpgradeInterrupted):
            service.login("carol", "retry-me")

        # 中断后磁盘上仍是旧参数的有效记录，没有半成品文件。
        after_fail = self.store.get("carol")
        self.assertEqual(after_fail.param_version, "v1")
        self.assertEqual(after_fail.hash_hex, before.hash_hex)
        self.assertEqual(after_fail.record_version, 1)
        leftovers = [name for name in os.listdir(self.tmpdir.name) if name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

        # 用有效记录重试，升级成功并建立会话。
        session = service.login("carol", "retry-me")
        self.assertEqual(session.param_version, CURRENT_PARAM_VERSION)
        self.assertEqual(tracer.attempts, 2)
        self.assertEqual(self.store.get("carol").param_version, CURRENT_PARAM_VERSION)

    def test_persistence_survives_new_store_and_service_instance(self):
        """新实例从同一目录读取记录：升级结果与停用状态均持久化。"""
        self.service.register("dave", "persist-pw", param_version="v1")
        self.service.login("dave", "persist-pw")

        reborn_store = JsonFileCredentialStore(self.tmpdir.name)
        reborn_service = LoginService(reborn_store)
        record = reborn_store.get("dave")
        self.assertEqual(record.param_version, CURRENT_PARAM_VERSION)
        session = reborn_service.login("dave", "persist-pw")
        self.assertEqual(session.param_version, CURRENT_PARAM_VERSION)

    # ------------------------------------------------------------ 应拒绝路径

    def test_wrong_password_is_rejected_and_record_untouched(self):
        self.service.register("erin", "correct horse", param_version="v1")
        before = self.store.get("erin")

        with self.assertRaises(InvalidPassword):
            self.service.login("erin", "wrong horse")

        after = self.store.get("erin")
        self.assertEqual(after.param_version, "v1")
        self.assertEqual(after.hash_hex, before.hash_hex)
        self.assertEqual(after.record_version, 1)

    def test_disabled_account_is_stably_rejected(self):
        self.service.register("frank", "disabled-pw", param_version="v1", disabled=True)

        for _ in range(3):
            with self.assertRaises(AccountDisabled):
                self.service.login("frank", "disabled-pw")

        # 停用状态下即使口令正确也绝不升级、绝不推进版本。
        record = self.store.get("frank")
        self.assertTrue(record.disabled)
        self.assertEqual(record.param_version, "v1")
        self.assertEqual(record.record_version, 1)

        # 边界：先正常升级，再停用，停用后依旧稳定拒绝且记录不变。
        self.service.register("grace", "grace-pw", param_version="v1")
        self.service.login("grace", "grace-pw")
        self.service.set_disabled("grace", True)
        with self.assertRaises(AccountDisabled):
            self.service.login("grace", "grace-pw")
        self.assertTrue(self.store.get("grace").disabled)

    def test_password_changed_during_verify_old_password_rejected(self):
        """交错场景：验证通过后、升级前用户改口令，旧口令不得建立会话。"""
        tracer = ChangePasswordDuringVerifyTracer(self.service, "henry", "brand-new-pw")
        service = LoginService(self.store, tracer)
        service.register("henry", "legacy-pw", param_version="v1")
        old_record = self.store.get("henry")

        with self.assertRaises(CredentialModifiedDuringLogin):
            service.login("henry", "legacy-pw")

        # 新口令记录未被旧哈希升级覆盖：参数为 v2，版本已推进。
        current = self.store.get("henry")
        self.assertEqual(current.param_version, CURRENT_PARAM_VERSION)
        self.assertEqual(current.record_version, 2)
        self.assertNotEqual(current.hash_hex, old_record.hash_hex)

        # 旧口令仍然登不进来；新口令可以正常建立会话。
        with self.assertRaises(InvalidPassword):
            service.login("henry", "legacy-pw")
        session = service.login("henry", "brand-new-pw")
        self.assertEqual(session.user_id, "henry")
        self.assertEqual(session.record_version, 2)

    def test_disabled_during_verify_is_rejected(self):
        """边界：验证之后、升级临界区内被停用，本次登录仍被拒绝。"""

        class DisableAfterVerifyTracer(LoginTracer):
            def __init__(self, service):
                self.service = service

            def password_verified(self, user_id, param_version):
                # 验证已通过、升级临界区尚未进入时停用账户。
                self.service.set_disabled(user_id, True)

        service = LoginService(self.store, DisableAfterVerifyTracer(self.service))
        service.register("ivy", "ivy-pw", param_version="v1")

        with self.assertRaises(AccountDisabled):
            service.login("ivy", "ivy-pw")
        record = self.store.get("ivy")
        self.assertTrue(record.disabled)
        # 停用发生在升级前，旧参数记录不得被升级写入。
        self.assertEqual(record.param_version, "v1")
        self.assertEqual(record.record_version, 2)

    def test_cas_rejects_stale_upgrade_and_never_overwrites_new_record(self):
        """存储层交叉约束：版本不匹配的 CAS 必须拒绝，新记录保持不变。"""
        self.service.register("judy", "judy-pw", param_version="v1")
        stale = self.store.get("judy")
        # 用户先改成新口令（record_version 推进到 2）。
        new_record = self.service.change_password("judy", "judy-new-pw")
        self.assertEqual(new_record.record_version, 2)

        # 手里仍拿着旧版本（1）的升级尝试必须被 CAS 阻止。
        stale_upgrade = CredentialRecord(
            user_id="judy",
            algorithm=stale.algorithm,
            param_version=CURRENT_PARAM_VERSION,
            iterations=200_000,
            salt_hex=stale.salt_hex,
            hash_hex="deadbeef" * 8,
            disabled=False,
            record_version=stale.record_version,
            updated_at="stale-write",
        )
        with self.assertRaises(ConcurrentModification):
            self.store.compare_and_swap("judy", stale.record_version, stale_upgrade)

        current = self.store.get("judy")
        self.assertEqual(current.record_version, 2)
        self.assertEqual(current.hash_hex, new_record.hash_hex)
        self.assertNotEqual(current.hash_hex, stale_upgrade.hash_hex)

    # ------------------------------------------------------------------ 边界

    def test_unknown_user_and_unsafe_id_are_rejected(self):
        with self.assertRaises(UserNotFound):
            self.service.login("nobody", "whatever")
        with self.assertRaises(ValueError):
            self.store.get("../escape")

    def test_already_current_record_login_does_not_upgrade(self):
        tracer = CountingTracer()
        service = LoginService(self.store, tracer)
        service.register("karen", "karen-pw")  # 默认当前参数
        session = service.login("karen", "karen-pw")
        self.assertEqual(session.param_version, CURRENT_PARAM_VERSION)
        self.assertEqual(tracer.committed_calls, 0)
        self.assertEqual(tracer.before_upgrade_calls, 0)
        self.assertEqual(tracer.skipped_calls, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
