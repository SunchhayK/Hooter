"""Unit tests for the background scheduler."""

import asyncio
from datetime import datetime, timezone
from unittest import mock

from app.bot.scheduler import start_scheduler


class TerminateLoop(Exception):
    """Exception to break the infinite loop in test."""

    pass


async def async_sleep_mock(delay):
    raise TerminateLoop()


def test_scheduler_runs_one_tick():
    """Verify scheduler executes daily notification and 1-hour reminder."""
    # 1. Setup mocks
    mock_app = mock.MagicMock()
    mock_app.bot.send_message = mock.AsyncMock()

    mock_calendar = mock.MagicMock()
    # Mock events returned: one today and one starting in 1 hour
    mock_calendar.list_events.return_value = [
        {
            "id": "event_1h",
            "summary": "Meeting soon",
            "htmlLink": "http://event_link",
            "start": {"dateTime": "2026-07-14T12:30:20Z"},
            "end": {"dateTime": "2026-07-14T13:30:20Z"},
        }
    ]

    # Current mock time matches 11:30:20 UTC (which is exactly 1 hour before event_1h)
    mock_now = datetime(2026, 7, 14, 11, 30, 20, tzinfo=timezone.utc)

    # 2. Run scheduler with mocked imports
    with (
        mock.patch("app.bot.scheduler.Config") as mock_config,
        mock.patch("app.bot.scheduler.os.path.exists", return_value=True),
        mock.patch("app.bot.scheduler.CalendarService", return_value=mock_calendar),
        mock.patch("app.bot.scheduler.get_timezone", return_value=timezone.utc),
        mock.patch("app.bot.scheduler.datetime") as mock_dt,
        mock.patch("app.bot.scheduler.asyncio.sleep", side_effect=async_sleep_mock),
    ):
        mock_config.ALLOWED_USER_IDS = [12345]
        mock_config.DAILY_REMINDER_HOUR = 8
        mock_config.TIMEZONE = "UTC"
        mock_dt.now.return_value = mock_now
        # Support datetime.fromisoformat which is called in scheduler
        mock_dt.fromisoformat = datetime.fromisoformat

        # Run scheduler until TerminateLoop raised
        try:
            asyncio.run(start_scheduler(mock_app))
        except TerminateLoop:
            pass

    # 3. Assertions
    # Daily schedule check and 1-hour reminder check both list events
    assert mock_calendar.list_events.call_count >= 2

    # Verify messages sent:
    # 1. Daily schedule (sent at 11:30 >= 8:00)
    # 2. 1-hour reminder for "Meeting soon"
    assert mock_app.bot.send_message.call_count == 2

    calls = mock_app.bot.send_message.call_args_list
    daily_msg = calls[0][1]["text"]
    reminder_msg = calls[1][1]["text"]

    assert "Today's Schedule" in daily_msg
    assert "Upcoming Event Reminder" in reminder_msg
    assert "Meeting soon" in reminder_msg
    assert "12:30 PM" in reminder_msg

    print("✓ Scheduler tick test passed.")


if __name__ == "__main__":
    test_scheduler_runs_one_tick()
