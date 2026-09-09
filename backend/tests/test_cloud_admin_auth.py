"""云端账号审批链路回归测试。"""
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from fastapi import HTTPException

from backend.app.api.routes.admin_users import _list_remote_users, _serialize_remote_user
from common.services.cloud_auth import CloudAuthError, cloud_auth_request


class _FakeAsyncClient:
    def __init__(self, outcomes):
        self.outcomes = outcomes

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, endpoint, json, headers):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class CloudAdminAuthTests(unittest.IsolatedAsyncioTestCase):
    def test_remote_user_keeps_central_plan_fields(self):
        mapped = _serialize_remote_user({
            "id": 7,
            "username": "cloud-user",
            "role": "employee",
            "status": "approved",
            "plan_code": "vip",
            "plan_expires_at": "2030-01-02T03:04:05",
        })

        self.assertTrue(mapped["cloud_mode"])
        self.assertEqual(mapped["plan_code"], "VIP")
        self.assertEqual(mapped["plan_expires_at"], "2030-01-02T03:04:05")
        self.assertEqual(mapped["expire_at"], "2030-01-02T03:04:05")

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

    async def test_cloud_auth_retries_transient_tls_failure_before_returning_success(self):
        outcomes = [
            httpx.ConnectError("tls handshake failure"),
            httpx.ConnectError("proxy is still connecting"),
            httpx.Response(200, json={"ok": True, "service": "auth"}),
        ]

        def make_client(**_kwargs):
            return _FakeAsyncClient(outcomes)

        with patch.dict(
            os.environ,
            {
                "XIANYU_CLOUD_AUTH_URL": "https://auth.example",
                "XIANYU_CLOUD_AUTH_MAX_ATTEMPTS": "3",
                "XIANYU_CLOUD_AUTH_RETRY_BACKOFF": "0.5",
            },
        ):
            with patch("common.services.cloud_auth.httpx.AsyncClient", side_effect=make_client):
                with patch("common.services.cloud_auth.asyncio.sleep", new=AsyncMock()) as sleep:
                    result = await cloud_auth_request("health", {})

        self.assertEqual(result["ok"], True)
        self.assertEqual(len(outcomes), 0)
        self.assertEqual(sleep.await_count, 2)


if __name__ == "__main__":
    unittest.main()
