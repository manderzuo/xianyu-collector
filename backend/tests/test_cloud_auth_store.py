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


if __name__ == "__main__":
    unittest.main()
