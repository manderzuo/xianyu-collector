# -*- coding: utf-8 -*-
"""内部令牌、错误透传、人脸核验时序与云端改密码的回归测试。

覆盖本轮两个线上问题的根因：

1. **添加账号报「连接服务拒绝请求」**：``qr_login.py`` 调连接服务的
   ``/internal/`` 接口时漏发 ``X-Internal-Token``，被中间件 401 拒绝；
   且错误映射只读 ``message`` 不读 ``detail``，把真实原因吞掉换成含糊文案。
2. **管理员改密码无法同步云端**：``change_password`` 只写本机，
   而 1.3.5 起登录的密码权威是云端，导致新密码登不上。
"""
from __future__ import annotations

import inspect
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from common.models import User
from backend.app.api.routes.qr_login import (
    _notify_account_runtime,
    _runtime_failure_message,
)
from backend.app.services.qr_login import QRLoginSession

_CLOUD_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "deploy", "cloud_auth",
)
if _CLOUD_DIR not in sys.path:
    sys.path.insert(0, _CLOUD_DIR)

from auth_store import AuthError, AuthStore  # noqa: E402


# --------------------------------------------------------------------------- #
# 问题 2：内部令牌与错误透传
# --------------------------------------------------------------------------- #

class RuntimeFailureMessageTests(unittest.TestCase):
    """错误映射必须能读出 FastAPI 的 ``detail``。"""

    def test_reads_detail_field(self):
        # 连接服务中间件返回的就是这种形状：只有 detail，没有 message
        self.assertEqual(
            _runtime_failure_message({"detail": "内部调用凭证无效"}, "连接服务拒绝请求"),
            "内部调用凭证无效",
        )

    def test_reads_message_field(self):
        self.assertEqual(_runtime_failure_message({"message": "账号繁忙"}, "兜底"), "账号繁忙")

    def test_reads_nested_detail(self):
        self.assertEqual(
            _runtime_failure_message({"detail": {"message": "嵌套原因"}}, "兜底"),
            "嵌套原因",
        )

    def test_falls_back_when_payload_unusable(self):
        for payload in (None, [], "string", {}, {"detail": "   "}):
            self.assertEqual(_runtime_failure_message(payload, "兜底"), "兜底", repr(payload))

    def test_truncates_long_message(self):
        result = _runtime_failure_message({"detail": "x" * 900}, "兜底")
        self.assertEqual(len(result), 500)


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeClient:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._response

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._response


class NotifyRuntimeTests(unittest.IsolatedAsyncioTestCase):
    """通知连接服务必须带内部令牌，否则一定被 401 拒绝。"""

    def _patch(self, client):
        return patch(
            "backend.app.api.routes.qr_login.httpx.AsyncClient", return_value=client
        )

    async def test_notify_sends_internal_token(self):
        from common.config import settings

        client = _FakeClient(_FakeResponse({"success": True, "data": {"status": "pending"}}))
        account = SimpleNamespace(id=7)
        with self._patch(client):
            result = await _notify_account_runtime(account, "unb=1", 1, True)

        self.assertEqual(result["status"], "pending")
        method, url, kwargs = client.calls[0]
        self.assertEqual(method, "POST")
        self.assertIn("/internal/accounts/7/start", url)
        headers = kwargs.get("headers") or {}
        self.assertEqual(
            headers.get("X-Internal-Token"),
            settings.jwt_secret,
            "缺少内部令牌会被连接服务 401 拒绝，正是线上故障根因",
        )

    async def test_status_query_sends_internal_token(self):
        from common.config import settings
        from backend.app.api.routes.qr_login import _get_runtime_status

        client = _FakeClient(_FakeResponse({"success": True, "data": {"connection_state": "connected"}}))
        with self._patch(client):
            result = await _get_runtime_status(7)

        self.assertEqual(result["status"], "ok")
        method, url, kwargs = client.calls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/internal/accounts/7/status"))
        self.assertEqual((kwargs.get("headers") or {}).get("X-Internal-Token"), settings.jwt_secret)

    async def test_401_surfaces_real_reason_not_generic_text(self):
        """回归保护：401 必须显示真实原因，不能再是「连接服务拒绝请求」。"""
        client = _FakeClient(_FakeResponse({"detail": "内部调用凭证无效"}, status_code=401))
        with self._patch(client):
            result = await _notify_account_runtime(SimpleNamespace(id=7), "unb=1", 1, True)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["detail"], "内部调用凭证无效")
        self.assertNotEqual(result["detail"], "连接服务拒绝请求")

    async def test_http_error_is_reported_as_unavailable(self):
        class _FailingClient(_FakeClient):
            async def post(self, url, **kwargs):
                raise httpx.ConnectError("boom")

        with self._patch(_FailingClient(None)):
            result = await _notify_account_runtime(SimpleNamespace(id=7), "unb=1", 1, False)

        self.assertEqual(result["status"], "unavailable")

    async def test_non_json_response_is_tolerated(self):
        client = _FakeClient(_FakeResponse(None, status_code=502))
        with self._patch(client):
            result = await _notify_account_runtime(SimpleNamespace(id=7), "unb=1", 1, False)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["http_status"], 502)

    def test_all_internal_calls_in_qr_login_carry_token(self):
        """结构性保护：qr_login 中每处连接服务请求都必须带令牌。

        逐段检查每个 ``websocket_service_url`` 请求段里是否出现
        ``X-Internal-Token``——上一版按 ``/internal/`` 字面量计数，会被
        注释里的写法干扰。
        """
        import backend.app.api.routes.qr_login as module

        source = inspect.getsource(module)
        segments = source.split("websocket_service_url")[1:]
        self.assertGreaterEqual(len(segments), 2, "应存在通知与状态查询两处请求")
        for index, segment in enumerate(segments):
            # 只看该请求附近的一段，覆盖到 headers 传递
            window = segment[:600]
            self.assertIn(
                "X-Internal-Token", window,
                f"第 {index + 1} 处连接服务请求漏发内部令牌，会被 401 拒绝",
            )


