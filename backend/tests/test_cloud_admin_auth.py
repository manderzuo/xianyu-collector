"""云端账号审批链路回归测试。"""
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from backend.app.api.routes.admin_users import _list_remote_users
from common.services.cloud_auth import CloudAuthError, cloud_auth_request


class CloudAdminAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_cloud_mode_never_falls_back_to_local_list_without_token(self):
        with patch.dict(os.environ, {"XIANYU_CLOUD_AUTH_URL": "https://auth.example"}):
            with self.assertRaises(HTTPException) as raised:
                await _list_remote_users({}, username=None, limit=20, offset=0)

        self.assertEqual(raised.exception.status_code, 401)
        self.assertIn("云端管理员会话已失效", str(raised.exception.detail))

    async def test_cloud_error_is_returned_instead_of_becoming_internal_error(self):
        error = CloudAuthError("invalid_invite", "邀请码无效", 400)
        with patch.dict(os.environ, {"XIANYU_CLOUD_AUTH_URL": "https://auth.example"}):
            with patch(
                "backend.app.api.routes.admin_users.cloud_auth_request",
                new=AsyncMock(side_effect=error),
            ):
                with self.assertRaises(HTTPException) as raised:
                    await _list_remote_users(
                        {"cloud_session_token": "token"},
                        username=None,
                        limit=20,
                        offset=0,
                    )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "邀请码无效")

    async def test_cloud_auth_rejects_http_without_explicit_development_override(self):
        with patch.dict(
            os.environ,
            {
                "XIANYU_CLOUD_AUTH_URL": "http://auth.example",
                "XIANYU_ALLOW_INSECURE_CLOUD_AUTH": "false",
            },
        ):
            with self.assertRaises(CloudAuthError) as raised:
                await cloud_auth_request("health", {})

        self.assertEqual(raised.exception.code, "insecure_configuration")
        self.assertEqual(raised.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
