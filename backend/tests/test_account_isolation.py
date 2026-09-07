"""账号租户范围回归测试。"""
import unittest

from sqlalchemy import select

from backend.app.api.routes.accounts import _account_scope
from common.models.accounts import Account


class AccountIsolationTests(unittest.TestCase):
    def test_admin_account_scope_is_still_limited_to_current_user(self):
        statement = _account_scope(
            select(Account),
            {"sub": "3", "role": "admin", "is_admin": True},
        )

        sql = str(statement)
        params = statement.compile().params
        self.assertIn("xr_accounts.user_id", sql)
        self.assertIn(3, params.values())


if __name__ == "__main__":
    unittest.main()
