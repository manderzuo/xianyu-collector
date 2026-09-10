"""账号密码配置的敏感数据回归测试。"""
import unittest

from backend.app.services.account_settings import (
    PASSWORD_PREFIX,
    _merge_platform_ai_settings,
    _decrypt_login_password,
    _encrypt_login_password,
)
from backend.app.services.builtin_keyword_service import load_builtin_keyword_rules


class AccountSettingsSecurityTests(unittest.TestCase):
    def test_login_password_round_trip_is_encrypted(self):
        ciphertext = _encrypt_login_password("correct horse battery staple")
        self.assertTrue(ciphertext.startswith(PASSWORD_PREFIX))
        self.assertNotIn("correct horse", ciphertext)
        self.assertEqual(_decrypt_login_password(ciphertext), "correct horse battery staple")

    def test_legacy_plaintext_remains_readable_for_migration(self):
        self.assertEqual(_decrypt_login_password("legacy-password"), "legacy-password")

    def test_builtin_ai_reply_switch_is_persisted_by_partial_merge(self):
        enabled = _merge_platform_ai_settings(
            {"ai_enabled": False, "builtin_ai_reply_enabled": False, "ai_settings": {}},
            {"builtin_ai_reply_enabled": True},
        )
        self.assertTrue(enabled["builtin_ai_reply_enabled"])

        provider_update = _merge_platform_ai_settings(
            enabled,
            {"ai_settings": {"model_name": "test-model"}},
        )
        self.assertTrue(provider_update["builtin_ai_reply_enabled"])
        self.assertEqual(provider_update["ai_settings"]["model_name"], "test-model")

    def test_builtin_keyword_pack_is_not_empty(self):
        rules = load_builtin_keyword_rules()
        self.assertGreater(len(rules), 0)
        self.assertTrue(any("能拍么" in rule.payload["keyword"] for rule in rules))


if __name__ == "__main__":
    unittest.main()
