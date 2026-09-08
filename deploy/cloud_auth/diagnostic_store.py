# -*- coding: utf-8 -*-
"""Encrypted, private storage for client diagnostic archives."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping

from cryptography.fernet import Fernet, InvalidToken

from auth_store import AuthError


class DiagnosticStore:
    MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
    MAX_STORAGE_BYTES = 512 * 1024 * 1024
    MAX_ARCHIVE_MEMBERS = 128
    MAX_MEMBER_BYTES = 4 * 1024 * 1024
    MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
    ALLOWED_STATUS = {"pending", "processing", "resolved", "ignored"}
    ALLOWED_SEVERITY = {"info", "warning", "error", "critical"}
    SECRET_FIELD_RE = re.compile(
        r"(?i)([\"']?(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|"
        r"jwt[_-]?secret|cookie|set-cookie|authorization|encryption[_-]?key|private[_-]?key)"
        r"[\"']?\s*[:=]\s*)(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;}\r\n]+)"
    )
    BEARER_RE = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+")
    QUERY_SECRET_RE = re.compile(r"(?i)([?&](?:token|password|secret|key)=)[^&\s]+")
    PRIVATE_KEY_RE = re.compile(r"(?is)-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----")
    ALLOWED_ARCHIVE_EXTENSIONS = {".csv", ".json", ".log", ".txt", ".xml", ".yaml", ".yml"}

    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)
        self.storage_dir = Path(self.db_path).with_name("diagnostics")
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.storage_dir.chmod(0o700)
        except OSError:
            pass
        self._storage_lock = threading.RLock()
        self.ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _text(value: Any, maximum: int | None, default: str = "") -> str:
        text = str(value or "").strip()
        if maximum is not None:
            text = text[:maximum]
        result = "".join(char for char in text if ord(char) >= 32 or char in "\r\n\t")
        return result or default

    @classmethod
    def _redact_text(cls, value: Any, maximum: int | None = 500) -> str:
        text = cls._text(value, maximum)
        if not text:
            return ""
        text = cls.PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)
        text = cls.SECRET_FIELD_RE.sub(lambda match: match.group(1) + "[REDACTED]", text)
        text = cls.BEARER_RE.sub(lambda match: match.group(1) + "[REDACTED]", text)
        return cls.QUERY_SECRET_RE.sub(lambda match: match.group(1) + "[REDACTED]", text)

    @staticmethod
    def _storage_key() -> Fernet:
        seed = os.environ.get("XIANYU_DIAGNOSTIC_STORAGE_KEY", "").strip()
        if not seed:
            seed = os.environ.get("XIANYU_CLOUD_SESSION_KEY", "").strip()
        if not seed:
            raise AuthError("configuration_error", "诊断日志加密密钥未配置")
        key = base64.urlsafe_b64encode(hashlib.sha256(("diagnostics:" + seed).encode("utf-8")).digest())
        return Fernet(key)

    @staticmethod
    def _device_hash(value: Any) -> str:
        text = str(value or "").strip()
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24] if text else ""

    def ensure_schema(self) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS client_diagnostics (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  report_code TEXT NOT NULL UNIQUE,
                  owner_user_id INTEGER,
                  owner_username TEXT,
                  device_hash TEXT,
                  version TEXT NOT NULL DEFAULT '',
                  build TEXT NOT NULL DEFAULT '',
                  stage TEXT NOT NULL DEFAULT '',
                  severity TEXT NOT NULL DEFAULT 'error',
                  summary TEXT NOT NULL DEFAULT '',
                  metadata_json TEXT NOT NULL DEFAULT '{}',
                  storage_name TEXT NOT NULL,
                  size_bytes INTEGER NOT NULL DEFAULT 0,
                  sha256 TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT 'pending',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  resolved_at TEXT,
                  expires_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS client_diagnostic_audit (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  report_id INTEGER,
                  actor_user_id INTEGER,
                  action TEXT NOT NULL,
                  detail TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS client_diagnostic_rate_limits (
                  identity TEXT PRIMARY KEY,
                  window_started_at REAL NOT NULL,
                  request_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.commit()

    def _audit(self, conn: sqlite3.Connection, report_id: int | None, actor_user_id: int | None, action: str, detail: str = "") -> None:
        conn.execute(
            "INSERT INTO client_diagnostic_audit(report_id, actor_user_id, action, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (report_id, actor_user_id, self._text(action, 64), self._text(detail, 500), self._now()),
        )

    def _retention_days(self) -> int:
        try:
            value = int(os.environ.get("XIANYU_DIAGNOSTICS_RETENTION_DAYS", "30"))
        except ValueError:
            value = 30
        return max(7, min(value, 180))

    def _check_upload_rate(self, owner_id: int | None, source_ip: str, device_id: str) -> None:
        # Keep the public pre-login limit keyed to the server-observed source
        # address. A client-controlled device_id must never be the only guard,
        # otherwise an attacker can rotate it and fill the storage quota.
        del device_id
        window = 300.0 if owner_id is None else 3600.0
        maximum = 3 if owner_id is None else 20
        source_hash = self._device_hash("ip:" + self._text(source_ip, 128)) or "unknown"
        identity = f"user:{owner_id}" if owner_id else f"anonymous-ip:{source_hash}"
        now = time.time()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM client_diagnostic_rate_limits WHERE window_started_at < ?",
                (now - 2 * 24 * 60 * 60,),
            )
            row = conn.execute(
                "SELECT window_started_at, request_count FROM client_diagnostic_rate_limits WHERE identity = ?",
                (identity,),
            ).fetchone()
            if row is not None and now - float(row["window_started_at"]) < window:
                request_count = int(row["request_count"] or 0)
                if request_count >= maximum:
                    raise AuthError("rate_limited", "诊断日志上传过于频繁，请稍后重试")
                conn.execute(
                    "UPDATE client_diagnostic_rate_limits SET request_count = request_count + 1 WHERE identity = ?",
                    (identity,),
                )
            else:
                conn.execute(
                    "INSERT INTO client_diagnostic_rate_limits(identity, window_started_at, request_count) VALUES (?, ?, 1) "
                    "ON CONFLICT(identity) DO UPDATE SET window_started_at=excluded.window_started_at, request_count=1",
                    (identity, now),
                )
            conn.commit()

    @classmethod
    def _validate_archive(cls, archive: bytes) -> None:
        try:
            with zipfile.ZipFile(BytesIO(archive)) as package:
                members = package.infolist()
                if not members:
                    raise AuthError("invalid_input", "诊断报告压缩包为空")
                if len(members) > cls.MAX_ARCHIVE_MEMBERS:
                    raise AuthError("invalid_input", "诊断报告文件数量超过限制")
                total_size = 0
                for member in members:
                    normalized = member.filename.replace("\\", "/")
                    parts = normalized.split("/")
                    mode = (member.external_attr >> 16) & 0o170000
                    if (
                        not normalized
                        or normalized.startswith("/")
                        or re.match(r"^[A-Za-z]:/", normalized)
                        or any(part in {"", ".", ".."} for part in parts)
                        or mode == 0o120000
                    ):
                        raise AuthError("invalid_input", "诊断报告包含不安全的文件路径")
                    if member.is_dir():
                        continue
                    extension = Path(normalized).suffix.lower()
                    if extension not in cls.ALLOWED_ARCHIVE_EXTENSIONS:
                        raise AuthError("invalid_input", "诊断报告包含不允许的文件类型")
                    if member.file_size > cls.MAX_MEMBER_BYTES:
                        raise AuthError("invalid_input", "诊断报告单个文件超过大小限制")
                    total_size += member.file_size
                    if total_size > cls.MAX_UNCOMPRESSED_BYTES:
                        raise AuthError("invalid_input", "诊断报告解压后超过大小限制")
        except AuthError:
            raise
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise AuthError("invalid_input", "诊断报告文件格式无效") from exc

    @classmethod
    def _sanitize_archive(cls, archive: bytes) -> bytes:
        text_extensions = {".csv", ".json", ".log", ".txt", ".xml", ".yaml", ".yml"}
        output = BytesIO()
        try:
            with zipfile.ZipFile(BytesIO(archive)) as source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
                for member in source.infolist():
                    if member.is_dir():
                        continue
                    data = source.read(member)
                    extension = Path(member.filename.replace("\\", "/")).suffix.lower()
                    if extension in text_extensions and b"\x00" not in data:
                        text = data.decode("utf-8", errors="replace")
                        data = cls._redact_text(text, None).encode("utf-8")
                    target.writestr(member.filename, data)
            return output.getvalue()
        except (OSError, RuntimeError, UnicodeError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise AuthError("invalid_input", "诊断报告文件格式无效") from exc

    def purge_expired(self) -> int:
        now = self._now()
        removed = 0
        storage_paths: list[Path] = []
        with self._storage_lock:
            with self._connection() as conn:
                rows = conn.execute("SELECT id, storage_name FROM client_diagnostics WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)).fetchall()
                for row in rows:
                    storage_paths.append(self.storage_dir / str(row["storage_name"]))
                    conn.execute("DELETE FROM client_diagnostic_audit WHERE report_id = ?", (int(row["id"]),))
                    conn.execute("DELETE FROM client_diagnostics WHERE id = ?", (int(row["id"]),))
                    removed += 1
                if removed:
                    conn.commit()
                known_storage = {
                    str(row["storage_name"])
                    for row in conn.execute("SELECT storage_name FROM client_diagnostics").fetchall()
                }
            for storage_path in storage_paths:
                try:
                    storage_path.unlink(missing_ok=True)
                except OSError:
                    pass
            # A process crash between writing the encrypted archive and
            # inserting its database row can leave an orphan file.  Keep a
            # grace period so an in-flight upload is never removed, then
            # garbage-collect only the expected *.bin storage files.
            orphan_cutoff = time.time() - 3600
            for candidate in self.storage_dir.glob("*.bin"):
                if candidate.name in known_storage:
                    continue
                try:
                    if candidate.stat().st_mtime >= orphan_cutoff:
                        continue
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass
        return removed

    def save_upload(self, payload: Mapping[str, Any], owner: Mapping[str, Any] | None = None, source_ip: str = "") -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise AuthError("invalid_input", "诊断报告格式无效")
        encoded = payload.get("archive_base64")
        if not isinstance(encoded, str) or not encoded:
            raise AuthError("invalid_input", "诊断报告文件为空")
        if len(encoded) > self.MAX_ARCHIVE_BYTES * 2:
            raise AuthError("invalid_input", "诊断报告超过大小限制")
        try:
            archive = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (ValueError, UnicodeError) as exc:
            raise AuthError("invalid_input", "诊断报告编码无效") from exc
        if not archive or len(archive) > self.MAX_ARCHIVE_BYTES:
            raise AuthError("invalid_input", "诊断报告超过10MB大小限制")
        if not zipfile.is_zipfile(BytesIO(archive)):
            raise AuthError("invalid_input", "诊断报告文件格式无效")
        self._validate_archive(archive)
        archive = self._sanitize_archive(archive)
        if not archive or len(archive) > self.MAX_ARCHIVE_BYTES:
            raise AuthError("invalid_input", "诊断报告超过10MB大小限制")

        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
        safe_metadata: dict[str, str] = {}
        for key in ("error_code", "component", "client_type", "os", "python", "docker", "network", "message"):
            value = self._redact_text(metadata.get(key), 300)
            if value:
                safe_metadata[key] = value
        metadata_json = json.dumps(safe_metadata, ensure_ascii=False, separators=(",", ":"))

        severity = self._text(metadata.get("severity"), 16, "error").lower()
        if severity not in self.ALLOWED_SEVERITY:
            severity = "error"
        version = self._text(metadata.get("version"), 64)
        build = self._text(metadata.get("build"), 128)
        stage = self._text(metadata.get("stage"), 32, "unknown")
        summary = self._redact_text(metadata.get("summary") or metadata.get("message"), 500) or "客户端诊断报告"
        owner_id = None
        owner_name = None
        if owner:
            try:
                owner_id = int(owner.get("id") or 0) or None
            except (TypeError, ValueError):
                owner_id = None
            owner_name = self._text(owner.get("username"), 64) or None
        self._check_upload_rate(owner_id=owner_id, source_ip=source_ip, device_id=metadata.get("device_id"))
        device_hash = self._device_hash(metadata.get("device_id")) or self._device_hash("ip:" + self._text(source_ip, 128))

        with self._storage_lock:
            self.purge_expired()
            with self._connection() as conn:
                current_bytes = int(conn.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM client_diagnostics").fetchone()[0] or 0)
            if current_bytes + len(archive) > self.MAX_STORAGE_BYTES:
                raise AuthError("storage_full", "诊断日志存储空间已满，请稍后重试")
            encrypted = self._storage_key().encrypt(archive)
            storage_name = f"{uuid.uuid4().hex}.bin"
            storage_path = self.storage_dir / storage_name
            now = datetime.now().astimezone()
            expires = now + timedelta(days=self._retention_days())
            try:
                storage_path.write_bytes(encrypted)
                try:
                    storage_path.chmod(0o600)
                except OSError:
                    pass
                with self._connection() as conn:
                    report_code = ""
                    for _ in range(5):
                        report_code = f"XY-{now.strftime('%Y%m%d')}-{secrets.token_hex(4).upper()}"
                        try:
                            cursor = conn.execute(
                                """
                                INSERT INTO client_diagnostics
                                  (report_code, owner_user_id, owner_username, device_hash, version, build,
                                   stage, severity, summary, metadata_json, storage_name, size_bytes, sha256,
                                   status, created_at, updated_at, expires_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                                """,
                                (report_code, owner_id, owner_name, device_hash, version, build, stage, severity,
                                 summary, metadata_json, storage_name, len(archive), hashlib.sha256(archive).hexdigest(),
                                 self._now(), self._now(), expires.isoformat(timespec="seconds")),
                            )
                            self._audit(conn, int(cursor.lastrowid), owner_id, "upload", "anonymous" if owner_id is None else "authenticated")
                            conn.commit()
                            report_id = int(cursor.lastrowid)
                            break
                        except sqlite3.IntegrityError:
                            continue
                    else:
                        raise AuthError("server_error", "无法生成诊断报告编号")
            except Exception:
                storage_path.unlink(missing_ok=True)
                raise
        return self.get_public(report_id)

    @staticmethod
    def _public_row(row: sqlite3.Row) -> dict[str, Any]:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        return {
            "id": int(row["id"]), "report_code": str(row["report_code"]),
            "owner_user_id": int(row["owner_user_id"]) if row["owner_user_id"] is not None else None,
            "owner_username": row["owner_username"], "device_hash": row["device_hash"] or "",
            "version": row["version"] or "", "build": row["build"] or "", "stage": row["stage"] or "",
            "severity": row["severity"] or "error", "summary": row["summary"] or "", "metadata": metadata,
            "size_bytes": int(row["size_bytes"] or 0), "sha256": row["sha256"] or "",
            "status": row["status"] or "pending", "created_at": row["created_at"],
            "updated_at": row["updated_at"], "resolved_at": row["resolved_at"], "expires_at": row["expires_at"],
        }

    def get_public(self, report_id: int) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM client_diagnostics WHERE id = ?", (int(report_id),)).fetchone()
            if row is None:
                raise AuthError("not_found", "诊断报告不存在")
            return self._public_row(row)

    def list_reports(self, *, owner_user_id: int | None = None, status: str = "", severity: str = "", search: str = "", limit: int = 50, offset: int = 0) -> dict[str, Any]:
        self.purge_expired()
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        conditions: list[str] = []
        params: list[Any] = []
        if owner_user_id is not None:
            conditions.append("owner_user_id = ?")
            params.append(int(owner_user_id))
        if self._text(status, 16).lower() in self.ALLOWED_STATUS:
            conditions.append("status = ?")
            params.append(self._text(status, 16).lower())
        if self._text(severity, 16).lower() in self.ALLOWED_SEVERITY:
            conditions.append("severity = ?")
            params.append(self._text(severity, 16).lower())
        search = self._text(search, 100)
        if search:
            conditions.append("(report_code LIKE ? OR owner_username LIKE ? OR summary LIKE ? OR version LIKE ?)")
            term = f"%{search}%"
            params.extend([term, term, term, term])
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self._connection() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM client_diagnostics" + where, params).fetchone()[0])
            rows = conn.execute("SELECT * FROM client_diagnostics" + where + " ORDER BY id DESC LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
            return {"items": [self._public_row(row) for row in rows], "total": total, "limit": limit, "offset": offset}

    def stats(self) -> dict[str, Any]:
        self.purge_expired()
        with self._connection() as conn:
            counts = {str(row["status"]): int(row["count"]) for row in conn.execute("SELECT status, COUNT(*) AS count FROM client_diagnostics GROUP BY status").fetchall()}
            day = (datetime.now().astimezone() - timedelta(days=1)).isoformat(timespec="seconds")
            recent = int(conn.execute("SELECT COUNT(*) FROM client_diagnostics WHERE created_at >= ?", (day,)).fetchone()[0])
            return {"total": sum(counts.values()), "pending": counts.get("pending", 0), "processing": counts.get("processing", 0), "resolved": counts.get("resolved", 0), "ignored": counts.get("ignored", 0), "last_24_hours": recent}

    def set_status(self, report_id: int, status: str, actor_user_id: int | None, note: str = "") -> dict[str, Any]:
        status = self._text(status, 16).lower()
        if status not in self.ALLOWED_STATUS:
            raise AuthError("invalid_input", "诊断报告状态无效")
        now = self._now()
        with self._connection() as conn:
            if conn.execute("SELECT id FROM client_diagnostics WHERE id = ?", (int(report_id),)).fetchone() is None:
                raise AuthError("not_found", "诊断报告不存在")
            conn.execute("UPDATE client_diagnostics SET status=?, updated_at=?, resolved_at=? WHERE id=?", (status, now, now if status == "resolved" else None, int(report_id)))
            self._audit(conn, int(report_id), actor_user_id, "status", f"{status}:{self._text(note, 300)}")
            conn.commit()
        return self.get_public(report_id)

    def delete(self, report_id: int, actor_user_id: int | None) -> None:
        with self._storage_lock:
            with self._connection() as conn:
                row = conn.execute("SELECT storage_name FROM client_diagnostics WHERE id = ?", (int(report_id),)).fetchone()
                if row is None:
                    raise AuthError("not_found", "诊断报告不存在")
                storage_path = self.storage_dir / str(row["storage_name"])
                self._audit(conn, int(report_id), actor_user_id, "delete")
                conn.execute("DELETE FROM client_diagnostics WHERE id = ?", (int(report_id),))
                conn.commit()
            try:
                storage_path.unlink(missing_ok=True)
            except OSError:
                pass

    def download(self, report_id: int) -> tuple[bytes, str, dict[str, Any]]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM client_diagnostics WHERE id = ?", (int(report_id),)).fetchone()
            if row is None:
                raise AuthError("not_found", "诊断报告不存在")
            try:
                data = self._storage_key().decrypt((self.storage_dir / str(row["storage_name"])).read_bytes())
            except FileNotFoundError as exc:
                raise AuthError("not_found", "诊断报告文件不存在") from exc
            except InvalidToken as exc:
                raise AuthError("server_error", "诊断报告无法解密") from exc
            return data, f"{row['report_code']}.zip", self._public_row(row)


__all__ = ["DiagnosticStore"]
