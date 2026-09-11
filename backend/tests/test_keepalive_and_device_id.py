# -*- coding: utf-8 -*-
"""IM 会话保活、风险 Cookie 清理与设备指纹的回归测试。

覆盖本次修复的三件事：

1. 补上 ``loginuser.get`` 保活（此前缺失，导致长登录态只能枯死）；
2. 续期前清除 Baxia 风险标记（避免"刷新→带标记→再 punish→刷新"死循环）；
3. 设备指纹固化（此前每次取 Token 都随机生成新指纹）。
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from common.models.accounts import Account
from common.services.cookie_renewal import (
    CookieRenewalService,
    RISK_COOKIE_NAMES,
    strip_risk_cookies,
)
from websocket.app.xianyu_runtime import (
    IM_KEEPALIVE_API,
    AccountRuntime,
    AccountRuntimeManager,
    _account_platform_id,
    _request_web_keepalive,
    _stable_device_id,
)


class _FakeHeaders:
    def __init__(self, set_cookies=()):
        self._set_cookies = list(set_cookies)

    def get_list(self, name):
        return list(self._set_cookies) if str(name).lower() == "set-cookie" else []


class _FakeResponse:
    def __init__(self, payload, set_cookies=(), status_code=200, text=""):
        self._payload = payload
        self.headers = _FakeHeaders(set_cookies)
        self.status_code = status_code
        # _api_renew_once 会读 response.text 解析业务消息
        self.text = text or (json.dumps(payload, ensure_ascii=False) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)


class _FakeClient:
    """记录每次 post 的入参，便于断言实际发出的 Cookie。"""

    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self._error is not None:
            raise self._error
        return self._response


def _patch_client(client):
    return patch("websocket.app.xianyu_runtime.httpx.AsyncClient", return_value=client)


# --------------------------------------------------------------------------- #
# 风险 Cookie 清理
# --------------------------------------------------------------------------- #

class StripRiskCookieTests(unittest.TestCase):
    def test_removes_risk_markers_and_keeps_credentials(self):
        raw = "unb=2223021364297; cookie2=abc; x5secdata=deadbeef; x5sectag=1; tfstk=xyz; cbc=zz"
        cleaned, removed = strip_risk_cookies(raw)
        self.assertIn("unb=2223021364297", cleaned)
        self.assertIn("cookie2=abc", cleaned)
        self.assertNotIn("x5secdata", cleaned)
        self.assertNotIn("x5sectag", cleaned)
        self.assertNotIn("tfstk", cleaned)
        self.assertNotIn("cbc", cleaned)
        self.assertEqual(sorted(removed), ["cbc", "tfstk", "x5secdata", "x5sectag"])

    def test_no_risk_cookies_is_noop(self):
        raw = "unb=1; cookie2=2"
        cleaned, removed = strip_risk_cookies(raw)
        self.assertEqual(cleaned, raw)
        self.assertEqual(removed, [])

    def test_empty_cookie_is_safe(self):
        cleaned, removed = strip_risk_cookies("")
        self.assertEqual(cleaned, "")
        self.assertEqual(removed, [])

    def test_risk_list_covers_baxia_markers(self):
        for name in ("x5secdata", "x5sectag", "tfstk", "cbc", "x5sec", "bx-cookie-test"):
            self.assertIn(name, RISK_COOKIE_NAMES)


class RenewalStripsRiskCookiesTests(unittest.IsolatedAsyncioTestCase):
    async def test_risk_cookies_are_never_sent_to_passport(self):
        """续期请求不得携带风险标记，否则会被再次判定高风险。"""
        from common.services import cookie_renewal as module

        response = _FakeResponse({"content": {"success": True}}, set_cookies=["cookie2=fresh"])
        client = _FakeClient(response=response)
        service = CookieRenewalService()

        with patch.object(module.httpx, "AsyncClient", return_value=client):
            result = await service._api_renew_once(
                "unb=2223021364297; cookie2=old; x5secdata=deadbeef; tfstk=abc",
                "账号 7",
            )

        self.assertTrue(client.calls, "应当至少请求一次 Passport")
        for url, kwargs in client.calls:
            sent = str((kwargs.get("headers") or {}).get("Cookie") or "")
            self.assertNotIn("x5secdata", sent, url)
            self.assertNotIn("tfstk", sent, url)
        self.assertTrue(any("风险标记" in step for step in result["steps"]))

    async def test_platform_rewritten_risk_marker_is_stripped_from_result(self):
        """平台本轮回写的风险标记不能被写进后续重试与数据库。"""
        from common.services import cookie_renewal as module

        response = _FakeResponse(
            {"content": {"success": True}},
            set_cookies=["unb=2223021364297", "x5secdata=rewritten", "tfstk=again"],
        )
        client = _FakeClient(response=response)
        service = CookieRenewalService()

        with patch.object(module.httpx, "AsyncClient", return_value=client):
            result = await service._api_renew_once("unb=2223021364297; cookie2=old", "账号 7")

        self.assertNotIn("x5secdata", result["new_cookie"])
        self.assertNotIn("tfstk", result["new_cookie"])
        self.assertIn("unb=2223021364297", result["new_cookie"])


# --------------------------------------------------------------------------- #
# 设备指纹固化
# --------------------------------------------------------------------------- #

class DeviceFingerprintTests(unittest.TestCase):
    def test_existing_fingerprint_is_reused(self):
        account = Account(
            id=7, user_id=1, account_name="账号", status="active",
            cookie="unb=2223021364297",
            im_device_id="7f8fd3c9-0781-4f4a-816b-e0be6b62b291-2223021364297",
        )
        device_id, created = _stable_device_id(account)
        self.assertEqual(device_id, "7f8fd3c9-0781-4f4a-816b-e0be6b62b291-2223021364297")
        self.assertFalse(created)

    def test_missing_fingerprint_is_generated_once(self):
        account = Account(
            id=7, user_id=1, account_name="账号", status="active",
            cookie="unb=2223021364297",
        )
        first, created = _stable_device_id(account)
        self.assertTrue(created)
        self.assertTrue(first.endswith("-2223021364297"))
        # 落库后再取必须得到同一个值
        account.im_device_id = first
        second, created_again = _stable_device_id(account)
        self.assertEqual(second, first)
        self.assertFalse(created_again)

    def test_generated_fingerprints_differ_between_accounts(self):
        a = Account(id=1, user_id=1, account_name="a", status="active", cookie="unb=111")
        b = Account(id=2, user_id=1, account_name="b", status="active", cookie="unb=222")
        self.assertNotEqual(_stable_device_id(a)[0], _stable_device_id(b)[0])

    def test_platform_id_falls_back_to_goofish_id(self):
        account = Account(id=7, user_id=1, account_name="账号", status="active", goofish_id="999")
        self.assertEqual(_account_platform_id(account), "999")

    def test_account_model_declares_device_column(self):
        # 模型必须声明该列，否则规范化查询会忽略它、指纹无法持久化。
        self.assertIn("im_device_id", Account.__table__.columns)


# --------------------------------------------------------------------------- #
# 保活接口
# --------------------------------------------------------------------------- #

class KeepaliveRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_returns_true_and_merges_cookies(self):
        response = _FakeResponse(
            {"ret": ["SUCCESS::调用成功"]},
            set_cookies=["_m_h5_tk=newtoken_123; Path=/", "cookie2=refreshed; Path=/"],
        )
        client = _FakeClient(response=response)
        with _patch_client(client):
            ok, message, refreshed = await _request_web_keepalive("unb=2223021364297; cookie2=old")

        self.assertTrue(ok)
        self.assertEqual(message, "会话保活成功")
        self.assertIn("_m_h5_tk=newtoken_123", refreshed)
        self.assertIn("cookie2=refreshed", refreshed)
        self.assertIn("unb=2223021364297", refreshed)

    async def test_keepalive_targets_loginuser_api(self):
        response = _FakeResponse({"ret": ["SUCCESS::调用成功"]})
        client = _FakeClient(response=response)
        with _patch_client(client):
            await _request_web_keepalive("unb=1")

        url = client.calls[0][0]
        self.assertIn(IM_KEEPALIVE_API, url)
        self.assertIn("loginuser.get", url)
        # 必须带签名参数，否则平台会拒绝
        params = client.calls[0][1]["params"]
        self.assertEqual(params["api"], IM_KEEPALIVE_API)
        self.assertEqual(params["sessionOption"], "AutoLoginOnly")
        self.assertTrue(params["sign"])

    async def test_session_expired_is_reported(self):
        response = _FakeResponse({"ret": ["FAIL_SYS_SESSION_EXPIRED::Session过期"]})
        client = _FakeClient(response=response)
        with _patch_client(client):
            ok, message, _ = await _request_web_keepalive("unb=1")

        self.assertFalse(ok)
        self.assertIn("FAIL_SYS_SESSION_EXPIRED", message)

    async def test_missing_unb_short_circuits(self):
        client = _FakeClient(response=_FakeResponse({"ret": ["SUCCESS::ok"]}))
        with _patch_client(client):
            ok, message, _ = await _request_web_keepalive("cookie2=only")

        self.assertFalse(ok)
        self.assertIn("unb", message)
        self.assertEqual(client.calls, [])

    async def test_transport_error_is_reported_not_raised(self):
        client = _FakeClient(error=httpx.ConnectError("network down"))
        with _patch_client(client):
            ok, message, refreshed = await _request_web_keepalive("unb=1")

        self.assertFalse(ok)
        self.assertIn("保活请求失败", message)
        self.assertEqual(refreshed, "unb=1")

    async def test_non_json_response_is_reported(self):
        client = _FakeClient(response=_FakeResponse(None))
        with _patch_client(client):
            ok, message, _ = await _request_web_keepalive("unb=1")

        self.assertFalse(ok)
        self.assertIn("不是 JSON", message)


# --------------------------------------------------------------------------- #
# 保活循环
# --------------------------------------------------------------------------- #

class KeepaliveLoopTests(unittest.IsolatedAsyncioTestCase):
    async def _run_loop_briefly(self, manager, runtime, calls, *, keepalive, seconds=0.05):
        """让保活循环跑一小段真实时间后取消。

        落库被替换成 AsyncMock：这里验证的是循环编排，不应触碰数据库
        （真实 DB 连接会阻塞到用例超时）。
        """
        manager._persist_runtime_cookie = AsyncMock()
        with patch(
            "websocket.app.xianyu_runtime.IM_KEEPALIVE_INTERVAL_SECONDS", 0.01
        ), patch(
            "websocket.app.xianyu_runtime._request_web_keepalive", new=keepalive
        ):
            task = asyncio.create_task(manager._keepalive_loop(runtime))
            await asyncio.sleep(seconds)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _manager_with_renew(self):
        manager = AccountRuntimeManager()
        manager._renew_runtime_account = AsyncMock(return_value=False)
        return manager

    async def test_success_records_timestamp_and_does_not_renew(self):
        manager = self._manager_with_renew()
        runtime = AccountRuntime(account_id="7", user_id=1, cookie_value="unb=1")
        keepalive = AsyncMock(return_value=(True, "会话保活成功", "unb=1; cookie2=new"))

        await self._run_loop_briefly(manager, runtime, None, keepalive=keepalive)

        self.assertTrue(keepalive.await_count >= 1)
        self.assertTrue(runtime.last_keepalive_at)
        self.assertEqual(runtime.last_keepalive_error, "")
        # 刷新的 Cookie 必须回写到运行时，供后续取 Token 与保活使用
        self.assertEqual(runtime.cookie_value, "unb=1; cookie2=new")
        manager._renew_runtime_account.assert_not_awaited()

    async def test_session_expired_triggers_immediate_renewal(self):
        manager = self._manager_with_renew()
        runtime = AccountRuntime(account_id="7", user_id=1, cookie_value="unb=1")
        keepalive = AsyncMock(
            return_value=(False, "FAIL_SYS_SESSION_EXPIRED::Session过期", "unb=1")
        )

        await self._run_loop_briefly(manager, runtime, None, keepalive=keepalive)

        manager._renew_runtime_account.assert_awaited()
        self.assertIn("SESSION_EXPIRED", runtime.last_keepalive_error)

    async def test_renewal_is_rate_limited_by_cooldown(self):
        """冷却期内不得反复续期，避免高频打 Passport。"""
        manager = self._manager_with_renew()
        runtime = AccountRuntime(account_id="7", user_id=1, cookie_value="unb=1")
        runtime.last_renewal_attempt_monotonic = 0.0  # 首次允许
        keepalive = AsyncMock(
            return_value=(False, "FAIL_SYS_SESSION_EXPIRED::Session过期", "unb=1")
        )

        await self._run_loop_briefly(manager, runtime, None, keepalive=keepalive, seconds=0.08)

        self.assertGreaterEqual(keepalive.await_count, 2, "循环应多次触发")
        self.assertEqual(manager._renew_runtime_account.await_count, 1, "冷却期内只续期一次")

    async def test_non_session_error_does_not_renew(self):
        manager = self._manager_with_renew()
        runtime = AccountRuntime(account_id="7", user_id=1, cookie_value="unb=1")
        keepalive = AsyncMock(return_value=(False, "保活请求失败：网络不可达", "unb=1"))

        await self._run_loop_briefly(manager, runtime, None, keepalive=keepalive)

        manager._renew_runtime_account.assert_not_awaited()
        self.assertIn("网络不可达", runtime.last_keepalive_error)

    async def test_exception_in_keepalive_does_not_kill_loop(self):
        manager = self._manager_with_renew()
        runtime = AccountRuntime(account_id="7", user_id=1, cookie_value="unb=1")
        keepalive = AsyncMock(side_effect=[RuntimeError("boom"), (True, "ok", "unb=1")])

        await self._run_loop_briefly(manager, runtime, None, keepalive=keepalive, seconds=0.08)

        self.assertTrue(keepalive.await_count >= 2, "一次异常后循环必须继续")
        self.assertTrue(runtime.last_keepalive_at)

    async def test_empty_cookie_skips_request(self):
        manager = self._manager_with_renew()
        runtime = AccountRuntime(account_id="7", user_id=1, cookie_value="   ")
        keepalive = AsyncMock(return_value=(True, "ok", "unb=1"))

        await self._run_loop_briefly(manager, runtime, None, keepalive=keepalive)

        keepalive.assert_not_awaited()


class KeepaliveWiringTests(unittest.IsolatedAsyncioTestCase):
    """保活必须真的被接进运行时，否则修复等于没生效。"""

    async def test_token_request_uses_provided_device_id(self):
        from websocket.app.xianyu_runtime import _request_token

        captured = {}

        async def fake_web_token(cookie_value, device_id):
            captured["device_id"] = device_id
            return "token-abc", device_id, cookie_value

        with patch(
            "websocket.app.xianyu_runtime._request_web_token", new=fake_web_token
        ), patch(
            "websocket.app.xianyu_runtime._load_token_settings",
            new=AsyncMock(return_value=("web", "", "")),
        ):
            token, device_id, mode, _ = await _request_token(
                "unb=2223021364297", 1, "stable-device-id-2223021364297"
            )

        self.assertEqual(token, "token-abc")
        self.assertEqual(mode, "web")
        # 关键：必须原样沿用传入的稳定指纹，不能临时再生成一个
        self.assertEqual(captured["device_id"], "stable-device-id-2223021364297")
        self.assertEqual(device_id, "stable-device-id-2223021364297")

    async def test_run_actually_starts_keepalive_task(self):
        """回归保护：_run 必须创建并回收保活任务。"""
        import inspect

        from websocket.app.xianyu_runtime import AccountRuntimeManager

        source = inspect.getsource(AccountRuntimeManager._run)
        self.assertIn("_keepalive_loop", source, "_run 未启动保活循环")
        self.assertIn("keepalive_task.cancel()", source, "保活任务未在退出时回收")

    async def test_keepalive_interval_matches_reference(self):
        from websocket.app.xianyu_runtime import IM_KEEPALIVE_INTERVAL_SECONDS

        # 与公开实现一致的 600 秒；间隔过长会在会话失效前来不及保活。
        self.assertEqual(IM_KEEPALIVE_INTERVAL_SECONDS, 600.0)


if __name__ == "__main__":
    unittest.main()
