"""Offline tests for the opt-in single-topic route. No Telegram/Supabase requests."""
import asyncio
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "goldilocks.py"


def load_monitor():
    env = {
        "MOODLE_API_URL": "https://example.invalid",
        "MOODLE_API_TOKEN": "fake-token",
        "TELEGRAM_BOT_TOKEN": "fake-bot",
        "ADMIN_CHAT_ID": "999",
        "TIMETABLE_URL": "https://example.invalid/timetable",
        "SUPABASE_URL": "https://example.invalid",
        "SUPABASE_KEY": "fake-db-key",
        "CHAT_ID": "999,-10042",
        "MOODLE_TOPIC_CHAT_ID": "-10042",
        "MOODLE_TOPIC_THREAD_ID": "2468",
        "MOODLE_TOPIC_ENABLED": "0",
        "QUIET_RUN": "0",
    }
    with patch.dict(os.environ, env):
        spec = importlib.util.spec_from_file_location("moodle_topic_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class TopicRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.m = load_monitor()

    async def test_disabled_keeps_both_recipients_without_topic(self):
        self.assertIsNone(self.m.topic_thread_for("-10042"))
        self.assertIsNone(self.m.topic_thread_for("999"))

    async def test_only_configured_group_gets_thread_id(self):
        self.m.TOPIC_ENABLED = True
        self.assertEqual(2468, self.m.topic_thread_for("-10042"))
        self.assertIsNone(self.m.topic_thread_for("999"))
        self.assertIsNone(self.m.topic_thread_for("-100-other"))
        fetch = AsyncMock(return_value={"ok": True})
        with patch.object(self.m, "fetch_data", fetch), patch.object(self.m.asyncio, "sleep", AsyncMock()):
            self.assertTrue(await self.m.send_telegram(None, "same message", "-10042", 2468))
            self.assertTrue(await self.m.send_telegram(None, "same message", "999"))
        self.assertEqual(2468, fetch.await_args_list[0].kwargs["post_data"]["message_thread_id"])
        self.assertNotIn("message_thread_id", fetch.await_args_list[1].kwargs["post_data"])
        self.assertEqual("same message", fetch.await_args_list[0].kwargs["post_data"]["text"])
        self.assertEqual("same message", fetch.await_args_list[1].kwargs["post_data"]["text"])

    async def test_partial_or_invalid_configuration_stops_enabled_routing(self):
        self.m.TOPIC_ENABLED = True
        self.m.TOPIC_THREAD_RAW = ""
        self.assertIn("MOODLE_TOPIC_THREAD_ID must be a positive number", self.m.config_problems())
        self.m.TOPIC_THREAD_RAW = "2468"
        self.m.TOPIC_CHAT_ID = "-10099"  # not present in CHAT_ID
        self.assertIn("MOODLE_TOPIC_CHAT_ID must be in CHAT_ID", self.m.config_problems())
        self.m.TOPIC_CHAT_ID = "-10042"
        self.assertEqual([], self.m.config_problems())

    async def test_explicit_telegram_rejection_is_a_failure(self):
        with patch.object(self.m, "fetch_data", AsyncMock(return_value={"ok": False, "description": "bad topic"})):
            self.assertFalse(await self.m.send_telegram(None, "hi", "-10042", 2468))

    async def test_manual_test_sends_one_message_without_scanning_or_saving(self):
        self.m.RUN_MODE = "test-topic"
        with patch.object(self.m, "send_telegram", AsyncMock(return_value=True)) as send, \
             patch.object(self.m, "load_memory", AsyncMock(side_effect=AssertionError("must not load"))), \
             patch.object(self.m, "save_memory", AsyncMock(side_effect=AssertionError("must not save"))):
            await self.m.main()
        send.assert_awaited_once()
        args, kwargs = send.await_args
        self.assertEqual("-10042", args[2])
        self.assertEqual(2468, kwargs["message_thread_id"])

    async def test_failed_topic_send_exits_before_save_or_private_broadcast(self):
        self.m.TOPIC_ENABLED = True
        memory = {"files": {}, "deadlines": {}, "timetable": {}, "grades": {}, "server_status": "ok"}

        async def files(mem, notes, session):
            notes.append("📢 A new file")
            mem["files"]["204"] = {"new": True}
            return True, True

        async def fine(*args):
            return False, True

        async def fake_send(session, message, chat, message_thread_id=None):
            self.assertEqual("-10042", chat)  # group first, no private delivery yet
            self.assertEqual(2468, message_thread_id)
            return False

        with patch.object(self.m, "load_memory", AsyncMock(return_value=(memory, None))), \
             patch.object(self.m, "scan_moodle", files), \
             patch.object(self.m, "scan_timetable", fine), \
             patch.object(self.m, "scan_deadlines", fine), \
             patch.object(self.m, "scan_grades", fine), \
             patch.object(self.m, "send_telegram", fake_send), \
             patch.object(self.m, "send_alert_dedup", AsyncMock()) as alert, \
             patch.object(self.m, "save_memory", AsyncMock()) as save:
            with self.assertRaises(SystemExit) as exit_code:
                await self.m.main()
            self.assertEqual(1, exit_code.exception.code)
        alert.assert_awaited_once()
        save.assert_not_awaited()
    async def test_normal_broadcast_keeps_combined_format_and_private_delivery(self):
        self.m.TOPIC_ENABLED = True
        memory = {"files": {}, "deadlines": {}, "timetable": {}, "grades": {}, "server_status": "ok"}

        async def files(mem, notes, session):
            notes.append("📢 File alert")
            return True, True

        async def deadlines(mem, notes, session):
            notes.append("🚨 Assignment alert")
            return True, True

        async def fine(*args):
            return False, True

        calls = []

        async def fake_send(session, message, chat, message_thread_id=None):
            calls.append((chat, message_thread_id, message))
            return True

        with patch.object(self.m, "load_memory", AsyncMock(return_value=(memory, None))), \
             patch.object(self.m, "scan_moodle", files), \
             patch.object(self.m, "scan_timetable", fine), \
             patch.object(self.m, "scan_deadlines", deadlines), \
             patch.object(self.m, "scan_grades", fine), \
             patch.object(self.m, "send_telegram", fake_send), \
             patch.object(self.m, "save_memory", AsyncMock(return_value=True)) as save:
            await self.m.main()
        self.assertEqual(["-10042", "999"], [chat for chat, _, _ in calls])
        self.assertEqual([2468, None], [thread for _, thread, _ in calls])
        self.assertEqual(calls[0][2], calls[1][2])  # unchanged combined message
        self.assertIn("📢 File alert", calls[0][2])
        self.assertIn("🚨 Assignment alert", calls[0][2])
        save.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
