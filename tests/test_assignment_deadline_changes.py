"""Offline contract tests for changes to class-wide assignment due dates."""
import asyncio
import datetime as dt
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

SCRIPT = Path(__file__).resolve().parents[1] / "goldilocks.py"
IRAQ = ZoneInfo("Asia/Baghdad")


def due(day):
    return int(dt.datetime(2031, 3, day, 12, 0, tzinfo=IRAQ).timestamp())


def load_monitor():
    # Import only: never contact a real service or read a user credential.
    env = {"MOODLE_API_URL": "https://example.invalid", "MOODLE_API_TOKEN": "fake",
           "TELEGRAM_BOT_TOKEN": "fake", "ADMIN_CHAT_ID": "1",
           "TIMETABLE_URL": "https://example.invalid/tt", "SUPABASE_URL": "https://example.invalid",
           "SUPABASE_KEY": "fake", "IGNORE_COURSES": "0"}
    with patch.dict(os.environ, env):
        spec = importlib.util.spec_from_file_location("deadline_changes", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class AssignmentDeadlineChangeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.m = load_monitor()

    async def scan(self, old, new, ignored=False):
        self.m.IGNORE_COURSES = ["204"] if ignored else []
        memory = {"deadlines": {"assign_2162": {"timestamp": old,
                   "name": "ASSIGNMENT I", "course": "Phonology I"}}}
        notices = []

        async def feed(session, url, post_data=None, **kwargs):
            fn = post_data["wsfunction"]
            if fn == "mod_assign_get_assignments":
                return {"courses": [{"id": 204, "fullname": "Phonology I",
                       "assignments": [{"id": 2162, "name": "ASSIGNMENT I", "duedate": new}]}]}
            if fn == "mod_quiz_get_quizzes_by_courses":
                return {"quizzes": []}
            if fn == "core_calendar_get_action_events_by_timesort":
                return {"events": []}
            raise AssertionError(f"Unexpected API call: {fn}")

        with patch.object(self.m, "fetch_data", feed):
            updated, ok = await self.m.scan_deadlines(memory, notices, None)
            # Ignored courses enter the existing missing-item cleanup; don't
            # advance that unrelated two-scan cleanup during this test.
            again = []
            second_updated, second_ok = (False, True)
            if not ignored:
                second_updated, second_ok = await self.m.scan_deadlines(memory, again, None)
        return memory, notices, (updated, ok), (again, second_updated, second_ok)

    async def test_moved_earlier_sends_both_dates_and_reduction_once(self):
        mem, notes, outcome, again = await self.scan(due(15), due(12))
        self.assertEqual((True, True), outcome)
        self.assertEqual(1, len(notes))
        self.assertIn("DEADLINE MOVED EARLIER", notes[0])
        self.assertIn("Phonology I", notes[0])
        self.assertIn("ASSIGNMENT I", notes[0])
        self.assertIn("3 days", notes[0])
        self.assertIn("Mar 15, 2031", notes[0])
        self.assertIn("Mar 12, 2031", notes[0])
        self.assertEqual(due(12), mem["deadlines"]["assign_2162"]["timestamp"])
        self.assertEqual(([], False, True), again)

    async def test_extended_still_sends_both_dates_and_increase_once(self):
        mem, notes, outcome, again = await self.scan(due(12), due(15))
        self.assertEqual((True, True), outcome)
        self.assertEqual(1, len(notes))
        self.assertIn("DEADLINE EXTENDED", notes[0])
        self.assertIn("3 days", notes[0])
        self.assertIn("Mar 12, 2031", notes[0])
        self.assertIn("Mar 15, 2031", notes[0])
        self.assertEqual(([], False, True), again)

    async def test_undated_to_dated_is_not_called_extension(self):
        _, notes, outcome, again = await self.scan(0, due(15))
        self.assertEqual((True, True), outcome)
        self.assertEqual(1, len(notes))
        self.assertIn("DUE DATE SET", notes[0])
        self.assertNotIn("EXTENDED", notes[0])
        self.assertIn("No due date", notes[0])
        self.assertIn("Mar 15, 2031", notes[0])
        self.assertEqual(([], False, True), again)

    async def test_dated_to_undated_is_not_called_earlier(self):
        _, notes, outcome, again = await self.scan(due(15), 0)
        self.assertEqual((True, True), outcome)
        self.assertEqual(1, len(notes))
        self.assertIn("DUE DATE REMOVED", notes[0])
        self.assertNotIn("MOVED EARLIER", notes[0])
        self.assertIn("Mar 15, 2031", notes[0])
        self.assertIn("No due date", notes[0])
        self.assertEqual(([], False, True), again)

    async def test_unchanged_does_not_notify_or_write(self):
        _, notes, outcome, again = await self.scan(due(15), due(15))
        self.assertEqual((False, True), outcome)
        self.assertEqual([], notes)
        self.assertEqual(([], False, True), again)

    async def test_shortening_into_the_past_still_notifies(self):
        now = int(dt.datetime.now(tz=IRAQ).timestamp())
        old = now + 2 * 86400
        new = now - 86400
        mem, notes, outcome, again = await self.scan(old, new)
        self.assertEqual((True, True), outcome)
        self.assertEqual(1, len(notes))
        self.assertIn("DEADLINE MOVED EARLIER", notes[0])
        self.assertEqual(new, mem["deadlines"]["assign_2162"]["timestamp"])
        self.assertEqual(([], False, True), again)

    async def test_short_subhour_change_has_minutes_not_zero_days(self):
        _, notes, _, _ = await self.scan(due(15), due(15) - 30 * 60)
        self.assertEqual(1, len(notes))
        self.assertIn("Shortened by: 30 minutes", notes[0])

    async def test_ignored_course_does_not_notify(self):
        mem, notes, outcome, _ = await self.scan(due(15), due(12), ignored=True)
        self.assertEqual((True, True), outcome)  # existing missing-item counter advanced
        self.assertEqual([], notes)
        self.assertEqual(due(15), mem["deadlines"]["assign_2162"]["timestamp"])


if __name__ == "__main__":
    unittest.main()
