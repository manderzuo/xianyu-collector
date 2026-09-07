# -*- coding: utf-8 -*-
"""本地/服务端共用的账户认证数据层。

第一阶段只负责账户、审批和角色，不参与采集业务数据。存储采用 SQLite，
密码使用 PBKDF2-SHA256 加随机盐保存，数据库中不出现明文密码。
"""

from __future__ import annotations

import base64
import hashlib
import json
import hmac
import os
import secrets
import sqlite3
import threading
from cryptography.fernet import Fernet, InvalidToken
from datetime import datetime, timedelta
from typing import Any, Mapping


def normalize_invite_code(value: Any) -> str:
    """Keep invite-code validation independent from the client package."""
    return "".join(str(value or "").upper().split()).replace("-", "")


def hash_invite_code(value: Any) -> str:
    return hashlib.sha256(normalize_invite_code(value).encode("utf-8")).hexdigest()


class AuthError(RuntimeError):
    """可安全展示给界面的认证业务错误。"""

    def __init__(self, code: str, message: str):
        self.code = str(code)
        self.message = str(message)
        super().__init__(self.message)


class AuthStore:
    """账户表访问器；每次操作使用独立连接，适合后台多线程调用。"""

    HASH_ALGORITHM = "sha256"
    HASH_ITERATIONS = 120_000
    MAX_USERNAME_LENGTH = 64
    MAX_PASSWORD_LENGTH = 256
    MAX_EMPLOYEE_NAME_LENGTH = 80
    SESSION_TTL_DAYS = 180

    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)
        self._schema_lock = threading.Lock()
        self.ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _session_fernet() -> Fernet:
        seed = os.environ.get("XIANYU_CLOUD_SESSION_KEY", "").strip()
        if not seed:
            raise AuthError("configuration_error", "云端会话加密密钥未配置")
        key = base64.urlsafe_b64encode(hashlib.sha256(seed.encode("utf-8")).digest())
        return Fernet(key)

    def ensure_schema(self) -> None:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with self._schema_lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS app_users (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                      password_hash TEXT NOT NULL,
                      employee_name TEXT NOT NULL,
                      role TEXT NOT NULL DEFAULT 'employee',
                      status TEXT NOT NULL DEFAULT 'pending',
                      created_at TEXT NOT NULL,
                      approved_at TEXT,
                      last_login_at TEXT,
                      updated_at TEXT NOT NULL
                    )
                    """
                )
                for column, definition in (
                    ("plan_code", "TEXT NOT NULL DEFAULT 'NORMAL'"),
                    ("plan_expires_at", "TEXT"),
                    ("entitlements_json", "TEXT NOT NULL DEFAULT '{}'"),
                ):
                    try:
                        conn.execute(f"ALTER TABLE app_users ADD COLUMN {column} {definition}")
                    except sqlite3.OperationalError as exc:
                        if "duplicate column" not in str(exc).lower():
                            raise
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS app_invites (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      code_hash TEXT NOT NULL UNIQUE,
                      code_preview TEXT NOT NULL,
                      status TEXT NOT NULL DEFAULT 'active',
                      created_at TEXT NOT NULL,
                      used_at TEXT,
                      used_by INTEGER
                    )
                    """
                )
                # 登录保持只保存不可逆的会话令牌摘要，不保存密码。会话表
                # 与业务数据同库，代码升级时会被保留，启动后可重新建立
                # 当前用户的数据读取范围。
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS app_sessions (
                      token_hash TEXT PRIMARY KEY,
                      user_id INTEGER NOT NULL,
                      created_at TEXT NOT NULL,
                      last_seen_at TEXT NOT NULL,
                      expires_at TEXT NOT NULL,
                      revoked_at TEXT,
                      FOREIGN KEY(user_id) REFERENCES app_users(id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS app_account_sessions (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      owner_user_id INTEGER NOT NULL,
                      account_key TEXT NOT NULL,
                      account_name TEXT NOT NULL,
                      ciphertext TEXT NOT NULL,
                      device_id TEXT NOT NULL,
                      metadata_json TEXT NOT NULL DEFAULT '{}',
                      revision INTEGER NOT NULL DEFAULT 1,
                      status TEXT NOT NULL DEFAULT 'active',
                      last_validated_at TEXT,
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL,
                      UNIQUE(owner_user_id, account_key),
                      FOREIGN KEY(owner_user_id) REFERENCES app_users(id)
                    )
                    """
                )
                # 只在管理员不存在时创建默认管理员；已有管理员密码绝不覆盖。
                row = conn.execute(
                    "SELECT id FROM app_users WHERE username = ?", ("admin",)
                ).fetchone()
                if row is None:
                    now = self._now()
                    conn.execute(
                        """
                        INSERT INTO app_users
                          (username, password_hash, employee_name, role, status,
                           created_at, approved_at, updated_at)
                        VALUES (?, ?, ?, 'admin', 'approved', ?, ?, ?)
                        """,
                        (
                            "admin", self.hash_password(os.environ["XIANYU_BOOTSTRAP_PASSWORD"]), "系统管理员",
                            now, now, now,
                        ),
                    )
                else:
                    # admin 是保留账号，防止误注册后失去审批能力；不动其密码。
                    conn.execute(
                        """
                        UPDATE app_users
                           SET role = 'admin', status = 'approved',
                               employee_name = CASE WHEN employee_name = ''
                                                    THEN '系统管理员'
                                                    ELSE employee_name END,
                               updated_at = ?
                         WHERE username = ?
                        """,
                        (self._now(), "admin"),
                    )
                conn.commit()
            finally:
                conn.close()

    @classmethod
    def hash_password(cls, password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            cls.HASH_ALGORITHM,
            password.encode("utf-8"),
            salt,
            cls.HASH_ITERATIONS,
        )
        return "pbkdf2_{algorithm}${iterations}${salt}${digest}".format(
            algorithm=cls.HASH_ALGORITHM,
            iterations=cls.HASH_ITERATIONS,
            salt=base64.urlsafe_b64encode(salt).decode("ascii"),
            digest=base64.urlsafe_b64encode(digest).decode("ascii"),
        )

    @classmethod
    def verify_password(cls, password: str, encoded: str) -> bool:
        try:
            algorithm, iterations_text, salt_text, digest_text = str(encoded).split("$", 3)
            if not algorithm.startswith("pbkdf2_"):
                return False
            digest_algorithm = algorithm[len("pbkdf2_"):]
            iterations = int(iterations_text)
            salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
            expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
            actual = hashlib.pbkdf2_hmac(
                digest_algorithm, password.encode("utf-8"), salt, iterations
            )
            return hmac.compare_digest(actual, expected)
        except (TypeError, ValueError, UnicodeError):
            return False

    @classmethod
    def _text(cls, value: Any, field: str, maximum: int, *, required: bool = True) -> str:
        text = str(value or "").strip()
        if required and not text:
            raise AuthError("invalid_input", f"{field}不能为空")
        if len(text) > maximum:
            raise AuthError("invalid_input", f"{field}不能超过{maximum}个字符")
        if any(ord(char) < 32 for char in text):
            raise AuthError("invalid_input", f"{field}包含无效控制字符")
        return text

    @classmethod
    def _password(cls, value: Any) -> str:
        password = str(value or "")
        if len(password) < 6:
            raise AuthError("invalid_input", "密码至少需要6位")
        if len(password) > cls.MAX_PASSWORD_LENGTH:
            raise AuthError("invalid_input", "密码不能超过256位")
        return password

    @staticmethod
    def _public_user(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "username": str(row["username"]),
            "employee_name": str(row["employee_name"] or ""),
            "role": str(row["role"] or "employee"),
            "status": str(row["status"] or "pending"),
            "created_at": str(row["created_at"] or ""),
            "approved_at": str(row["approved_at"] or ""),
            "last_login_at": str(row["last_login_at"] or ""),
            "plan_code": str(row["plan_code"] or "NORMAL"),
            "plan_expires_at": str(row["plan_expires_at"] or "") or None,
            "entitlements": json.loads(row["entitlements_json"] or "{}"),
        }

    def get_entitlements(self, user_id: int) -> dict[str, Any]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT plan_code, plan_expires_at, entitlements_json FROM app_users WHERE id = ?", (int(user_id),)).fetchone()
            if row is None:
                raise AuthError("not_found", "账号不存在")
            return {"user_id": int(user_id), "plan_code": row["plan_code"] or "NORMAL", "plan_expires_at": row["plan_expires_at"], "overrides": json.loads(row["entitlements_json"] or "{}")}
        finally:
            conn.close()

    def update_entitlements(self, user_id: int, *, plan_code: str | None = None, plan_expires_at: str | None = None, feature_key: str | None = None, feature: Mapping[str, Any] | None = None, delete_feature: bool = False) -> dict[str, Any]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM app_users WHERE id = ?", (int(user_id),)).fetchone()
            if row is None:
                raise AuthError("not_found", "账号不存在")
            overrides = json.loads(row["entitlements_json"] or "{}")
            if feature_key:
                if delete_feature:
                    overrides.pop(feature_key, None)
                else:
                    overrides[feature_key] = dict(feature or {})
            conn.execute("UPDATE app_users SET plan_code = ?, plan_expires_at = ?, entitlements_json = ?, updated_at = ? WHERE id = ?", (plan_code or row["plan_code"] or "NORMAL", plan_expires_at if plan_expires_at is not None else row["plan_expires_at"], json.dumps(overrides, ensure_ascii=False), self._now(), int(user_id)))
            conn.commit()
            return self.get_entitlements(user_id)
        finally:
            conn.close()

    def register(self, username: Any, password: Any, employee_name: Any, invite_code: Any) -> dict[str, Any]:
        username = self._text(username, "账号", self.MAX_USERNAME_LENGTH)
        password = self._password(password)
        employee_name = self._text(
            employee_name, "员工姓名", self.MAX_EMPLOYEE_NAME_LENGTH
        )
        invite_code = normalize_invite_code(invite_code)
        if len(invite_code) < 8:
            raise AuthError("invalid_invite", "邀请码无效，请向管理员索取有效邀请码")
        now = self._now()
        conn = self._connect()
        try:
            invite = conn.execute(
                "SELECT id, status FROM app_invites WHERE code_hash = ?",
                (hash_invite_code(invite_code),),
            ).fetchone()
            if invite is None:
                raise AuthError("invalid_invite", "邀请码无效，请向管理员索取有效邀请码")
            if str(invite["status"] or "") != "active":
                message = {"used": "邀请码已使用", "revoked": "邀请码已撤销"}.get(
                    str(invite["status"] or ""), "邀请码不可用"
                )
                raise AuthError("invalid_invite", message)
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO app_users
                      (username, password_hash, employee_name, role, status,
                       created_at, updated_at)
                    VALUES (?, ?, ?, 'employee', 'pending', ?, ?)
                    """,
                    (username, self.hash_password(password), employee_name, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise AuthError("username_exists", "该账号已存在") from exc
            conn.execute(
                "UPDATE app_invites SET status='used', used_at=?, used_by=? WHERE id=? AND status='active'",
                (now, cursor.lastrowid, int(invite["id"])),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM app_users WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return self._public_user(row)
        finally:
            conn.close()

    def sync_invites(self, items: Any) -> list[dict[str, Any]]:
        """Upsert invite hashes sent by an administrator client."""
        if not isinstance(items, list) or len(items) > 100:
            raise AuthError("invalid_input", "邀请码数据无效")
        now = self._now()
        conn = self._connect()
        try:
            for item in items:
                if not isinstance(item, Mapping):
                    raise AuthError("invalid_input", "邀请码数据无效")
                code = normalize_invite_code(item.get("code"))
                if len(code) < 8:
                    raise AuthError("invalid_input", "邀请码格式无效")
                status = str(item.get("status") or "active").strip().lower()
                if status not in {"active", "used", "revoked", "expired"}:
                    raise AuthError("invalid_input", "邀请码状态无效")
                preview = code[:4] + "-" + code[4:8] + ("-..." if len(code) > 8 else "")
                conn.execute(
                    """
                    INSERT INTO app_invites(code_hash, code_preview, status, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(code_hash) DO UPDATE SET status=excluded.status
                    """,
                    (hash_invite_code(code), preview, status, now),
                )
            conn.commit()
            rows = conn.execute(
                "SELECT id, code_preview, status, created_at, used_at, used_by FROM app_invites ORDER BY id DESC"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def authenticate(self, username: Any, password: Any) -> dict[str, Any]:
        username = self._text(username, "账号", self.MAX_USERNAME_LENGTH)
        password = str(password or "")
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM app_users WHERE username = ?", (username,)
            ).fetchone()
            if row is None or not self.verify_password(password, row["password_hash"]):
                raise AuthError("invalid_credentials", "账号或密码错误")
            status = str(row["status"] or "")
            if status == "pending":
                raise AuthError("account_pending", "账号正在等待管理员审批")
            if status == "rejected":
                raise AuthError("account_rejected", "账号注册申请未通过")
            if status == "disabled":
                raise AuthError("account_disabled", "账号已被管理员禁用")
            now = self._now()
            conn.execute(
                "UPDATE app_users SET last_login_at = ?, updated_at = ? WHERE id = ?",
                (now, now, int(row["id"])),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM app_users WHERE id = ?", (int(row["id"]),)
            ).fetchone()
            return self._public_user(row)
        finally:
            conn.close()

    @staticmethod
    def _session_hash(token: Any) -> str:
        value = str(token or "").strip()
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def create_session(self, user_id: int) -> str:
        """创建持久登录会话，返回只给客户端保存的原始令牌。"""
        user_id = int(user_id)
        token = secrets.token_urlsafe(48)
        now = datetime.now().astimezone()
        expires = now + timedelta(days=self.SESSION_TTL_DAYS)
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO app_sessions "
                "(token_hash, user_id, created_at, last_seen_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (self._session_hash(token), user_id,
                 now.isoformat(timespec="seconds"),
                 now.isoformat(timespec="seconds"),
                 expires.isoformat(timespec="seconds")),
            )
            conn.commit()
            return token
        finally:
            conn.close()

    def get_session_user(self, token: Any) -> dict[str, Any] | None:
        """校验会话并返回当前审批状态的用户；失效会话不会恢复登录。"""
        raw = str(token or "").strip()
        if len(raw) < 24:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT s.*, u.* FROM app_sessions s "
                "JOIN app_users u ON u.id = s.user_id "
                "WHERE s.token_hash = ? AND s.revoked_at IS NULL",
                (self._session_hash(raw),),
            ).fetchone()
            if row is None:
                return None
            try:
                expires = datetime.fromisoformat(str(row["expires_at"]))
                current = datetime.now().astimezone()
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=current.tzinfo)
                if expires <= current:
                    conn.execute(
                        "UPDATE app_sessions SET revoked_at = ? WHERE token_hash = ?",
                        (current.isoformat(timespec="seconds"), self._session_hash(raw)),
                    )
                    conn.commit()
                    return None
            except (TypeError, ValueError):
                return None
            if str(row["status"] or "") != "approved":
                return None
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            conn.execute(
                "UPDATE app_sessions SET last_seen_at = ? WHERE token_hash = ?",
                (now, self._session_hash(raw)),
            )
            conn.commit()
            return self._public_user(row)
        finally:
            conn.close()

    def revoke_session(self, token: Any) -> None:
        raw = str(token or "").strip()
        if not raw:
            return
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE app_sessions SET revoked_at = ? "
                "WHERE token_hash = ? AND revoked_at IS NULL",
                (self._now(), self._session_hash(raw)),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _session_public(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]), "account_key": str(row["account_key"]),
            "account_name": str(row["account_name"]), "device_id": str(row["device_id"]),
            "metadata": json.loads(row["metadata_json"] or "{}"),
            "revision": int(row["revision"] or 1), "status": str(row["status"] or "active"),
            "last_validated_at": row["last_validated_at"], "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_account_sessions(self, owner_user_id: int) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM app_account_sessions WHERE owner_user_id = ? ORDER BY id DESC", (int(owner_user_id),)).fetchall()
            return [self._session_public(row) for row in rows]
        finally:
            conn.close()

    def save_account_session(self, owner_user_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        account_key = self._text(payload.get("account_key"), "账号标识", 128)
        account_name = self._text(payload.get("account_name") or account_key, "账号名称", 64)
        session_payload = payload.get("session_payload")
        if not isinstance(session_payload, Mapping):
            raise AuthError("invalid_input", "会话内容无效")
        try:
            ciphertext = self._session_fernet().encrypt(json.dumps(dict(session_payload), ensure_ascii=False).encode("utf-8")).decode("ascii")
        except (TypeError, ValueError) as exc:
            raise AuthError("invalid_input", "会话内容无法加密") from exc
        device_id = self._text(payload.get("device_id"), "设备标识", 128)
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
        now = self._now()
        conn = self._connect()
        try:
            row = conn.execute("SELECT id, revision FROM app_account_sessions WHERE owner_user_id = ? AND account_key = ?", (int(owner_user_id), account_key)).fetchone()
            if row is None:
                conn.execute("INSERT INTO app_account_sessions(owner_user_id, account_key, account_name, ciphertext, device_id, metadata_json, revision, last_validated_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)", (int(owner_user_id), account_key, account_name, ciphertext, device_id, json.dumps(metadata, ensure_ascii=False), now, now, now))
            else:
                conn.execute("UPDATE app_account_sessions SET account_name=?, ciphertext=?, device_id=?, metadata_json=?, revision=?, status='active', last_validated_at=?, updated_at=? WHERE id=? AND owner_user_id=?", (account_name, ciphertext, device_id, json.dumps(metadata, ensure_ascii=False), int(row["revision"] or 1) + 1, now, now, int(row["id"]), int(owner_user_id)))
            conn.commit()
            saved = conn.execute("SELECT * FROM app_account_sessions WHERE owner_user_id=? AND account_key=?", (int(owner_user_id), account_key)).fetchone()
            return self._session_public(saved)
        finally:
            conn.close()

    def get_account_session(self, owner_user_id: int, session_id: int) -> dict[str, Any]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM app_account_sessions WHERE owner_user_id=? AND id=? AND status='active'", (int(owner_user_id), int(session_id))).fetchone()
            if row is None:
                raise AuthError("not_found", "云端登录会话不存在")
            result = self._session_public(row)
            try:
                result["session_payload"] = json.loads(self._session_fernet().decrypt(str(row["ciphertext"]).encode("ascii")).decode("utf-8"))
            except (InvalidToken, ValueError, UnicodeError) as exc:
                raise AuthError("configuration_error", "云端会话无法解密") from exc
            return result
        finally:
            conn.close()

    def delete_account_session(self, owner_user_id: int, session_id: int) -> None:
        conn = self._connect()
        try:
            conn.execute("UPDATE app_account_sessions SET status='revoked', updated_at=? WHERE owner_user_id=? AND id=?", (self._now(), int(owner_user_id), int(session_id)))
            conn.commit()
        finally:
            conn.close()

    def get(self, user_id: int) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM app_users WHERE id = ?", (int(user_id),)).fetchone()
            return self._public_user(row) if row else None
        finally:
            conn.close()

    def get_by_username(self, username: Any) -> dict[str, Any] | None:
        """按账号读取本机镜像用户。远程认证成功后用它绑定本地数据归属。"""
        username = self._text(username, "账号", self.MAX_USERNAME_LENGTH)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM app_users WHERE username = ?", (username,)
            ).fetchone()
            return self._public_user(row) if row else None
        finally:
            conn.close()

    def ensure_external_user(self, user: Mapping[str, Any], password: Any = None) -> dict[str, Any]:
        """把统一账号服务返回的用户镜像到本机，保留本机数据归属 ID。

        远程账号 ID 不能直接当作每台电脑 SQLite 的 owner_user_id：不同电脑
        可能已有同号的历史本地账号。这里按用户名更新/创建本机镜像，避免
        新员工登录后误读旧员工数据，同时让已有本地数据继续归属于原账号。
        """
        if not isinstance(user, Mapping):
            raise AuthError("invalid_user", "统一账号服务返回的用户信息无效")
        username = self._text(user.get("username"), "账号", self.MAX_USERNAME_LENGTH)
        employee_name = self._text(
            user.get("employee_name"), "员工姓名", self.MAX_EMPLOYEE_NAME_LENGTH,
            required=False,
        )
        role = str(user.get("role") or "employee").strip().lower()
        status = str(user.get("status") or "pending").strip().lower()
        if role not in {"admin", "employee"}:
            role = "employee"
        if status not in {"pending", "approved", "rejected", "disabled"}:
            status = "pending"
        encoded_password = self.hash_password(
            self._password(password) if password is not None else secrets.token_urlsafe(32)
        )
        approved_at = str(user.get("approved_at") or "") or None
        now = self._now()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM app_users WHERE username = ?", (username,)
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO app_users
                      (username, password_hash, employee_name, role, status,
                       created_at, approved_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (username, encoded_password, employee_name, role, status,
                     str(user.get("created_at") or now), approved_at, now),
                )
            else:
                # 本机 admin 是保留账号；远程服务不能把它降级为员工。
                if str(row["username"]).casefold() == "admin":
                    role = "admin"
                    status = "approved"
                conn.execute(
                    """
                    UPDATE app_users
                       SET password_hash = ?, employee_name = ?, role = ?, status = ?,
                           approved_at = ?, updated_at = ?
                     WHERE id = ?
                    """,
                    (encoded_password, employee_name, role, status, approved_at, now,
                     int(row["id"])),
                )
            conn.commit()
            current = conn.execute(
                "SELECT * FROM app_users WHERE username = ?", (username,)
            ).fetchone()
            return self._public_user(current)
        finally:
            conn.close()

    def list_users(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM app_users ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, id"
            ).fetchall()
            return [self._public_user(row) for row in rows]
        finally:
            conn.close()

    def _set_status(self, user_id: int, status: str) -> dict[str, Any]:
        if status not in {"approved", "rejected", "disabled"}:
            raise AuthError("invalid_input", "无效的账号状态")
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM app_users WHERE id = ?", (int(user_id),)).fetchone()
            if row is None:
                raise AuthError("not_found", "账号不存在")
            if str(row["username"]).casefold() == "admin":
                raise AuthError("forbidden", "不能修改管理员账号状态")
            now = self._now()
            approved_at = now if status == "approved" else row["approved_at"]
            conn.execute(
                "UPDATE app_users SET status = ?, approved_at = ?, updated_at = ? WHERE id = ?",
                (status, approved_at, now, int(user_id)),
            )
            conn.commit()
            return self._public_user(
                conn.execute("SELECT * FROM app_users WHERE id = ?", (int(user_id),)).fetchone()
            )
        finally:
            conn.close()

    def approve(self, user_id: int) -> dict[str, Any]:
        return self._set_status(user_id, "approved")

    def reject(self, user_id: int) -> dict[str, Any]:
        return self._set_status(user_id, "rejected")

    def disable(self, user_id: int) -> dict[str, Any]:
        return self._set_status(user_id, "disabled")

    def reset_password(self, user_id: int, password: Any) -> dict[str, Any]:
        password = self._password(password)
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM app_users WHERE id = ?", (int(user_id),)).fetchone()
            if row is None:
                raise AuthError("not_found", "账号不存在")
            now = self._now()
            conn.execute(
                "UPDATE app_users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (self.hash_password(password), now, int(user_id)),
            )
            conn.commit()
            return self._public_user(
                conn.execute("SELECT * FROM app_users WHERE id = ?", (int(user_id),)).fetchone()
            )
        finally:
            conn.close()


__all__ = ["AuthError", "AuthStore"]
