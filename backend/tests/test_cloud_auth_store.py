"""Shared cloud authentication regression tests."""
from __future__ import annotations

import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from deploy.cloud_auth.auth_store import AuthError, AuthStore


class CloudAuthStoreTests(unittest.TestCase):
    def setUp(self):
        self.environment = os.environ.copy()
        os.environ["XIANYU_BOOTSTRAP_PASSWORD"] = "test-bootstrap-password"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.environment)

    def test_invite_expiry_is_persisted_and_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AuthStore(str(Path(directory) / "server.db"))
            synced = store.sync_invites([{"code": "ABCD-EFGH-IJKL-MNOP", "status": "active", "expires_at": "2000-01-01T00:00:00+00:00"}])
            self.assertTrue(synced[0]["expires_at"].startswith("2000-01-01T"))

            with self.assertRaisesRegex(AuthError, "邀请码已过期") as raised:
                store.register("expired-user", "password123", "Expired", "ABCD-EFGH-IJKL-MNOP", "203.0.113.10")
            self.assertEqual(raised.exception.code, "invalid_invite")

    def test_single_use_invite_is_atomic_under_concurrent_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AuthStore(str(Path(directory) / "server.db"))
            store.sync_invites([{"code": "QRST-UVWX-YZ12-3456", "status": "active"}])

            def register(index: int):
                try:
                    return ("ok", store.register(f"parallel-{index}", "password123", "Parallel", "QRST-UVWX-YZ12-3456", f"203.0.113.{index + 10}"))
                except AuthError as exc:
                    return (exc.code, exc.message)

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(register, (0, 1)))

            self.assertEqual(sum(result[0] == "ok" for result in results), 1)
            self.assertEqual(sum(result[0] == "invalid_invite" for result in results), 1)

    def test_account_session_payload_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            os.environ["XIANYU_CLOUD_SESSION_KEY"] = "test-session-key"
            store = AuthStore(str(Path(directory) / "server.db"))
            oversized = "x" * (store.MAX_ACCOUNT_SESSION_PAYLOAD_BYTES + 1)
            with self.assertRaisesRegex(AuthError, "会话内容过大") as raised:
                store.save_account_session(
                    1,
                    {
                        "account_key": "account-1",
                        "account_name": "Account 1",
                        "device_id": "device-1",
                        "session_payload": {"cookie": oversized},
                    },
                )
            self.assertEqual(raised.exception.code, "invalid_input")

    def test_cloud_user_plan_can_be_updated_and_expiration_can_be_cleared(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AuthStore(str(Path(directory) / "server.db"))
            store.sync_invites([{"code": "PLAN-TEST-1234-5678", "status": "active"}])
            user = store.register("plan-user", "password123", "Plan User", "PLAN-TEST-1234-5678", "203.0.113.20")

            updated = store.update_entitlements(
                user["id"],
                plan_code="vip",
                plan_expires_at="2030-01-02T03:04:05",
                clear_plan_expires_at=True,
            )
            self.assertEqual(updated["plan_code"], "VIP")
            self.assertEqual(updated["plan_expires_at"], "2030-01-02T03:04:05")
            self.assertEqual(store.get_user(user["id"])["plan_code"], "VIP")

            cleared = store.update_entitlements(
                user["id"],
                plan_code="VIP",
                plan_expires_at=None,
                clear_plan_expires_at=True,
            )
            self.assertIsNone(cleared["plan_expires_at"])


if __name__ == "__main__":
    unittest.main()
