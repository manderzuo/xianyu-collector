# -*- coding: utf-8 -*-
"""状态分离、续期冷却与任务状态真实性的回归测试。

覆盖四类缺陷：

1. **死锁**：账号被标记 ``expired`` 后，网页侧同步任务整个跳过它，
   导致 ``sync_account_products`` 内部的自愈逻辑永久不可达。
2. **状态混淆**：续期已成功（登录态有效）但 IM Token 取不到时，
   早期实现把整个账号写成 ``status = "expired"``，
   把「聊天链路故障」放大成「整账号停摆」。
3. **静默成功**：处理 0 个账号却报 ``completed``。
4. **验证类失败误判**：滑块/人脸/punish 被当成「登录态失效」。
"""
from __future__ import annotations

import asyncio
import inspect
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from common.models.accounts import Account
from common.services.account_renewal import (
    COOLDOWN_EXEMPT_SOURCES,
    RENEWAL_ATTEMPT_COOLDOWN_MINUTES,
    renewal_cooldown_remaining,
    renewal_in_cooldown,
)
from common.services.cookie_renewal import (
    VERIFICATION_MARKERS,
    describe_failure,
    is_session_expired_message,
    requires_browser_recovery_message,
    requires_verification_message,
)


def _account(**overrides) -> Account:
    base = {
        "id": 7, "user_id": 1, "account_name": "账号", "status": "active",
        "cookie": "unb=2223021364297; cookie2=abc",
    }
    base.update(overrides)
    return Account(**base)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# BUG-1：死锁——expired 账号必须仍能被网页侧同步覆盖
# --------------------------------------------------------------------------- #

class SyncIncludesExpiredTests(unittest.TestCase):
    """同步任务必须包含 expired 账号，否则自愈逻辑不可达。"""

    def _sync_source(self) -> str:
        from scheduler.app.jobs import account_sync

        return inspect.getsource(account_sync.execute_account_sync)

    def test_sync_job_includes_expired_accounts(self):
        source = self._sync_source()
        self.assertIn(
            'in_(["active", "expired"])', source,
            "同步任务仍在按 status == 'active' 过滤，expired 账号的自愈路径不可达",
        )

    def test_sync_job_no_longer_filters_active_only(self):
        source = self._sync_source()
        self.assertNotIn(
            'Account.status == "active"', source,
            "同步任务不应只选 active",
        )

    def test_self_healing_path_exists_in_sync_service(self):
        """前提校验：sync_account_products 确实带“过期→续期→重试”。"""
        from common.services import account_sync

        source = inspect.getsource(account_sync.sync_account_products)
        self.assertIn("renew_account_session", source)
        self.assertIn("observed_session_expired=True", source)


class RenewalCooldownTests(unittest.TestCase):
    """冷却窗口：让新增的 expired 同步不会变成高频打 Passport。"""

    def test_no_attempt_means_no_cooldown(self):
        self.assertEqual(renewal_cooldown_remaining(_account()), 0)

    def test_recent_attempt_is_in_cooldown(self):
        account = _account(last_renewal_attempt_at=_now() - timedelta(minutes=1))
        self.assertGreater(renewal_cooldown_remaining(account), 0)
        self.assertTrue(renewal_in_cooldown(account, source="scheduled_task", force=True))

    def test_old_attempt_is_out_of_cooldown(self):
        account = _account(
            last_renewal_attempt_at=_now() - timedelta(minutes=RENEWAL_ATTEMPT_COOLDOWN_MINUTES + 1)
        )
        self.assertEqual(renewal_cooldown_remaining(account), 0)
        self.assertFalse(renewal_in_cooldown(account, source="scheduled_task", force=True))

    def test_manual_renewal_bypasses_cooldown(self):
        """用户手动点续期不能被冷却挡住，否则界面“点了没反应”。"""
        account = _account(last_renewal_attempt_at=_now())
        self.assertFalse(renewal_in_cooldown(account, source="manual", force=True))

    def test_runtime_renewal_bypasses_cooldown(self):
        """运行时自带冷却，不再叠加一层，避免会话失效后长时间无法恢复。"""
        account = _account(last_renewal_attempt_at=_now())
        self.assertFalse(renewal_in_cooldown(account, source="runtime", force=True))

    def test_non_forced_renewal_is_not_blocked(self):
        account = _account(last_renewal_attempt_at=_now())
        self.assertFalse(renewal_in_cooldown(account, source="scheduled_task", force=False))

    def test_exempt_sources_are_declared(self):
        self.assertIn("manual", COOLDOWN_EXEMPT_SOURCES)
        self.assertIn("runtime", COOLDOWN_EXEMPT_SOURCES)

    def test_aware_timestamp_is_handled(self):
        """带时区的时间戳不能因为时区比较而抛异常。"""
        account = _account(last_renewal_attempt_at=datetime.now(timezone.utc))
        self.assertGreaterEqual(renewal_cooldown_remaining(account), 0)


