"""Regression tests for /cancel (feedback #37/#39).

The support prompt told users to "Send /cancel to abort". /cancel is a command,
so it bypassed the text flow and switched auto-renew off at once. /cancel must
now close a pending text flow first and otherwise only ask for confirmation.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test")
os.environ.setdefault("SUPABASE_URL", "https://example.invalid")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test")
os.environ.setdefault("TELEGRAM_TRADING_CHANNEL_ID", "-1000000000000")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402

SOURCE = (Path(__file__).resolve().parent.parent / "bot.py").read_text(encoding="utf-8")

ACTIVE_TRIBUTE = {
    "provider": "tribute",
    "status": "active",
    "current_period_end": "2026-11-14T21:27:24+00:00",
    "cancel_at_period_end": False,
}


class _Message:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


def _update():
    msg = _Message()
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=1), message=msg)
    return upd, msg


class SupportPromptCopy(unittest.TestCase):
    def test_no_prompt_tells_users_to_send_cancel_command(self):
        self.assertNotIn("Send /cancel", SOURCE)
        self.assertNotIn("Отправьте /cancel", SOURCE)

    def test_menu_label_says_what_command_does(self):
        self.assertIn('BotCommand("cancel",    "Turn off auto-renew")', SOURCE)
        self.assertIn('BotCommand("cancel",    "Отключить автопродление")', SOURCE)


class CancelTexts(unittest.TestCase):
    def test_tribute_confirm_mentions_tribute(self):
        for lang in ("en", "ru"):
            text = bot.cancel_confirm_text(lang, "14.11.2026", "tribute")
            self.assertIn("14.11.2026", text)
            self.assertIn("@tribute", text)

    def test_non_tribute_confirm_has_no_tribute_note(self):
        text = bot.cancel_confirm_text("en", "14.11.2026", "yookassa")
        self.assertNotIn("@tribute", text)

    def test_keyboard_has_confirm_and_keep(self):
        kb = bot.cancel_confirm_keyboard("en")
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertEqual(data, [bot.CB_CANCEL_YES, bot.CB_CANCEL_NO])

    def test_no_long_dashes_in_new_copy(self):
        for lang in ("en", "ru"):
            for key in ("cancel_confirm", "cancel_tribute_note", "cancel_kept",
                        "cancel_already", "flow_aborted"):
                self.assertNotIn("—", bot.T(lang, key))


class CancelCommand(unittest.IsolatedAsyncioTestCase):
    async def _run(self, user_data, sub=ACTIVE_TRIBUTE):
        upd, msg = _update()
        ctx = SimpleNamespace(user_data=user_data)
        patch = mock.AsyncMock()
        with mock.patch.object(bot, "get_profile_by_telegram", mock.AsyncMock(return_value={"id": "u1"})), \
             mock.patch.object(bot, "get_user_lang", mock.AsyncMock(return_value="en")), \
             mock.patch.object(bot, "get_subscription", mock.AsyncMock(return_value=sub)), \
             mock.patch.object(bot, "sb_patch", patch):
            await bot.cmd_cancel(upd, ctx)
        return msg, patch

    async def test_cancel_during_support_message_only_aborts_flow(self):
        user_data = {"awaiting_feedback": {"lang": "en"}}
        msg, patch = await self._run(user_data)
        patch.assert_not_called()
        self.assertNotIn("awaiting_feedback", user_data)
        self.assertEqual(msg.replies[0][0], bot.T("en", "flow_aborted"))

    async def test_cancel_during_chart_request_only_aborts_flow(self):
        user_data = {"awaiting_chart_request_ticker": {"lang": "en"}}
        msg, patch = await self._run(user_data)
        patch.assert_not_called()
        self.assertEqual(user_data, {})

    async def test_cancel_asks_for_confirmation_and_changes_nothing(self):
        msg, patch = await self._run({})
        patch.assert_not_called()
        text, kwargs = msg.replies[0]
        self.assertTrue(text.startswith("Turn off auto-renew?"))
        self.assertIsNotNone(kwargs.get("reply_markup"))

    async def test_cancel_when_already_off(self):
        msg, patch = await self._run({}, sub={**ACTIVE_TRIBUTE, "cancel_at_period_end": True})
        patch.assert_not_called()
        self.assertIn("already off", msg.replies[0][0])


class _Query:
    def __init__(self):
        self.from_user = SimpleNamespace(id=1)
        self.message = _Message()
        self.edited = []

    async def edit_message_text(self, text, **kwargs):
        self.edited.append(text)


class CancelCallback(unittest.IsolatedAsyncioTestCase):
    async def _run(self, data, sub=ACTIVE_TRIBUTE):
        q = _Query()
        patch = mock.AsyncMock()
        with mock.patch.object(bot, "get_profile_by_telegram", mock.AsyncMock(return_value={"id": "u1"})), \
             mock.patch.object(bot, "get_user_lang", mock.AsyncMock(return_value="en")), \
             mock.patch.object(bot, "get_subscription", mock.AsyncMock(return_value=sub)), \
             mock.patch.object(bot, "sb_patch", patch):
            await bot._handle_cancel_callback(q, SimpleNamespace(), data)
        return q, patch

    async def test_keep_button_changes_nothing(self):
        q, patch = await self._run(bot.CB_CANCEL_NO)
        patch.assert_not_called()
        self.assertEqual(q.edited, [bot.T("en", "cancel_kept")])

    async def test_confirm_button_sets_flag(self):
        q, patch = await self._run(bot.CB_CANCEL_YES)
        patch.assert_awaited_once()
        body = patch.await_args.kwargs["body"]
        self.assertTrue(body["cancel_at_period_end"])
        self.assertEqual(body["cancel_reason"], "user_requested_via_bot")
        self.assertIn("@tribute", q.edited[0])

    async def test_confirm_on_stale_question_does_not_repeat(self):
        q, patch = await self._run(bot.CB_CANCEL_YES,
                                   sub={**ACTIVE_TRIBUTE, "cancel_at_period_end": True})
        patch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
