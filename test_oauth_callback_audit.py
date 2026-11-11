"""oauth_callback_audit 的验证测试。

覆盖:
  * 四类报告: 精确匹配 / 回调仅在其他环境注册 / 匹配条件过宽 / 缺少注册证据
  * 多个合法回调可共存
  * 同名客户端在不同颁发方下独立判定
  * 开发例外到期后触发告警
  * 配置补齐后旧告警消失
"""

import unittest
from datetime import date

from oauth_callback_audit import (
    Category, Registration, CallbackConfig, ExceptionRecord,
    audit, render_report,
)

TODAY = date(2026, 11, 12)


def reg(issuer, client, env, uri, src):
    return Registration(issuer=issuer, client_id=client, environment=env,
                        redirect_uri=uri, source=src)


def cb(issuer, client, env, uri, match="exact", src="manifest:1"):
    return CallbackConfig(issuer=issuer, client_id=client, environment=env,
                          callback_uri=uri, match_type=match, source=src)


def exc(issuer, client, env, expires, src):
    return ExceptionRecord(issuer=issuer, client_id=client, environment=env,
                           expires_at=expires, source=src)


def categories(findings):
    return {f.category for f in findings}


class TestReportCategories(unittest.TestCase):
    """四类报告各自产生。"""

    def test_exact_match(self):
        fs = audit(
            [reg("auth0", "web-app", "prod", "https://app.example.com/cb", "reg/prod.json:3")],
            [cb("auth0", "web-app", "prod", "https://app.example.com/cb", src="manifest/prod.yaml:10")],
            [], TODAY)
        self.assertEqual(categories(fs), {Category.EXACT_MATCH})
        self.assertFalse(any(f.is_alert for f in fs))

    def test_other_env_only(self):
        fs = audit(
            [reg("auth0", "web-app", "dev", "https://dev.example.com/cb", "reg/dev.json:7")],
            [cb("auth0", "web-app", "prod", "https://dev.example.com/cb", src="manifest/prod.yaml:11")],
            [], TODAY)
        self.assertEqual(categories(fs), {Category.OTHER_ENV_ONLY})
        f = fs[0]
        self.assertTrue(f.is_alert)
        # 报告须给出实际绑定键、注册项与配置来源行
        self.assertEqual(f.binding_key, ("auth0", "web-app", "prod"))
        self.assertEqual(f.registrations[0].source, "reg/dev.json:7")
        self.assertEqual(f.config_source, "manifest/prod.yaml:11")

    def test_broad_match(self):
        fs = audit(
            [reg("auth0", "web-app", "prod", "https://app.example.com/cb", "reg/prod.json:3")],
            [cb("auth0", "web-app", "prod", "https://app.example.com/", match="prefix", src="manifest/prod.yaml:12")],
            [], TODAY)
        self.assertEqual(categories(fs), {Category.BROAD_MATCH})
        self.assertTrue(fs[0].is_alert)

    def test_no_registration(self):
        fs = audit(
            [],
            [cb("auth0", "web-app", "prod", "https://rogue.example.com/cb", src="manifest/prod.yaml:13")],
            [], TODAY)
        self.assertEqual(categories(fs), {Category.NO_REGISTRATION})
        self.assertTrue(fs[0].is_alert)


class TestCoexistenceAndIsolation(unittest.TestCase):
    """多个合法回调共存; 同名客户端跨颁发方独立判定。"""

    def test_multiple_legitimate_callbacks_coexist(self):
        regs = [
            reg("auth0", "web-app", "prod", "https://app.example.com/cb", "reg/prod.json:3"),
            reg("auth0", "web-app", "prod", "https://app.example.com/cb2", "reg/prod.json:4"),
            reg("auth0", "web-app", "prod", "https://admin.example.com/oauth", "reg/prod.json:5"),
        ]
        cbs = [
            cb("auth0", "web-app", "prod", "https://app.example.com/cb", src="m:1"),
            cb("auth0", "web-app", "prod", "https://app.example.com/cb2", src="m:2"),
            cb("auth0", "web-app", "prod", "https://admin.example.com/oauth", src="m:3"),
        ]
        fs = audit(regs, cbs, [], TODAY)
        self.assertEqual(len(fs), 3)
        self.assertEqual(categories(fs), {Category.EXACT_MATCH})
        self.assertFalse(any(f.is_alert for f in fs))

    def test_same_client_name_different_issuers_independent(self):
        # issuer-A 注册了 URI, issuer-B 的同名客户端未注册 -> B 独立判为缺少注册证据
        regs = [reg("issuer-A", "web-app", "prod", "https://app.example.com/cb", "regA.json:3")]
        cbs = [
            cb("issuer-A", "web-app", "prod", "https://app.example.com/cb", src="mA:1"),
            cb("issuer-B", "web-app", "prod", "https://app.example.com/cb", src="mB:1"),
        ]
        fs = audit(regs, cbs, [], TODAY)
        by_key = {f.binding_key: f for f in fs}
        self.assertEqual(by_key[("issuer-A", "web-app", "prod")].category, Category.EXACT_MATCH)
        self.assertEqual(by_key[("issuer-B", "web-app", "prod")].category, Category.NO_REGISTRATION)