# --------------------------------------------------------------------------- #
# BUG-2：IM 失败不得写成登录态失效
# --------------------------------------------------------------------------- #

class ImStatusSeparationTests(unittest.TestCase):
    def _runtime_source(self) -> str:
        from websocket.app.xianyu_runtime import AccountRuntimeManager

        return inspect.getsource(AccountRuntimeManager._renew_runtime_account_locked)

    def test_im_token_failure_does_not_mark_login_expired(self):
        source = self._runtime_source()
        # 续期成功但 IM 验收失败的分支里，不得再出现 account.status = "expired"
        marker = 'account.im_status = "error"'
        self.assertIn(marker, source, "IM Token 失败时应写 im_status 而不是 status")
        self.assertNotIn(
            'account.status = "expired"\n                        account.cookie_expire_at',
            source,
            "IM Token 失败仍然把登录态写成 expired",
        )

    def test_im_failure_message_says_login_is_fine(self):
        source = self._runtime_source()
        self.assertIn("登录态有效", source, "提示必须说明登录态仍有效")

    def test_successful_verify_marks_im_connected(self):
        source = self._runtime_source()
        self.assertIn('account.im_status = "connected"', source)

    def test_account_model_declares_im_status(self):
        self.assertIn("im_status", Account.__table__.columns)

    def test_account_model_declares_renewal_timestamps(self):
        for column in ("last_renewal_attempt_at", "cookie_last_renewed_at", "cookie_next_renewal_at"):
            self.assertIn(column, Account.__table__.columns)

    def test_im_status_defaults_to_unknown(self):
        column = Account.__table__.columns["im_status"]
        self.assertEqual(column.default.arg, "unknown")

    def test_serialize_exposes_im_status(self):
        from backend.app.api.routes.accounts import serialize

        data = serialize(_account(im_status="error"))
        self.assertEqual(data["im_status"], "error")
        self.assertEqual(data["status"], "active")
        self.assertIn("cookie_last_renewed_at", data)

    def test_serialize_survives_missing_optional_columns(self):
        """旧对象缺少新列时序列化不应崩溃。"""
        from backend.app.api.routes.accounts import serialize

        plain = Account(id=1, user_id=1, account_name="a", status="active")
        data = serialize(plain)
        self.assertEqual(data["im_status"], "unknown")


