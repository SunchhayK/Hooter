"""Formatting utilities for Telegram Markdown output."""

from datetime import datetime, timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup



def format_event_list(events: list, tz, start_date=None, end_date=None) -> str:
    """Format Google Calendar events grouped by date in Markdown, including blank dates."""
    grouped: dict = {}

    # Pre-populate all dates in range with empty lists
    if start_date and end_date:
        curr = start_date
        while curr <= end_date:
            grouped[curr] = []
            curr += timedelta(days=1)

    for event in events:
        start = event.get("start", {})
        if "date" in start:
            dt = datetime.strptime(start["date"], "%Y-%m-%d").date()
        else:
            dt = datetime.fromisoformat(start["dateTime"]).astimezone(tz).date()

        grouped.setdefault(dt, []).append(event)

    sorted_dates = sorted(grouped.keys())
    if not sorted_dates:
        return "📅 No events found in this period."

    parts = []
    for d in sorted_dates:
        date_header = d.strftime("%A, %b %d")
        day_lines = []

        for event in grouped[d]:
            summary = event.get("summary", "(No Title)")
            start = event.get("start", {})
            html_link = event.get("htmlLink")
            summary_link = f"[{summary}]({html_link})" if html_link else summary

            if "date" in start:
                day_lines.append(f"  • *[All Day]* {summary_link}")
            else:
                start_dt = datetime.fromisoformat(start["dateTime"]).astimezone(tz)
                day_lines.append(f"  • *[{start_dt.strftime('%I:%M %p')}]* {summary_link}")

        if not day_lines:
            day_lines.append("  • _No events_")

        parts.append(f"📅 *{date_header}*\n" + "\n".join(day_lines))

    return "\n\n".join(parts)


def format_event_time_range(event_or_parsed, tz) -> str:
    """Format start/end time of a Google Calendar dict or ParsedEvent to a friendly string."""
    if hasattr(event_or_parsed, "is_all_day"):
        # ParsedEvent
        if event_or_parsed.is_all_day:
            return event_or_parsed.start_date
        return f"{event_or_parsed.start_datetime} to {event_or_parsed.end_datetime}"

    # Google Calendar event dict
    start = event_or_parsed.get("start", {})
    if "date" in start:
        return start["date"]
    dt = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00")).astimezone(tz)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def build_tasks_keyboard(tasks: list) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build task list text and inline keyboard for completing tasks."""
    if not tasks:
        return "✅ No active tasks found.", None

    lines = ["📝 *Active Tasks:*"]
    keyboard_buttons = []

    for idx, task in enumerate(tasks, 1):
        title = task.get("title", "(No Title)")
        due = task.get("due", "")
        due_str = ""
        if due:
            try:
                dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
                due_str = f" (Due: {dt.strftime('%b %d')})"
            except Exception:
                due_str = f" (Due: {due[:10]})"
        lines.append(f"{idx}. {title}{due_str}")

        task_id = task.get("id")
        cb_data = f"completetask_{task_id}"
        # Telegram callback data limit is 64 bytes
        if len(cb_data.encode("utf-8")) <= 64:
            keyboard_buttons.append(
                InlineKeyboardButton(f"✅ {idx}", callback_data=cb_data)
            )

    keyboard = None
    if keyboard_buttons:
        # Group buttons in rows of up to 5 buttons
        rows = [keyboard_buttons[i : i + 5] for i in range(0, len(keyboard_buttons), 5)]
        keyboard = InlineKeyboardMarkup(rows)

    return "\n".join(lines), keyboard