class TestExceptions(unittest.TestCase):
    """开发例外: 未到期豁免, 到期告警, 补齐后告警消失。"""

    DEV_URI = "https://dev.example.com/cb"

    def _dev_cb(self):
        return cb("auth0", "web-app", "dev", self.DEV_URI, src="manifest/dev.yaml:5")

    def test_unexpired_exception_covers_dev(self):
        fs = audit([], [self._dev_cb()],
                   [exc("auth0", "web-app", "dev", date(2027, 1, 1), "exceptions.yaml:2")],
                   TODAY)
        self.assertEqual(categories(fs), {Category.EXCEPTION_COVERED})
        self.assertFalse(any(f.is_alert for f in fs))

    def test_expired_exception_triggers_alert(self):
        fs = audit([], [self._dev_cb()],
                   [exc("auth0", "web-app", "dev", date(2026, 1, 1), "exceptions.yaml:2")],
                   TODAY)
        cats = categories(fs)
        self.assertIn(Category.EXCEPTION_EXPIRED, cats)
        self.assertIn(Category.NO_REGISTRATION, cats)  # 豁免失效, 回调本身也告警
        self.assertTrue(any(f.is_alert for f in fs))

    def test_exception_bound_to_specific_client_and_env(self):
        # 例外绑定 dev 环境, 不能豁免 staging; 绑定 client-A, 不能豁免 client-B
        exceptions = [exc("auth0", "client-A", "dev", date(2027, 1, 1), "exceptions.yaml:2")]
        cbs = [
            cb("auth0", "client-A", "staging", self.DEV_URI, src="m:1"),
            cb("auth0", "client-B", "dev", self.DEV_URI, src="m:2"),
        ]
        fs = audit([], cbs, exceptions, TODAY)
        self.assertEqual(categories(fs), {Category.NO_REGISTRATION})
        self.assertTrue(all(f.is_alert for f in fs))

    def test_alert_disappears_after_registration_added(self):
        exception = [exc("auth0", "web-app", "dev", date(2026, 1, 1), "exceptions.yaml:2")]
        before = audit([], [self._dev_cb()], exception, TODAY)
        self.assertTrue(any(f.is_alert for f in before))

        # 配置补齐: 注册快照中新增该 URI
        after = audit(
            [reg("auth0", "web-app", "dev", self.DEV_URI, "reg/dev.json:9")],
            [self._dev_cb()], exception, TODAY)
        self.assertEqual(categories(after), {Category.EXACT_MATCH, Category.EXCEPTION_EXPIRED})
        # 回调本身的告警消失; 到期例外记录仍如实报告, 但不再影响发布门禁判定中该回调
        cb_findings = [f for f in after if f.callback_uri == self.DEV_URI]
        self.assertFalse(any(f.is_alert for f in cb_findings))

    def test_exception_without_expiry_is_invalid(self):
        fs = audit([], [self._dev_cb()],
                   [exc("auth0", "web-app", "dev", None, "exceptions.yaml:3")],
                   TODAY)
        cats = categories(fs)
        self.assertIn(Category.EXCEPTION_INVALID, cats)
        self.assertIn(Category.NO_REGISTRATION, cats)


class TestReportRendering(unittest.TestCase):
    def test_report_contains_key_fields(self):
        fs = audit(
            [reg("auth0", "web-app", "dev", "https://dev.example.com/cb", "reg/dev.json:7")],
            [cb("auth0", "web-app", "prod", "https://dev.example.com/cb", src="manifest/prod.yaml:11")],
            [], TODAY)
        report = render_report(fs)
        self.assertIn("回调仅在其他环境注册", report)
        self.assertIn("issuer=auth0 client=web-app env=prod", report)
        self.assertIn("manifest/prod.yaml:11", report)
        self.assertIn("reg/dev.json:7", report)
        self.assertIn("阻断", report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