class MarkLoginExpiredTests(unittest.IsolatedAsyncioTestCase):
    async def test_mark_login_expired_sets_both_status_and_im_status(self):
        """登录态确认失效时，status 与 im_status 一起写入。"""
        from websocket.app.xianyu_runtime import AccountRuntime, AccountRuntimeManager

        runtime = AccountRuntime(account_id="7", user_id=1, cookie_value="unb=1")
        account = _account()

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, _query):
                result = MagicMock()
                result.scalar_one_or_none.return_value = account
                return result

            async def commit(self):
                return None

        manager = AccountRuntimeManager()
        with patch(
            "websocket.app.xianyu_runtime.async_session_maker", return_value=_Session()
        ):
            await manager._mark_login_expired(runtime)

        self.assertEqual(account.status, "expired")
        self.assertEqual(account.im_status, "expired")

    async def test_persist_im_status_does_not_touch_login_status(self):
        from websocket.app.xianyu_runtime import AccountRuntime, AccountRuntimeManager

        runtime = AccountRuntime(account_id="7", user_id=1, cookie_value="unb=1")
        account = _account()

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, _query):
                result = MagicMock()
                result.scalar_one_or_none.return_value = account
                return result

            async def commit(self):
                return None

        manager = AccountRuntimeManager()
        with patch(
            "websocket.app.xianyu_runtime.async_session_maker", return_value=_Session()
        ):
            await manager._persist_im_status(runtime, "error")

        self.assertEqual(account.im_status, "error")
        self.assertEqual(account.status, "active", "IM 状态写入不得影响登录态")


# --------------------------------------------------------------------------- #
# BUG-3：静默成功
# --------------------------------------------------------------------------- #

class SilentSuccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_cookie_renewal_reports_skipped_when_no_accounts(self):
        from scheduler.app.jobs.account_renewal import execute_cookie_renewal

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, _query):
                result = MagicMock()
                result.scalars.return_value.all.return_value = []
                return result

        with patch(
            "scheduler.app.jobs.account_renewal.async_session_maker", return_value=_Session()
        ):
            result = await execute_cookie_renewal()

        self.assertEqual(result["status"], "skipped", "0 个账号不能报 completed")
        self.assertIn("没有可处理的账号", result["detail"])

    async def test_token_refresh_reports_skipped_when_no_accounts(self):
        from scheduler.app.jobs.account_renewal import execute_token_refresh

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, _query):
                result = MagicMock()
                result.scalars.return_value.all.return_value = []
                return result

        with patch(
            "scheduler.app.jobs.account_renewal.async_session_maker", return_value=_Session()
        ):
            result = await execute_token_refresh()

        self.assertEqual(result["status"], "skipped")
        self.assertIn("没有可处理的账号", result["detail"])

    async def test_sync_reports_skipped_when_no_accounts(self):
        from scheduler.app.jobs.account_sync import execute_account_sync

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, _query):
                result = MagicMock()
                result.scalars.return_value.all.return_value = []
                return result

        with patch(
            "scheduler.app.jobs.account_sync.async_session_maker", return_value=_Session()
        ):
            result = await execute_account_sync("products")

        self.assertEqual(result["status"], "skipped")
        self.assertIn("没有可处理的账号", result["detail"])

    def test_scheduler_treats_skipped_as_non_failure(self):
        """skipped 必须被调度器视为正常结束，否则会刷错误。"""
        from scheduler import main as scheduler_main

        source = inspect.getsource(scheduler_main)
        self.assertIn('{"completed", "success", "skipped"}', source)


# --------------------------------------------------------------------------- #
# BUG-4：验证类失败与登录态失效区分
# --------------------------------------------------------------------------- #

