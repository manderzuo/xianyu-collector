"""套餐权限接口的回归测试。

背景：`/api/v1/admin/entitlements/*` 曾因未在 backend/main.py 注册而被
legacy_surface 兜底路由吞掉，前端“套餐权限”页面因此永远拿不到套餐数据。
本文件用断言锁定路由注册，防止同类问题再次出现。
"""
import unittest
from datetime import datetime
from types import SimpleNamespace

from fastapi import HTTPException

from backend.app.api.routes.admin_entitlements import (
    KNOWN_FEATURE_KEYS,
    _override_summary,
    _normalize_override,
)
from backend.app.api.routes.auth import normalize_cloud_overrides


def _registered_paths() -> set[str]:
    from backend.main import app

    return {getattr(route, "path", "") for route in app.routes}


class AdminEntitlementRouteTests(unittest.TestCase):
    def test_entitlement_routes_are_registered_before_legacy_fallback(self):
        paths = _registered_paths()
        required = {
            "/api/v1/admin/entitlements/plans",
            "/api/v1/admin/entitlements/plans/{plan_code}",
            "/api/v1/admin/entitlements/plans/{plan_code}/features/{feature_key:path}",
            "/api/v1/admin/entitlements/users/{target_user_id}",
            "/api/v1/admin/entitlements/users/{target_user_id}/plan",
            "/api/v1/admin/entitlements/users/{target_user_id}/features/{feature_key:path}",
        }
        missing = required - paths
        self.assertEqual(missing, set(), f"套餐权限接口未注册：{sorted(missing)}")

    def test_entitlement_routes_win_over_catch_all(self):
        from backend.main import app

        # 兜底路由是 /api/v1/{full_path:path}，套餐权限接口必须排在它之前，
        # 否则请求会被 legacy_surface 当作 FeatureRecord 记录处理。
        order = [getattr(route, "path", "") for route in app.routes]
        fallback_index = order.index("/api/v1/{full_path:path}")
        plans_index = order.index("/api/v1/admin/entitlements/plans")
        self.assertLess(plans_index, fallback_index)


class NormalizeOverrideTests(unittest.TestCase):
    def test_known_feature_is_accepted_and_normalized(self):
        values = _normalize_override("ai.builtin_reply", {"enabled": True, "unlimited": False, "limit": 12, "reason": " 试用 "})
        self.assertEqual(values["enabled"], True)
        self.assertEqual(values["limit"], 12)
        self.assertEqual(values["unlimited"], False)
        self.assertEqual(values["reason"], "试用")

    def test_unknown_feature_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            _normalize_override("ai.unknown_feature", {"enabled": True})
        self.assertEqual(ctx.exception.status_code, 422)

    def test_negative_limit_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            _normalize_override("account.manage", {"limit": -5})
        self.assertEqual(ctx.exception.status_code, 422)

    def test_non_boolean_enabled_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            _normalize_override("account.manage", {"enabled": "yes"})
        self.assertEqual(ctx.exception.status_code, 422)

    def test_every_supported_feature_is_known(self):
        for key in ("account.manage", "card.auto_delivery", "product.auto_publish", "ai.smart_reply", "ai.builtin_reply", "keyword.reply"):
            self.assertIn(key, KNOWN_FEATURE_KEYS)


class OverrideSummaryTests(unittest.TestCase):
    def test_cloud_dictionary_payload_becomes_list(self):
        items = _override_summary({"ai.builtin_reply": {"enabled": True, "limit": None}})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["feature_key"], "ai.builtin_reply")
        self.assertTrue(items[0]["enabled"])

    def test_local_list_payload_is_preserved(self):
        items = _override_summary([{"feature_key": "account.manage", "limit": 3}])
        self.assertEqual(items, [{"feature_key": "account.manage", "limit": 3}])

    def test_empty_payload_returns_empty_list(self):
        self.assertEqual(_override_summary({}), [])
        self.assertEqual(_override_summary([]), [])


