"""Cloud-login handoff regression tests for the P0 authentication fix."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from backend.app.api.routes.auth import _authenticate_cloud_user, refresh
from backend.app.api.routes.auth import login
from common.schemas.api import LoginRequest
from common.services.cloud_auth import CloudAuthError


class _FakeResult:
    def __init__(self, record):
        self.record = record

    def scalar_one_or_none(self):
        return self.record


class _FakeSession:
    def __init__(self, record):
        self.record = record

    async def execute(self, _query):
        return _FakeResult(self.record)

    async def commit(self):
        return None

    async def refresh(self, _record):
        return None


class CloudAuthLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_embeds_cloud_session_for_follow_up_verify(self):
        record = SimpleNamespace(
            id=7,
            username="admin",
            password_hash="old-hash",
            nickname="系统管理员",
            role="admin",
            status=1,
            plan_code="NORMAL",
            plan_expires_at=None,
            auth_version=1,
            account_limit=None,
        )
        session = _FakeSession(record)
        remote_response = {
            "user": {"username": "admin", "role": "admin", "plan_code": "VIP"},
            "session_token": "cloud-session-token",
        }
        with patch(
            "backend.app.api.routes.auth.ensure_admin",
            new=AsyncMock(return_value=record),
        ), patch(
            "backend.app.api.routes.auth.cloud_auth_url",
            return_value="https://auth.example",
        ), patch(
            "backend.app.api.routes.auth.cloud_auth_request",
            new=AsyncMock(return_value=remote_response),
        ), patch(
            "backend.app.api.routes.auth.entitlement_payload",
            new=AsyncMock(return_value={}),
        ), patch(
            "backend.app.api.routes.auth.create_access_token",
            return_value=("access-token", 900),
        ) as create_access, patch(
            "backend.app.api.routes.auth.create_refresh_token",
            return_value=("refresh-token", 86400),
        ):
            response = await login(LoginRequest(username="admin", password="admin123"), session)

        claims = create_access.call_args.args[0]
        self.assertEqual(claims["cloud_session_token"], "cloud-session-token")
        self.assertEqual(response["data"]["access_token"], "access-token")
        self.assertEqual(response["data"]["user"]["plan_code"], "VIP")

    async def test_cloud_login_requires_and_returns_session_token(self):
        response = {
            "user": {"username": "admin", "role": "admin", "plan_code": "VIP"},
            "session_token": "cloud-session-token",
        }
        with patch(
            "backend.app.api.routes.auth.cloud_auth_request",
            new=AsyncMock(return_value=response),
        ) as request:
            remote_user, token = await _authenticate_cloud_user("admin", "admin123")

        request.assert_awaited_once_with(
            "login",
            {"username": "admin", "password": "admin123"},
        )
        self.assertEqual(remote_user["username"], "admin")
        self.assertEqual(token, "cloud-session-token")

    async def test_cloud_login_rejects_response_without_session_token(self):
        with patch(
            "backend.app.api.routes.auth.cloud_auth_request",
            new=AsyncMock(return_value={"user": {"username": "admin"}}),
        ):
            with self.assertRaises(HTTPException) as raised:
                await _authenticate_cloud_user("admin", "admin123")

        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("缺少有效会话", str(raised.exception.detail))

    async def test_cloud_login_maps_remote_errors_without_swallowing_status(self):
        error = CloudAuthError("invalid_credentials", "用户名或密码错误", 401)
        with patch(
            "backend.app.api.routes.auth.cloud_auth_request",
            new=AsyncMock(side_effect=error),
        ):
            with self.assertRaises(HTTPException) as raised:
                await _authenticate_cloud_user("admin", "wrong")

        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.detail, "用户名或密码错误")

    async def test_refresh_preserves_cloud_session_for_verify(self):
        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer",
            credentials="refresh-token",
        )
        claims = {
            "sub": "7",
            "username": "admin",
            "role": "admin",
            "cloud_session_token": "cloud-session-token",
        }
        with patch(
            "backend.app.api.routes.auth.decode_refresh_token",
            return_value=claims,
        ), patch(
            "backend.app.api.routes.auth.create_access_token",
            return_value=("access-token", 900),
        ) as create_access, patch(
            "backend.app.api.routes.auth.create_refresh_token",
            return_value=("new-refresh-token", 86400),
        ) as create_refresh:
            result = await refresh(credentials)

        expected = {
            "sub": "7",
            "username": "admin",
            "role": "admin",
            "cloud_session_token": "cloud-session-token",
        }
        create_access.assert_called_once_with(expected)
        create_refresh.assert_called_once_with(expected)
        self.assertEqual(result["data"]["access_token"], "access-token")


if __name__ == "__main__":
    unittest.main()