# --------------------------------------------------------------------------- #
# 人脸核验时序
# --------------------------------------------------------------------------- #

class FaceVerificationTimingTests(unittest.TestCase):
    """人脸二维码必须在状态对外可见之前就绪。"""

    def test_qr_is_set_before_verification_required(self):
        from backend.app.services.qr_login import QRLoginManager

        source = inspect.getsource(QRLoginManager._run_face_verification)
        qr_index = source.index("session.face_qr_url = self._render_qr")
        status_index = source.index('session.status = "verification_required"')
        self.assertLess(
            qr_index, status_index,
            "必须先设置二维码再置状态，否则前端会读到“需要验证”却拿不到图",
        )

    def test_monitor_uses_intermediate_state(self):
        from backend.app.services.qr_login import QRLoginManager

        source = inspect.getsource(QRLoginManager._monitor)
        self.assertIn('session.status = "face_verifying"', source)
        # 中间态必须早于人脸抓取，避免窗口期内暴露 verification_required
        self.assertLess(
            source.index('session.status = "face_verifying"'),
            source.index("await self._run_face_verification"),
        )

    def test_monitor_failure_lands_on_failed(self):
        """人脸分支异常必须落到 failed，不能永久停在中间态。"""
        from backend.app.services.qr_login import QRLoginManager

        source = inspect.getsource(QRLoginManager._monitor)
        self.assertIn("人脸验证失败：", source)
        self.assertIn('session.status = "failed"', source)

    def test_status_reports_qr_whenever_available(self):
        from backend.app.services.qr_login import QRLoginManager

        manager = QRLoginManager()
        session = QRLoginSession(session_id="s1", status="face_verifying")
        session.face_qr_url = "data:image/png;base64,AAAA"
        session.verification_url = "https://example.com/verify"
        manager.sessions["s1"] = session

        data = manager.status("s1")
        # 即使状态已推进，只要图还在就应回传，避免轮询间隔错过
        self.assertEqual(data["face_qr_url"], "data:image/png;base64,AAAA")
        self.assertEqual(data["verification_url"], "https://example.com/verify")
        self.assertIn("正在获取人脸验证二维码", data.get("message", ""))

    def test_verification_required_message(self):
        from backend.app.services.qr_login import QRLoginManager

        manager = QRLoginManager()
        session = QRLoginSession(session_id="s2", status="verification_required")
        session.face_qr_url = "data:image/png;base64,BBBB"
        manager.sessions["s2"] = session

        data = manager.status("s2")
        self.assertEqual(data["status"], "verification_required")
        self.assertIn("人脸验证", data.get("message", ""))


# --------------------------------------------------------------------------- #
# 问题 1：云端改密码
# --------------------------------------------------------------------------- #