class CloudOverrideMirrorTests(unittest.TestCase):
    """云端 entitlements_json → 本机覆盖表的规范化（登录时镜像用）。"""

    def test_known_feature_fields_are_kept(self):
        values = normalize_cloud_overrides({
            "ai.builtin_reply": {"enabled": True, "unlimited": False, "limit": 8, "reason": "活动"},
        })
        self.assertEqual(values["ai.builtin_reply"]["enabled"], True)
        self.assertEqual(values["ai.builtin_reply"]["limit_value"], 8)
        self.assertEqual(values["ai.builtin_reply"]["reason"], "活动")

    def test_unknown_feature_is_ignored(self):
        self.assertEqual(normalize_cloud_overrides({"ai.not_supported": {"enabled": True}}), {})

    def test_non_mapping_payload_is_ignored(self):
        self.assertEqual(normalize_cloud_overrides(None), {})
        self.assertEqual(normalize_cloud_overrides([{"enabled": True}]), {})
        self.assertEqual(normalize_cloud_overrides({"ai.builtin_reply": True}), {})

    def test_invalid_values_are_dropped(self):
        values = normalize_cloud_overrides({
            "account.manage": {"enabled": "yes", "unlimited": 1, "limit": -3, "expires_at": "not-a-date"},
        })
        self.assertEqual(values, {"account.manage": {}})

    def test_expires_at_is_parsed_to_naive_datetime(self):
        values = normalize_cloud_overrides({"keyword.reply": {"enabled": True, "expires_at": "2030-01-02T03:04:05"}})
        parsed = values["keyword.reply"]["expires_at"]
        self.assertIsInstance(parsed, datetime)
        self.assertIsNone(parsed.tzinfo)


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _ScalarsResult(self._rows)


class _RecordingSession:
    """记录 add/delete 的假会话，用于验证云端覆盖镜像行为。"""

    def __init__(self, rows):
        self.rows = rows
        self.added = []
        self.deleted = []

    async def execute(self, _query):
        return _Result(self.rows)

    def add(self, record):
        self.added.append(record)

    async def delete(self, record):
        self.deleted.append(record)


class OverrideRow:
    def __init__(self, feature_key, **values):
        self.feature_key = feature_key
        self.enabled = values.get("enabled")
        self.unlimited = values.get("unlimited")
        self.limit_value = values.get("limit_value")
        self.expires_at = values.get("expires_at")
        self.reason = values.get("reason")


class CloudOverrideSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_cloud_override_is_written_and_stale_row_is_pruned(self):
        from backend.app.api.routes.auth import _sync_cloud_overrides

        stale = OverrideRow("keyword.reply", enabled=True)
        session = _RecordingSession([stale])
        record = SimpleNamespace(id=7, auth_version=3)

        await _sync_cloud_overrides(session, record, {"ai.builtin_reply": {"enabled": True, "limit": 4}})

        self.assertEqual(session.deleted, [stale])
        self.assertEqual(len(session.added), 1)
        created = session.added[0]
        self.assertEqual(created.feature_key, "ai.builtin_reply")
        self.assertEqual(created.user_id, 7)
        self.assertTrue(created.enabled)
        self.assertEqual(created.limit_value, 4)
        # 登录时镜像授权不能作废已签发令牌
        self.assertEqual(record.auth_version, 3)

    async def test_existing_row_is_updated_in_place(self):
        from backend.app.api.routes.auth import _sync_cloud_overrides

        existing = OverrideRow("ai.builtin_reply", enabled=True, limit_value=4)
        session = _RecordingSession([existing])
        record = SimpleNamespace(id=7, auth_version=1)

        await _sync_cloud_overrides(session, record, {"ai.builtin_reply": {"enabled": False}})

        self.assertEqual(session.deleted, [])
        self.assertEqual(session.added, [])
        self.assertEqual(existing.enabled, False)
        # 云端未给出的字段应回落为空，表示“跟随套餐默认”
        self.assertIsNone(existing.limit_value)

    async def test_unmanaged_local_keys_are_kept(self):
        from backend.app.api.routes.auth import _sync_cloud_overrides

        other = OverrideRow("legacy.custom_flag", enabled=True)
        session = _RecordingSession([other])
        record = SimpleNamespace(id=7, auth_version=1)

        await _sync_cloud_overrides(session, record, {})

        self.assertEqual(session.deleted, [])

    async def test_empty_cloud_payload_does_not_wipe_local_rows(self):
        from backend.app.api.routes.auth import _sync_cloud_overrides

        # 云端未返回 entitlements（旧版鉴权服务）时不得清空本机授权
        local = OverrideRow("account.manage", enabled=True)
        session = _RecordingSession([local])
        record = SimpleNamespace(id=7, auth_version=1)

        await _sync_cloud_overrides(session, record, None)

        self.assertEqual(session.deleted, [])
        self.assertEqual(session.added, [])


if __name__ == "__main__":
    unittest.main()
