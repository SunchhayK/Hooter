"""Background scheduler for daily schedule notifications and 1-hour event reminders."""

import asyncio
import logging
import os
from datetime import datetime, timedelta

from telegram.ext import Application

from app.bot.formatters import format_event_list
from app.calendar.service import CalendarService, get_timezone
from app.config import Config

logger = logging.getLogger(__name__)


async def start_scheduler(application: Application) -> None:
    """Run background loop for daily schedules and 1-hour event reminders."""
    logger.info("Starting background scheduler task...")

    # last_daily_sent: dict[int, str] -> tracks last YYYY-MM-DD daily schedule sent to user
    last_daily_sent: dict[int, str] = {}
    # sent_reminders: set[tuple[int, str, str]] -> tracks (user_id, event_id, start_time_iso)
    sent_reminders: set[tuple[int, str, str]] = set()
    # last_task_reminder: dict[int, datetime] -> tracks last time 4h tasks reminder was sent
    last_task_reminder: dict[int, datetime] = {}

    while True:
        try:
            tz = get_timezone(Config.TIMEZONE)
            now = datetime.now(tz)

            # ── Clean up old reminders ────────────────────────────────────────
            expired_reminders = set()
            for key in sent_reminders:
                _, _, start_time_iso = key
                try:
                    start_time = datetime.fromisoformat(
                        start_time_iso.replace("Z", "+00:00")
                    ).astimezone(tz)
                    if start_time < now - timedelta(hours=2):
                        expired_reminders.add(key)
                except Exception:
                    expired_reminders.add(key)
            sent_reminders.difference_update(expired_reminders)

            # ── Check notifications for all allowed users ─────────────────────
            for user_id in Config.ALLOWED_USER_IDS:
                user_token = f"tokens/token_{user_id}.json"
                if not os.path.exists(user_token):
                    continue

                try:
                    calendar = CalendarService(user_id=user_id)

                    # 1. Daily Schedule Reminder
                    today_str = now.strftime("%Y-%m-%d")
                    if (
                        now.hour >= Config.DAILY_REMINDER_HOUR
                        and last_daily_sent.get(user_id) != today_str
                    ):
                        time_min = now.replace(
                            hour=0, minute=0, second=0, microsecond=0
                        )
                        time_max = now.replace(
                            hour=23, minute=59, second=59, microsecond=999999
                        )
                        events = calendar.list_events(
                            time_min=time_min, time_max=time_max
                        )

                        start_date = time_min.astimezone(tz).date()
                        end_date = time_max.astimezone(tz).date()
                        formatted_list = format_event_list(
                            events, tz, start_date, end_date
                        )

                        msg = f"✨ *Today's Schedule*\n\n{formatted_list}"
                        await application.bot.send_message(
                            chat_id=user_id,
                            text=msg,
                            parse_mode="Markdown",
                            disable_web_page_preview=True,
                        )
                        last_daily_sent[user_id] = today_str
                        logger.info(f"Sent daily schedule to user {user_id}")

                    # 2. 1-Hour Event Reminders
                    # Fetch events starting in the next 2 hours
                    time_min = now
                    time_max = now + timedelta(hours=2)
                    events = calendar.list_events(time_min=time_min, time_max=time_max)

                    for event in events:
                        start = event.get("start", {})
                        if "dateTime" not in start:
                            continue  # Skip all-day events

                        event_id = event.get("id")
                        start_time_iso = start["dateTime"]
                        reminder_key = (user_id, event_id, start_time_iso)

                        if reminder_key in sent_reminders:
                            continue

                        start_dt = datetime.fromisoformat(
                            start_time_iso.replace("Z", "+00:00")
                        ).astimezone(tz)
                        reminder_time = start_dt - timedelta(hours=1)

                        if now >= reminder_time and now < start_dt:
                            summary = event.get("summary", "(No Title)")
                            html_link = event.get("htmlLink")
                            time_str = start_dt.strftime("%I:%M %p")

                            text = (
                                f"⏰ *Upcoming Event Reminder*\n\n"
                                f"*{summary}*\n"
                                f"• *Time*: {time_str}"
                            )
                            if event.get("location"):
                                text += f"\n• *Location*: {event.get('location')}"
                            if event.get("description"):
                                text += f"\n• *Description*: {event.get('description')}"
                            if html_link:
                                text += f"\n\n🔗 [Open in Google Calendar]({html_link})"

                            await application.bot.send_message(
                                chat_id=user_id,
                                text=text,
                                parse_mode="Markdown",
                                disable_web_page_preview=True,
                            )
                            sent_reminders.add(reminder_key)
                            logger.info(
                                f"Sent 1-hour reminder for event {event_id} to user {user_id}"
                            )

                    # 3. 4-Hour Tasks Reminder
                    last_task = last_task_reminder.get(user_id)
                    if not last_task or now - last_task >= timedelta(hours=4):
                        tasks = calendar.list_tasks(show_completed=False)
                        if tasks:
                            from telegram import (
                                InlineKeyboardMarkup,
                                InlineKeyboardButton,
                            )

                            task_lines = []
                            keyboard = []
                            for task in tasks:
                                title = task.get("title", "Untitled")
                                task_id = task.get("id")
                                task_lines.append(f"• {title}")
                                keyboard.append(
                                    [
                                        InlineKeyboardButton(
                                            f"✅ Complete: {title[:20]}",
                                            callback_data=f"completetask_{task_id}",
                                        )
                                    ]
                                )

                            text = "📝 *Unfinished Tasks Reminder*\n\n" + "\n".join(
                                task_lines
                            )
                            await application.bot.send_message(
                                chat_id=user_id,
                                text=text,
                                parse_mode="Markdown",
                                reply_markup=InlineKeyboardMarkup(keyboard),
                            )
                        last_task_reminder[user_id] = now

                except Exception as e:
                    logger.error(
                        f"Error in scheduler check for user {user_id}: {e}",
                        exc_info=True,
                    )

        except Exception as e:
            logger.error(f"Error in scheduler loop: {e}", exc_info=True)

        await asyncio.sleep(60)