class CloudChangePasswordTests(unittest.TestCase):
    """云端自助改密码：必须校验旧密码，并作废旧会话。"""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        os.environ.setdefault("XIANYU_BOOTSTRAP_PASSWORD", "bootstrap-pass-1")
        self.store = AuthStore(os.path.join(self._dir.name, "auth.db"))

    def _make_user(self, username="alice", password="secret123"):
        self.store.sync_invites([{"code": "CLOUDINVITE01", "status": "active"}])
        user = self.store.register(username, password, "Alice", "CLOUDINVITE01")
        self.store.approve(user["id"])
        return user

    def test_change_password_with_correct_old_password(self):
        user = self._make_user()
        self.store.change_password(user["id"], "secret123", "newsecret1")
        # 新密码可登录
        self.assertEqual(self.store.authenticate("alice", "newsecret1")["username"], "alice")
        # 旧密码失效
        with self.assertRaises(AuthError):
            self.store.authenticate("alice", "secret123")

    def test_wrong_old_password_is_rejected(self):
        user = self._make_user()
        with self.assertRaises(AuthError) as ctx:
            self.store.change_password(user["id"], "wrong-old", "newsecret1")
        self.assertEqual(ctx.exception.code, "invalid_credentials")
        # 原密码仍然有效，未被改动
        self.store.authenticate("alice", "secret123")

    def test_new_password_must_differ(self):
        user = self._make_user()
        with self.assertRaises(AuthError) as ctx:
            self.store.change_password(user["id"], "secret123", "secret123")
        self.assertEqual(ctx.exception.code, "invalid_input")

    def test_short_new_password_is_rejected(self):
        user = self._make_user()
        with self.assertRaises(AuthError):
            self.store.change_password(user["id"], "secret123", "123")

    def test_change_password_revokes_existing_sessions(self):
        """改密码后旧会话必须失效，否则改密码没有意义。"""
        user = self._make_user()
        token = self.store.create_session(user["id"])
        self.assertIsNotNone(self.store.get_session_user(token))

        self.store.change_password(user["id"], "secret123", "newsecret1")
        revoked = self.store.revoke_user_sessions(user["id"])

        self.assertGreaterEqual(revoked, 1)
        self.assertIsNone(self.store.get_session_user(token), "旧会话应已失效")

    def test_unknown_user_raises(self):
        with self.assertRaises(AuthError) as ctx:
            self.store.change_password(99999, "a", "newsecret1")
        self.assertEqual(ctx.exception.code, "not_found")

    def test_reset_password_still_works_for_admin(self):
        """管理员重置路径不受影响（不校验旧密码）。"""
        user = self._make_user()
        self.store.reset_password(user["id"], "adminreset1")
        self.assertEqual(self.store.authenticate("alice", "adminreset1")["username"], "alice")


