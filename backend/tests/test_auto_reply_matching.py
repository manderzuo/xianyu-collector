from __future__ import annotations

import unittest
from types import SimpleNamespace

from backend.app.services import auto_reply_service as service


def _row(row_id: int, **payload):
    return SimpleNamespace(id=row_id, payload=payload)


async def _no_filter(_account, _text):
    return False


class AutoReplyMatchingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.account = SimpleNamespace(id=7, user_id=1)
        self.original_filter = service._should_skip_reply
        self.original_rules = service._load_rules
        service._should_skip_reply = _no_filter

    def tearDown(self):
        service._should_skip_reply = self.original_filter
        service._load_rules = self.original_rules

    def set_rules(self, rows):
        service._load_rules = lambda _account: _resolved(rows)

    async def test_longest_contains_keyword_wins(self):
        self.set_rules([_row(1, keyword="售后", reply="短"), _row(2, keyword="售后退款", reply="长")])
        result = await service.match_keyword_reply(self.account, {"text": "我想申请售后退款"})
        self.assertIsNotNone(result)
        self.assertEqual(result.reply, "长")

    async def test_item_rule_wins_over_higher_priority_common_rule(self):
        self.set_rules([_row(1, keyword="退款", reply="通用", priority=99), _row(2, keyword="退款", reply="商品专属", item_id="I1")])
        result = await service.match_keyword_reply(self.account, {"text": "退款", "item_id": "I1"})
        self.assertIsNotNone(result)
        self.assertEqual(result.reply, "商品专属")

    async def test_disabled_and_stage_mismatch_rules_are_skipped(self):
        self.set_rules([
            _row(1, keyword="发货", reply="禁用", enabled=False),
            _row(2, keyword="发货", reply="审核中", needs_human="true"),
            _row(3, keyword="发货", reply="售后专用", conversation_stage="after_sale"),
            _row(4, keyword="发货", reply="兜底"),
        ])
        result = await service.match_keyword_reply(self.account, {"text": "请发货", "conversation_stage": "pre_sale"})
        self.assertIsNotNone(result)
        self.assertEqual(result.reply, "兜底")

    async def test_known_variables_are_rendered(self):
        self.set_rules([_row(1, keyword="你好", reply="你好，{buyer_name}")])
        result = await service.match_keyword_reply(self.account, {"text": "你好", "senderName": "小明"})
        self.assertIsNotNone(result)
        self.assertEqual(result.reply, "你好，小明")

    async def test_missing_variable_does_not_send_placeholder(self):
        self.set_rules([_row(1, keyword="你好", reply="你好，{buyer_name}"), _row(2, keyword="你好", reply="你好，在的")])
        result = await service.match_keyword_reply(self.account, {"text": "你好"})
        self.assertIsNotNone(result)
        self.assertEqual(result.reply, "你好，在的")


async def _resolved(value):
    return value