class VerificationClassificationTests(unittest.TestCase):
    def test_punish_and_captcha_are_detected(self):
        for text in (
            "https://xxxx_____tmd_____/punish?x5secdata=abc",
            "登录触发闲鱼安全验证，请完成滑块或人脸验证",
            "请完成验证",
            "verification_required",
        ):
            self.assertTrue(requires_verification_message(text), text)

    def test_plain_session_expiry_is_not_verification(self):
        self.assertFalse(requires_verification_message("FAIL_SYS_SESSION_EXPIRED::Session过期"))

    def test_describe_failure_guides_to_verification(self):
        message = describe_failure("punish 安全验证")
        self.assertIn("安全验证", message)
        self.assertNotIn("扫码", message, "验证类失败不应提示重新扫码")

    def test_describe_failure_guides_to_rescan_on_expiry(self):
        message = describe_failure("FAIL_SYS_SESSION_EXPIRED::Session过期")
        self.assertIn("扫码", message)

    def test_describe_failure_handles_device_validation(self):
        message = describe_failure("FAIL_SYS_USER_VALIDATE::哎哟喂")
        self.assertIn("设备安全验证", message)

    def test_punish_does_not_break_session_expiry_detection(self):
        """punish 仍应触发恢复流程，只是分类不同。"""
        self.assertTrue(is_session_expired_message("FAIL_SYS_USER_VALIDATE::被挤爆啦"))

    def test_verification_markers_cover_reference_findings(self):
        # 参考实现明确点名的两个 punish 特征
        self.assertIn("_____tmd_____", VERIFICATION_MARKERS)
        self.assertIn("punish", VERIFICATION_MARKERS)

    def test_device_validation_still_requires_browser_recovery(self):
        self.assertTrue(requires_browser_recovery_message("FAIL_SYS_USER_VALIDATE"))


class AccountMigrationTests(unittest.TestCase):
    """旧库补列必须幂等，且覆盖 1.3.8 与 1.4.0 的全部新列。"""

    def _run_migration(self, existing_columns):
        from common.db import session as session_module

        class _Connection:
            def __init__(self):
                self.statements = []

            def execute(self, statement):
                self.statements.append(str(statement))

        class _Inspector:
            def get_table_names(self):
                return ["xr_accounts"]

            def get_columns(self, _table):
                return [{"name": name} for name in existing_columns]

        connection = _Connection()

        def _fake_text(sql):
            return sql

        with patch.object(session_module, "sqlalchemy_inspect", return_value=_Inspector()), patch.object(
            session_module, "text", side_effect=_fake_text
        ):
            session_module._migrate_account_columns(connection)

        return connection.statements

    def test_all_new_columns_are_added_on_old_schema(self):
        statements = self._run_migration(
            ["id", "user_id", "account_name", "goofish_id", "cookie", "status"]
        )
        joined = " ".join(statements)
        for column in (
            "im_device_id",
            "im_status",
            "last_renewal_attempt_at",
            "cookie_last_renewed_at",
            "cookie_next_renewal_at",
        ):
            self.assertIn(column, joined, f"迁移未补列 {column}")

    def test_im_status_is_not_null_with_default(self):
        statements = self._run_migration([])
        target = [s for s in statements if "im_status" in s]
        self.assertEqual(len(target), 1)
        self.assertIn("NOT NULL", target[0])
        self.assertIn("DEFAULT 'unknown'", target[0])

    def test_migration_is_idempotent(self):
        """已有全部列时必须一条 ALTER 都不发，重复启动不会报错。"""
        statements = self._run_migration([
            "id", "user_id", "account_name", "goofish_id", "cookie", "proxy",
            "status", "cookie_expire_at", "created_at", "updated_at",
            "im_device_id", "im_status", "last_renewal_attempt_at",
            "cookie_last_renewed_at", "cookie_next_renewal_at",
        ])
        self.assertEqual(statements, [], "已存在列不应重复 ALTER")

    def test_missing_table_is_skipped(self):
        from common.db import session as session_module

        class _Inspector:
            def get_table_names(self):
                return ["xr_users"]

            def get_columns(self, _table):  # pragma: no cover - 不应被调用
                raise AssertionError("表不存在时不应查询列")

        class _Connection:
            def __init__(self):
                self.statements = []

            def execute(self, statement):
                self.statements.append(str(statement))

        connection = _Connection()
        with patch.object(session_module, "sqlalchemy_inspect", return_value=_Inspector()), patch.object(
            session_module, "text", side_effect=lambda sql: sql
        ):
            session_module._migrate_account_columns(connection)
        self.assertEqual(connection.statements, [])


if __name__ == "__main__":
    unittest.main()
