import unittest

from fastapi import HTTPException

from backend.app.services.entitlements import (
    EffectiveEntitlement,
    FEATURE_AI_SMART_REPLY,
    FEATURE_ACCOUNT,
    quota_error,
    split_keywords,
)


class EntitlementUnitTests(unittest.TestCase):
    def test_split_keywords_normalizes_lines_and_empty_values(self):
        self.assertEqual(split_keywords("  Hello  \n\nHELLO\r\n world "), ["hello", "hello", "world"])

    def test_admin_entitlement_is_unlimited_shape(self):
        value = EffectiveEntitlement(FEATURE_ACCOUNT, True, None, True, "ADMIN", "role")
        self.assertIsNone(value.as_dict(99)["limit"])
        self.assertIsNone(value.as_dict(99)["remaining"])

    def test_quota_error_has_structured_code(self):
        error = quota_error(FEATURE_AI_SMART_REPLY, "blocked", {"limit": 0})
        self.assertIsInstance(error, HTTPException)
        self.assertEqual(error.status_code, 409)
        self.assertEqual(error.detail["code"], "quota_exceeded")
        self.assertEqual(error.detail["feature_key"], FEATURE_AI_SMART_REPLY)


if __name__ == "__main__":
    unittest.main()
