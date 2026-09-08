"""云端客户端诊断报告存储的隔离与生命周期测试。"""
import base64
import io
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path


AUTH_SOURCE = Path(__file__).resolve().parents[2] / "deploy" / "cloud_auth"
if str(AUTH_SOURCE) not in sys.path:
    sys.path.insert(0, str(AUTH_SOURCE))

from diagnostic_store import DiagnosticStore  # noqa: E402


class DiagnosticStoreTests(unittest.TestCase):
    def setUp(self):
        self.environment = os.environ.copy()
        os.environ["XIANYU_DIAGNOSTIC_STORAGE_KEY"] = "test-diagnostic-storage-key"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.environment)

    @staticmethod
    def archive_payload() -> dict:
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("metadata.json", '{"summary":"test"}')
            archive.writestr("logs/error.log", "password=should-not-be-read-by-server")
        return {
            "archive_base64": base64.b64encode(stream.getvalue()).decode("ascii"),
            "metadata": {
                "summary": "测试报告",
                "severity": "error",
                "version": "1.0.0",
                "device_id": "device-a",
            },
        }

    def test_report_is_owned_and_file_is_encrypted(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "server.db")
            store = DiagnosticStore(database)
            report = store.save_upload(self.archive_payload(), {"id": 7, "username": "alice"}, "127.0.0.1")

            self.assertEqual(report["owner_user_id"], 7)
            self.assertEqual(store.list_reports(owner_user_id=7)["total"], 1)
            self.assertEqual(store.list_reports(owner_user_id=8)["total"], 0)

            stored_files = list((Path(directory) / "diagnostics").glob("*.bin"))
            self.assertEqual(len(stored_files), 1)
            self.assertNotIn(b"password=should-not-be-read-by-server", stored_files[0].read_bytes())

            downloaded, filename, _ = store.download(report["id"])
            self.assertTrue(filename.endswith(".zip"))
            self.assertTrue(downloaded.startswith(b"PK"))
            with zipfile.ZipFile(io.BytesIO(downloaded)) as archive:
                self.assertNotIn(b"should-not-be-read-by-server", archive.read("logs/error.log"))

    def test_status_and_delete_release_database_handles(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "server.db")
            store = DiagnosticStore(database)
            report = store.save_upload(self.archive_payload(), {"id": 7, "username": "alice"}, "127.0.0.1")
            self.assertEqual(store.set_status(report["id"], "resolved", 1)["status"], "resolved")
            store.delete(report["id"], 1)
            self.assertEqual(store.stats()["total"], 0)

    def test_public_upload_limit_is_persisted_and_not_device_id_only(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "server.db")
            store = DiagnosticStore(database)
            for index in range(3):
                payload = self.archive_payload()
                payload["metadata"]["device_id"] = f"device-{index}"
                store.save_upload(payload, None, "203.0.113.10")
            payload = self.archive_payload()
            payload["metadata"]["device_id"] = "a-new-device"
            with self.assertRaisesRegex(Exception, "上传过于频繁"):
                store.save_upload(payload, None, "203.0.113.10")

            restarted = DiagnosticStore(database)
            payload["metadata"]["device_id"] = "after-restart"
            with self.assertRaisesRegex(Exception, "上传过于频繁"):
                restarted.save_upload(payload, None, "203.0.113.10")

    def test_unsafe_archive_members_are_rejected(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("../outside.txt", "should not be accepted")
        payload = self.archive_payload()
        payload["archive_base64"] = base64.b64encode(stream.getvalue()).decode("ascii")
        with tempfile.TemporaryDirectory() as directory:
            store = DiagnosticStore(str(Path(directory) / "server.db"))
            with self.assertRaisesRegex(Exception, "不安全的文件路径"):
                store.save_upload(payload, None, "127.0.0.1")

    def test_plaintext_metadata_is_redacted_before_storage(self):
        payload = self.archive_payload()
        payload["metadata"]["message"] = '{"password":"secret123","authorization":"Bearer abc.def"}'
        with tempfile.TemporaryDirectory() as directory:
            store = DiagnosticStore(str(Path(directory) / "server.db"))
            report = store.save_upload(payload, {"id": 7, "username": "alice"}, "127.0.0.1")
            self.assertNotIn("secret123", report["summary"])
            self.assertNotIn("abc.def", report["metadata"].get("message", ""))

    def test_old_orphaned_storage_file_is_removed_during_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DiagnosticStore(str(Path(directory) / "server.db"))
            orphan = Path(directory) / "diagnostics" / "orphan.bin"
            orphan.write_bytes(b"encrypted-orphan")
            old = time.time() - 7200
            os.utime(orphan, (old, old))
            store.purge_expired()
            self.assertFalse(orphan.exists())


if __name__ == "__main__":
    unittest.main()
