"""扫码 Cookie 和运行时自动续期的回归测试。"""
import unittest
from unittest.mock import AsyncMock

from common.models.accounts import Account
from common.services.cookie_renewal import (
    CookieRenewalService,
    is_session_expired_message,
    requires_browser_recovery_message,
)
from scheduler.app.jobs.account_renewal import _should_force_renewal
from websocket.app.xianyu_runtime import (
    AccountRuntime,
    RUNTIME_RENEWAL_COOLDOWN_SECONDS,
    _runtime_renewal_due,
)


class CookieSessionRecoveryTests(unittest.TestCase):
    def test_user_validate_enters_session_recovery(self):
        self.assertTrue(is_session_expired_message("FAIL_SYS_USER_VALIDATE::哎哟喂,被挤爆啦"))
        self.assertTrue(is_session_expired_message("FAIL_BIZ_WUA_IS_MACHINE"))
        self.assertTrue(requires_browser_recovery_message("FAIL_SYS_USER_VALIDATE"))
        self.assertFalse(requires_browser_recovery_message("FAIL_SYS_SESSION_EXPIRED"))

    def test_runtime_renewal_is_rate_limited(self):
        runtime = AccountRuntime(account_id="6", user_id=1, cookie_value="unb=123")
        self.assertTrue(_runtime_renewal_due(runtime, 1000.0))
        runtime.last_renewal_attempt_monotonic = 1000.0
        self.assertFalse(_runtime_renewal_due(runtime, 1001.0))
        self.assertTrue(
            _runtime_renewal_due(runtime, 1000.0 + RUNTIME_RENEWAL_COOLDOWN_SECONDS)
        )

    def test_disconnected_account_forces_renewal_despite_cached_expiry(self):
        account = Account(id=6, user_id=1, account_name="测试账号", status="active", cookie="unb=123")
        self.assertTrue(_should_force_renewal(account, force=False, connected_ids={"1", "4"}))
        self.assertFalse(_should_force_renewal(account, force=False, connected_ids={"6"}))
        # 连接服务不可用时不把所有账号误判为离线。
        self.assertFalse(_should_force_renewal(account, force=False, connected_ids=None))


class BrowserRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_validation_can_force_browser_after_api_success(self):
        service = CookieRenewalService()
        api_success = {
            "success": True,
            "new_cookie": "unb=123; cookie2=api",
            "updated_names": ["cookie2"],
            "message": "接口续期成功",
            "response_text": "{}",
            "steps": ["api"],
        }
        verify_success = {
            **api_success,
            "new_cookie": "unb=123; cookie2=browser",
            "steps": ["verify"],
        }
        service._api_renew_with_retry = AsyncMock(side_effect=[api_success, verify_success])
        service._browser_renew = AsyncMock(return_value={
            "success": True,
            "new_cookie": "unb=123; cookie2=browser",
            "message": "浏览器续期成功",
            "steps": ["browser"],
        })

        result = await service.renew(
            "unb=123; cookie2=old",
            "6",
            force_browser=True,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.method, "browser+api")
        service._browser_renew.assert_awaited_once()

    async def test_api_success_does_not_open_browser_without_force(self):
        service = CookieRenewalService()
        service._api_renew_with_retry = AsyncMock(return_value={
            "success": True,
            "new_cookie": "unb=123; cookie2=api",
            "updated_names": ["cookie2"],
            "message": "接口续期成功",
            "response_text": "{}",
            "steps": ["api"],
        })
        service._browser_renew = AsyncMock()

        result = await service.renew("unb=123; cookie2=old", "6")

        self.assertTrue(result.success)
        self.assertEqual(result.method, "api")
        service._browser_renew.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
