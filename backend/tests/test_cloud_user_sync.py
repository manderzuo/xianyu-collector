# -*- coding: utf-8 -*-
"""存量账号/邀请码导入云端的回归测试。

覆盖的核心缺陷：1.3.5 起登录由云端做密码权威，但界面注册只写本机 MySQL，
云端从来没有这些账号，用户会看到「用户名或密码错误」。

最关键的断言是 :func:`test_local_hash_transcodes_to_cloud_verifiable`：
本机格式的密码哈希转码成云端格式后，必须能被**云端真实实现**校验通过
（测试直接导入 ``deploy/cloud_auth/auth_store.py``，不是复制品）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.app.core.security import hash_password
from common.services.cloud_user_sync import (
    LOCAL_HASH_SCHEME,
    build_invite_payload,
    build_user_payload,
    chunked,
    cloud_role,
    cloud_status,
    local_hash_to_cloud,
)
from common.services.registration_invites import hash_invite_code, preview_invite_code

# 直接使用部署端的真实实现，保证转码兼容性断言不会因为复制实现而失真。
_CLOUD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "deploy", "cloud_auth")
if _CLOUD_DIR not in sys.path:
    sys.path.insert(0, _CLOUD_DIR)

from auth_store import AuthError, AuthStore  # noqa: E402
from server import Handler  # noqa: E402


class HashTranscodeTests(unittest.TestCase):
    """本机哈希 → 云端哈希的无损转码。"""

    def test_local_hash_transcodes_to_cloud_verifiable(self):
        # 本机默认盐与自定义盐都要能转码
        for salt in ("xr-default-salt", "another-salt"):
            local = hash_password("secret123", salt)
            cloud = local_hash_to_cloud(local)
            self.assertIsNotNone(cloud, salt)
            self.assertTrue(cloud.startswith("pbkdf2_sha256$120000$"), cloud)
            # 用云端真实实现验证：这是整条迁移链路的正确性依据
            self.assertTrue(AuthStore.verify_password("secret123", cloud), salt)

    def test_transcoded_hash_rejects_wrong_password(self):
        cloud = local_hash_to_cloud(hash_password("secret123"))
        self.assertFalse(AuthStore.verify_password("secret124", cloud))
        self.assertFalse(AuthStore.verify_password("", cloud))

    def test_cloud_format_passes_through_unchanged(self):
        already = AuthStore.hash_password("secret123")
        self.assertEqual(local_hash_to_cloud(already), already)

    def test_unparseable_values_return_none(self):
        for value in (None, "", "not-a-hash", "pbkdf2$only-two", "bcrypt$abc$def", "pbkdf2$$"):
            self.assertIsNone(local_hash_to_cloud(value), repr(value))

    def test_non_hex_digest_returns_none(self):
        self.assertIsNone(local_hash_to_cloud("pbkdf2$salt$zzzz"))

    def test_scheme_constant_matches_local_writer(self):
        self.assertTrue(hash_password("x").startswith(LOCAL_HASH_SCHEME + "$"))


class PayloadBuilderTests(unittest.TestCase):
    def _user(self, **overrides):
        base = {
            "id": 1, "username": "alice", "nickname": "爱丽丝", "email": "Alice@Example.com ",
            "role": "user", "status": 1, "plan_code": "vip",
            "password_hash": hash_password("secret123"), "created_at": datetime(2026, 1, 2, 3, 4, 5),
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_status_and_role_mapping(self):
        self.assertEqual(cloud_status(1), "approved")
        self.assertEqual(cloud_status(2), "pending")
        self.assertEqual(cloud_status(0), "disabled")
        self.assertEqual(cloud_status(None), "disabled")
        self.assertEqual(cloud_role("admin"), "admin")
        self.assertEqual(cloud_role("administrator"), "admin")
        self.assertEqual(cloud_role("user"), "employee")
        self.assertEqual(cloud_role(None), "employee")

    def test_payload_normalizes_fields(self):
        items, skipped = build_user_payload([self._user()])
        self.assertEqual(skipped, [])
        self.assertEqual(len(items), 1)
        entry = items[0]
        self.assertEqual(entry["username"], "alice")
        self.assertEqual(entry["status"], "approved")
        self.assertEqual(entry["role"], "employee")
        # 邮箱去除首尾空格并小写，套餐码大写
        self.assertEqual(entry["email"], "alice@example.com")
        self.assertEqual(entry["plan_code"], "VIP")
        self.assertTrue(AuthStore.verify_password("secret123", entry["password_hash"]))

    def test_unreadable_hash_is_skipped_not_imported(self):
        items, skipped = build_user_payload([
            self._user(),
            self._user(id=2, username="broken", password_hash="not-a-hash"),
        ])
        self.assertEqual([entry["username"] for entry in items], ["alice"])
        self.assertEqual(skipped, ["broken"])

    def test_blank_username_is_skipped(self):
        items, skipped = build_user_payload([self._user(username="   ")])
        self.assertEqual(items, [])
        self.assertEqual(skipped, ["(空用户名)"])

    def test_invite_payload_syncs_by_hash(self):
        invite = SimpleNamespace(
            id=7, code_hash=hash_invite_code("ABCD-EFGH-IJKL-MNOP"),
            code_preview=preview_invite_code("ABCD-EFGH-IJKL-MNOP"),
            status="active", expires_at=None,
        )
        items, skipped = build_invite_payload([invite])
        self.assertEqual(skipped, [])
        self.assertEqual(items[0]["code_hash"], hash_invite_code("ABCDEFGHIJKLMNOP"))
        self.assertEqual(items[0]["status"], "active")

    def test_invite_payload_skips_bad_hash(self):
        invite = SimpleNamespace(id=8, code_hash="short", code_preview="x", status="active", expires_at=None)
        items, skipped = build_invite_payload([invite])
        self.assertEqual(items, [])
        self.assertEqual(skipped, ["8"])

    def test_invite_hash_matches_cloud_hashing(self):
        """本机与云端对同一邀请码必须算出同一个哈希，否则同步无意义。"""
        code = "abcd efgh-ijkl-mnop"
        self.assertEqual(hash_invite_code(code), AuthStore.__module__ and __import__("auth_store").hash_invite_code(code))

    def test_chunked_respects_batch_size(self):
        items = [{"username": str(index)} for index in range(1201)]
        batches = chunked(items, 500)
        self.assertEqual([len(batch) for batch in batches], [500, 500, 201])
        self.assertEqual([item for batch in batches for item in batch], items)


class CloudImportStoreTests(unittest.TestCase):
    """云端 import_users 的行为与安全约定。"""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = AuthStore(os.path.join(self._dir.name, "auth.db"))

    def _entry(self, username="alice", password="secret123", **overrides):
        entry = {
            "username": username,
            "password_hash": local_hash_to_cloud(hash_password(password)),
            "employee_name": "爱丽丝", "role": "employee", "status": "approved",
        }
        entry.update(overrides)
        return entry

    def test_import_creates_user_that_can_authenticate(self):
        result = self.store.import_users([self._entry()])
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["created_users"], ["alice"])
        # 导入后必须能真正登录：这是修复“注册了却登不上”的验收点
        user = self.store.authenticate("alice", "secret123")
        self.assertEqual(user["username"], "alice")
        self.assertEqual(user["status"], "approved")

    def test_import_is_idempotent(self):
        self.store.import_users([self._entry()])
        second = self.store.import_users([self._entry()])
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(second["skipped_users"], ["alice"])

    def test_import_does_not_overwrite_existing_cloud_password(self):
        """云端是权威：导入不得覆盖云端已有账号的密码或状态。"""
        existing = self.store.register("bob", "cloudpass1", "Bob", self._invite("BOBINVITE01"))
        self.store.approve(existing["id"])
        # 本机同名账号用不同密码，导入时应被跳过
        result = self.store.import_users([self._entry("bob", password="localpass1")])
        self.assertEqual(result["created"], 0)
        # 云端原密码仍然有效，本机密码不应生效
        self.store.authenticate("bob", "cloudpass1")
        with self.assertRaises(AuthError):
            self.store.authenticate("bob", "localpass1")

    def test_import_does_not_overwrite_admin(self):
        admin = self.store.authenticate("admin", self._bootstrap_password())
        self.store.import_users([self._entry("admin", password="hijack123")])
        with self.assertRaises(AuthError):
            self.store.authenticate("admin", "hijack123")
        self.assertEqual(self.store.authenticate("admin", self._bootstrap_password())["id"], admin["id"])

    def test_import_rejects_local_format_hash(self):
        """本机序列化格式必须被拒，否则会写入无法验证的哈希。"""
        with self.assertRaises(AuthError) as ctx:
            self.store.import_users([self._entry(password_hash=hash_password("secret123"))])
        self.assertEqual(ctx.exception.code, "invalid_input")

    def test_import_rejects_invalid_status(self):
        with self.assertRaises(AuthError):
            self.store.import_users([self._entry(status="superuser")])

    def test_import_rejects_empty_and_oversized_batches(self):
        with self.assertRaises(AuthError):
            self.store.import_users([])
        with self.assertRaises(AuthError):
            self.store.import_users([self._entry(f"user{index}") for index in range(AuthStore.MAX_IMPORT_BATCH + 1)])

    def test_imported_pending_user_cannot_login(self):
        self.store.import_users([self._entry("carol", status="pending")])
        with self.assertRaises(AuthError) as ctx:
            self.store.authenticate("carol", "secret123")
        self.assertEqual(ctx.exception.code, "account_pending")

    def test_import_stores_email_for_mirror(self):
        self.store.import_users([self._entry(email="Carol@Example.com")])
        self.assertEqual(self.store.authenticate("alice", "secret123")["email"], "carol@example.com")

    # -- 辅助 --------------------------------------------------------------

    def _bootstrap_password(self):
        return os.environ["XIANYU_BOOTSTRAP_PASSWORD"]

    def _invite(self, code):
        self.store.sync_invites([{"code": code, "status": "active"}])
        return code


class SyncUsersAuthTests(unittest.TestCase):
    """云端 sync_users 的鉴权必须失败关闭。"""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        os.environ.setdefault("XIANYU_BOOTSTRAP_PASSWORD", "bootstrap-pass-1")
        self.store = AuthStore(os.path.join(self._dir.name, "auth.db"))

    def _handler(self, token="", body=None):
        captured = {}
        stub = SimpleNamespace(
            headers={"Authorization": f"Bearer {token}"} if token else {},
            request_id="test-request",
        )
        stub.reply = lambda status, data: captured.update(status=status, data=data) or captured
        result = Handler._handle_sync_users(stub, self.store, body or {})
        return captured if captured else result

    def test_rejects_when_no_secret_configured_and_no_token(self):
        with patch.dict(os.environ, {"XIANYU_CLOUD_SYNC_SECRET": ""}, clear=False):
            result = self._handler(body={"users": []})
        self.assertEqual(result["status"], 401)

    def test_rejects_wrong_secret(self):
        with patch.dict(os.environ, {"XIANYU_CLOUD_SYNC_SECRET": "right-secret"}, clear=False):
            result = self._handler(body={"sync_secret": "wrong-secret", "users": []})
        self.assertEqual(result["status"], 401)

    def test_rejects_non_admin_session(self):
        # 注册需要云端已存在该邀请码，先同步再注册
        self.store.sync_invites([{"code": "DAVEINVITE01", "status": "active"}])
        employee = self.store.register("dave", "secret123", "Dave", "DAVEINVITE01")
        self.store.approve(employee["id"])
        token = self.store.create_session(employee["id"])
        with patch.dict(os.environ, {"XIANYU_CLOUD_SYNC_SECRET": ""}, clear=False):
            result = self._handler(token=token, body={"users": []})
        self.assertEqual(result["status"], 403)

    def test_accepts_correct_secret_and_imports(self):
        entry = {
            "username": "erin",
            "password_hash": local_hash_to_cloud(hash_password("secret123")),
            "status": "approved", "role": "employee",
        }
        with patch.dict(os.environ, {"XIANYU_CLOUD_SYNC_SECRET": "shared-secret"}, clear=False):
            result = self._handler(body={"sync_secret": "shared-secret", "users": [entry]})
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["data"]["created"], 1)
        self.assertEqual(self.store.authenticate("erin", "secret123")["username"], "erin")


class RegisterForwardingTests(unittest.IsolatedAsyncioTestCase):
    """云端模式下注册必须转发到云端，而不是只写本机库。"""

    async def test_cloud_register_is_forwarded_and_no_local_row_created(self):
        from backend.app.api.routes.auth import register
        from common.schemas.api import LoginRequest  # noqa: F401  (仅为保持导入一致性)

        class _Request:
            username = "newbie"
            password = "secret123"
            nickname = None
            invite_code = "ABCD-EFGH-IJKL-MNOP"
            email = "Newbie@Example.com"
            session_id = "session-1"

        session = AsyncMock()
        forwarded = {}

        async def fake_cloud(action, payload, token=""):
            forwarded["action"] = action
            forwarded["payload"] = payload
            return {"user": {"username": "newbie", "status": "pending"}, "message": "注册申请已提交，请等待管理员审核"}

        with patch("backend.app.api.routes.auth.registration_enabled", new=AsyncMock(return_value=True)), \
             patch("backend.app.api.routes.auth.cloud_auth_url", return_value="https://auth.example"), \
             patch("backend.app.api.routes.auth.cloud_auth_request", new=fake_cloud):
            result = await register(_Request(), session)

        self.assertEqual(forwarded["action"], "register")
        self.assertEqual(forwarded["payload"]["username"], "newbie")
        self.assertEqual(forwarded["payload"]["email"], "newbie@example.com")
        # 邀请码已规范化（去掉短横线并大写）
        self.assertEqual(forwarded["payload"]["invite_code"], "ABCDEFGHIJKLMNOP")
        self.assertIn("等待管理员审核", result["message"])
        # 云端模式下不得在本机库创建账号
        session.add.assert_not_called()

    async def test_cloud_error_status_is_propagated(self):
        from backend.app.api.routes.auth import register
        from common.services.cloud_auth import CloudAuthError

        class _Request:
            username = "newbie"
            password = "secret123"
            nickname = None
            invite_code = "ABCD-EFGH-IJKL-MNOP"
            email = None
            session_id = "session-1"

        async def failing_cloud(action, payload, token=""):
            raise CloudAuthError("invalid_invite", "邀请码无效，请向管理员索取有效邀请码", 400)

        with patch("backend.app.api.routes.auth.registration_enabled", new=AsyncMock(return_value=True)), \
             patch("backend.app.api.routes.auth.cloud_auth_url", return_value="https://auth.example"), \
             patch("backend.app.api.routes.auth.cloud_auth_request", new=failing_cloud):
            with self.assertRaises(Exception) as ctx:
                await register(_Request(), AsyncMock())

        self.assertEqual(getattr(ctx.exception, "status_code", None), 400)


if __name__ == "__main__":
    unittest.main()