class CloudServerActionTests(unittest.TestCase):
    """云端必须真的暴露 change_password 动作。"""

    def test_server_exposes_change_password_action(self):
        server_path = os.path.join(_CLOUD_DIR, "server.py")
        with open(server_path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("action == 'change_password'", source)
        self.assertIn("store.change_password(", source)
        self.assertIn("revoke_user_sessions", source)

    def test_action_requires_session(self):
        """自助改密码必须放在会话校验之后，不能像 register 一样免鉴权。"""
        server_path = os.path.join(_CLOUD_DIR, "server.py")
        with open(server_path, encoding="utf-8") as handle:
            source = handle.read()
        session_check = source.index("user = store.get_session_user(token)")
        change_action = source.index("action == 'change_password'")
        self.assertLess(
            session_check, change_action,
            "change_password 必须在会话校验之后，否则任何人都能改密码",
        )


class BackendChangePasswordForwardTests(unittest.IsolatedAsyncioTestCase):
    """后端在云端模式下必须把改密码转发到云端。"""

    def _user(self):
        return {"sub": "1", "username": "admin", "role": "admin", "cloud_session_token": "cloud-token"}

    async def test_cloud_mode_forwards_and_updates_mirror(self):
        from backend.app.api.routes import auth as auth_module

        record = User(id=1, username="admin", role="admin", status=1, password_hash="pbkdf2$old$deadbeef")
        session = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: record)),
            commit=AsyncMock(),
        )
        forwarded = {}

        async def fake_cloud(action, payload, token=""):
            forwarded["action"] = action
            forwarded["payload"] = payload
            forwarded["token"] = token
            return {"ok": True}

        request = SimpleNamespace(old_password="admin123", new_password="newpass123")
        with patch.object(auth_module, "cloud_auth_url", return_value="https://auth.example"), \
             patch.object(auth_module, "cloud_auth_request", new=fake_cloud):
            result = await auth_module.change_password(request, user=self._user(), session=session)

        self.assertEqual(forwarded["action"], "change_password")
        self.assertEqual(forwarded["payload"]["old_password"], "admin123")
        self.assertEqual(forwarded["payload"]["new_password"], "newpass123")
        self.assertEqual(forwarded["token"], "cloud-token")
        self.assertTrue(result["data"]["cloud"])
        # 本机镜像同步为**新**密码，保持与云端一致
        from backend.app.core.security import verify_password

        self.assertTrue(verify_password("newpass123", record.password_hash))

    async def test_cloud_failure_does_not_touch_local_password(self):
        from backend.app.api.routes import auth as auth_module
        from common.services.cloud_auth import CloudAuthError

        original = "pbkdf2$old$deadbeef"
        record = User(id=1, username="admin", role="admin", status=1, password_hash=original)
        session = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: record)),
            commit=AsyncMock(),
        )

        async def failing_cloud(action, payload, token=""):
            raise CloudAuthError("invalid_credentials", "原密码不正确", 401)

        request = SimpleNamespace(old_password="wrong", new_password="newpass123")
        with patch.object(auth_module, "cloud_auth_url", return_value="https://auth.example"), \
             patch.object(auth_module, "cloud_auth_request", new=failing_cloud):
            with self.assertRaises(HTTPException) as ctx:
                await auth_module.change_password(request, user=self._user(), session=session)

        self.assertEqual(ctx.exception.status_code, 401)
        # 云端失败时本机密码不能被改动，否则两边不一致
        self.assertEqual(record.password_hash, original)
        session.commit.assert_not_awaited()

    async def test_cloud_mode_requires_old_password(self):
        from backend.app.api.routes import auth as auth_module

        record = User(id=1, username="admin", role="admin", status=1, password_hash="h")
        session = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: record)),
            commit=AsyncMock(),
        )
        request = SimpleNamespace(old_password=None, new_password="newpass123")
        with patch.object(auth_module, "cloud_auth_url", return_value="https://auth.example"):
            with self.assertRaises(HTTPException) as ctx:
                await auth_module.change_password(request, user=self._user(), session=session)
        self.assertEqual(ctx.exception.status_code, 422)

    async def test_cloud_mode_requires_cloud_session(self):
        from backend.app.api.routes import auth as auth_module

        record = User(id=1, username="admin", role="admin", status=1, password_hash="h")
        session = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: record)),
            commit=AsyncMock(),
        )
        request = SimpleNamespace(old_password="admin123", new_password="newpass123")
        user = {"sub": "1", "username": "admin", "role": "admin"}
        with patch.object(auth_module, "cloud_auth_url", return_value="https://auth.example"):
            with self.assertRaises(HTTPException) as ctx:
                await auth_module.change_password(request, user=user, session=session)
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_local_mode_changes_local_password(self):
        from backend.app.api.routes import auth as auth_module
        from backend.app.core.security import hash_password, verify_password

        record = User(id=1, username="admin", role="admin", status=1, password_hash=hash_password("admin123"))
        session = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: record)),
            commit=AsyncMock(),
        )
        request = SimpleNamespace(old_password="admin123", new_password="newpass123")
        with patch.object(auth_module, "cloud_auth_url", return_value=""):
            result = await auth_module.change_password(request, user={"sub": "1"}, session=session)

        self.assertFalse(result["data"]["cloud"])
        self.assertTrue(verify_password("newpass123", record.password_hash))

    async def test_local_mode_rejects_wrong_old_password(self):
        from backend.app.api.routes import auth as auth_module
        from backend.app.core.security import hash_password

        record = User(id=1, username="admin", role="admin", status=1, password_hash=hash_password("admin123"))
        session = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: record)),
            commit=AsyncMock(),
        )
        request = SimpleNamespace(old_password="wrong", new_password="newpass123")
        with patch.object(auth_module, "cloud_auth_url", return_value=""):
            with self.assertRaises(HTTPException) as ctx:
                await auth_module.change_password(request, user={"sub": "1"}, session=session)
        self.assertEqual(ctx.exception.status_code, 401)


class FrontendGateRemovedTests(unittest.TestCase):
    """默认密码门禁必须已移除（否则管理员无法新增账号）。"""

    def _source(self) -> str:
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "frontend", "src", "pages", "accounts", "Accounts.tsx",
        )
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_no_default_password_gate_left(self):
        source = self._source()
        self.assertNotIn("checkAdminPassword", source)
        self.assertNotIn("checkAdminDefaultPassword", source)

    def test_add_account_entries_still_present(self):
        """移除门禁不能连带删掉添加入口本身。"""
        source = self._source()
        for entry in ("'/accounts/shared-scan'", "setActiveModal('password')", "setActiveModal('manual')", "setActiveModal('qrcode')"):
            self.assertIn(entry, source, f"添加入口 {entry} 丢失")


if __name__ == "__main__":
    unittest.main()
