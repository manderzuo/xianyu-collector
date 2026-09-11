"""注册开关一致性的回归测试。

背景：`/system-settings/public` 在设置行缺失时返回 true（页面显示注册开放），
而 `/auth/register` 在设置行缺失时按关闭处理并返回 403
“注册功能已关闭，请联系管理员”。用户能填完表单却必然被拒绝。

`xr_system_settings` 里从来没有种子数据写入 `registration_enabled`，
所以“设置行缺失”是全新部署的默认状态，该缺陷对全新部署必然触发。
本文件锁定两个接口必须共用同一套默认值。
"""
import unittest

from common.services.system_settings import (
    DEFAULT_REGISTRATION_ENABLED,
    FALSY_VALUES,
    TRUTHY_VALUES,
    parse_bool_value,
)


class _Result:
    """同时支持两种查询形态：公开设置批量取行、read_setting 取单值。"""

    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        # 公开设置会一次性取出多行；registration_enabled 由共享 helper 计算，
        # 与这里的行集合无关，因此返回空行即可。
        return []


class _FakeSession:
    """返回固定设置值的假会话。"""

    def __init__(self, value):
        self.value = value

    async def execute(self, _query):
        return _Result(self.value)


class RegistrationDefaultTests(unittest.IsolatedAsyncioTestCase):
    def test_default_is_enabled(self):
        # 与公开设置接口和设置页 `?? true` 的既有默认值保持一致
        self.assertTrue(DEFAULT_REGISTRATION_ENABLED)

    async def test_missing_row_is_enabled(self):
        from common.services.system_settings import registration_enabled

        self.assertTrue(await registration_enabled(_FakeSession(None)))

    async def test_explicit_false_still_blocks(self):
        from common.services.system_settings import registration_enabled

        for value in ("false", "0", "no", "off", "FALSE", " False "):
            self.assertFalse(await registration_enabled(_FakeSession(value)), value)

    async def test_explicit_true_allows(self):
        from common.services.system_settings import registration_enabled

        for value in ("true", "1", "yes", "on", "TRUE", " true "):
            self.assertTrue(await registration_enabled(_FakeSession(value)), value)

    async def test_unrecognized_value_falls_back_to_default(self):
        from common.services.system_settings import registration_enabled

        # 无法识别的脏数据按默认值处理，而不是意外关闭注册
        self.assertTrue(await registration_enabled(_FakeSession("maybe")))


class ParseBoolValueTests(unittest.TestCase):
    def test_boolean_passthrough(self):
        self.assertTrue(parse_bool_value(True))
        self.assertFalse(parse_bool_value(False, default=True))

    def test_none_uses_default(self):
        self.assertTrue(parse_bool_value(None, default=True))
        self.assertFalse(parse_bool_value(None, default=False))

    def test_truthy_and_falsy_sets_are_disjoint(self):
        self.assertEqual(TRUTHY_VALUES & FALSY_VALUES, set())

    def test_blank_value_is_treated_as_unconfigured(self):
        # 空值不携带明确意图，按“未配置”处理并回落到默认值，
        # 而不是像旧实现那样一律关闭注册。
        self.assertTrue(parse_bool_value("", default=True))
        self.assertTrue(parse_bool_value("   ", default=True))
        self.assertFalse(parse_bool_value("", default=False))


class PublicSettingsConsistencyTests(unittest.IsolatedAsyncioTestCase):
    """公开设置接口必须输出规范化的 true/false。"""

    async def _public_settings(self, stored_value):
        from backend.app.api.routes.legacy_compat import public_settings

        return await public_settings(db=_FakeSession(stored_value))

    async def test_missing_row_reports_true(self):
        result = await self._public_settings(None)
        self.assertEqual(result["data"]["registration_enabled"], "true")
        # 页面据此显示注册表单，注册接口必须同样放行
        from common.services.system_settings import registration_enabled

        self.assertTrue(await registration_enabled(_FakeSession(None)))

    async def test_explicit_false_reports_false(self):
        result = await self._public_settings("false")
        self.assertEqual(result["data"]["registration_enabled"], "false")
        from common.services.system_settings import registration_enabled

        self.assertFalse(await registration_enabled(_FakeSession("false")))

    async def test_non_canonical_truthy_value_is_normalized(self):
        # 数据库里存 yes/on 时，旧实现会让前端隐藏注册入口而后端允许注册
        for stored in ("yes", "on", "1"):
            result = await self._public_settings(stored)
            self.assertEqual(result["data"]["registration_enabled"], "true", stored)

    async def test_public_and_register_agree_for_every_value(self):
        """核心断言：两个接口对同一取值必须得出相同结论。"""
        from common.services.system_settings import registration_enabled

        for stored in (None, "true", "false", "1", "0", "yes", "no", "on", "off", "maybe"):
            page_open = (await self._public_settings(stored))["data"]["registration_enabled"] == "true"
            register_allows = await registration_enabled(_FakeSession(stored))
            self.assertEqual(page_open, register_allows, f"取值 {stored!r} 下两个接口结论不一致")


if __name__ == "__main__":
    unittest.main()
