"""账号密码配置的敏感数据回归测试。"""
import unittest

from backend.app.services.account_settings import (
    PASSWORD_PREFIX,
    _decrypt_login_password,
    _encrypt_login_password,
)


class AccountSettingsSecurityTests(unittest.TestCase):
    def test_login_password_round_trip_is_encrypted(self):
        ciphertext = _encrypt_login_password("correct horse battery staple")
        self.assertTrue(ciphertext.startswith(PASSWORD_PREFIX))
        self.assertNotIn("correct horse", ciphertext)
        self.assertEqual(_decrypt_login_password(ciphertext), "correct horse battery staple")

    def test_legacy_plaintext_remains_readable_for_migration(self):
        self.assertEqual(_decrypt_login_password("legacy-password"), "legacy-password")


if __name__ == "__main__":
    unittest.main()
