"""Core AI → Calendar pipeline: query display and event creation."""

import logging
import uuid
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from app.ai.parser import AIParserFactory
from app.bot.formatters import format_event_list, format_event_time_range
from app.calendar.service import CalendarService, get_timezone
from app.config import Config

logger = logging.getLogger(__name__)


def _build_calendar_context(events: list, tasks: list, tz) -> str:
    """Compact context block injected into the AI prompt.

    Lets the AI resolve vague references ("the meeting", "that workshop")
    against what's actually on the calendar this week.
    """
    lines = ["## Your current calendar (today → end of week)"]

    if events:
        lines.append("### Upcoming events")
        for e in events:
            s = e.get("start", {})
            if "date" in s:
                time_str = s["date"]
            else:
                dt = datetime.fromisoformat(
                    s["dateTime"].replace("Z", "+00:00")
                ).astimezone(tz)
                time_str = dt.strftime("%Y-%m-%d %H:%M")
            lines.append(f"  - [{e.get('summary', '(No Title)')}] {time_str}")
    else:
        lines.append("### Upcoming events: none")

    pending = [t for t in tasks if t.get("status") != "completed"]
    if pending:
        lines.append("### Pending tasks")
        for t in pending:
            due = t.get("due", "")
            due_str = f" (due: {due[:10]})" if due else ""
            lines.append(f"  - [{t.get('title', '(No Title)')}]{due_str}")
    else:
        lines.append("### Pending tasks: none")

    return "\n".join(lines)


async def query_and_display(
    user_id: int,
    time_min: datetime,
    time_max: datetime,
    search_query: str,
    status_message,
    header_text: str = "Schedule",
) -> None:
    """Fetch and display calendar events for the specified criteria."""
    try:
        calendar = CalendarService(user_id=user_id)
        events = calendar.list_events(
            time_min=time_min, time_max=time_max, search_query=search_query
        )
        tz = get_timezone(Config.TIMEZONE)
        start_date = time_min.astimezone(tz).date() if time_min else None
        end_date = time_max.astimezone(tz).date() if time_max else None
        formatted_list = format_event_list(events, tz, start_date, end_date)
        response = f"✨ *{header_text}*\n\n{formatted_list}"
        await status_message.edit_text(
            response, parse_mode="Markdown", disable_web_page_preview=True
        )
    except FileNotFoundError:
        logger.error("Credentials missing for query_and_display")
        await status_message.edit_text(
            "❌ Google Calendar credentials not found.\nPlease run /auth to authorize."
        )
    except Exception:
        logger.exception("Failed to query events")
        await status_message.edit_text("❌ Error fetching events. Please try again.")


async def process_and_save(
    user_id: int, text: str, status_message, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Parse text with AI and either query or save to Google Calendar."""
    try:
        parser = AIParserFactory.get_parser()
        tz = get_timezone(Config.TIMEZONE)
        now = datetime.now(tz)
        reference_time_str = now.strftime("%Y-%m-%d %H:%M (%A)")

        # Fetch upcoming events + tasks to give the AI calendar context so it can
        # resolve vague references ("the meeting", "that workshop", no date given).
        # Gracefully degrades to empty context if not yet authenticated.
        calendar_context = ""
        try:
            _cal = CalendarService(user_id=user_id)
            week_end = now + timedelta(days=(6 - now.weekday()) or 7)  # next Sunday
            upcoming = _cal.list_events(time_min=now, time_max=week_end, max_results=20)
            tasks_raw = _cal.list_tasks(show_completed=False)
            calendar_context = _build_calendar_context(upcoming, tasks_raw, tz)
        except FileNotFoundError:
            pass  # not authenticated yet; AI proceeds without context
        except Exception:
            logger.warning("Failed to fetch calendar context; proceeding without it")

        ai_res = parser.parse_message(
            text, reference_time_str, Config.TIMEZONE, calendar_context=calendar_context
        )

        # ── Query intent ──────────────────────────────────────────────
        if ai_res.intent == "query":
            time_min = None
            time_max = None
            if ai_res.query_time_min:
                time_min = datetime.fromisoformat(ai_res.query_time_min).replace(
                    tzinfo=tz
                )
            if ai_res.query_time_max:
                time_max = datetime.fromisoformat(ai_res.query_time_max).replace(
                    tzinfo=tz
                )
            if not time_min and not time_max:
                time_min = now

            await status_message.edit_text("Searching calendar events...")
            calendar = CalendarService(user_id=user_id)
            events = calendar.list_events(
                time_min=time_min,
                time_max=time_max,
                search_query=ai_res.query_search,
            )

            start_date = time_min.astimezone(tz).date() if time_min else None
            end_date = time_max.astimezone(tz).date() if time_max else None
            formatted_list = format_event_list(events, tz, start_date, end_date)
            header = "Search Results"
            if ai_res.query_search:
                header += f" for '{ai_res.query_search}'"
            await status_message.edit_text(
                f"✨ *{header}*\n\n{formatted_list}",
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            return

        # ── Complete Task intent ──────────────────────────────────────
        if ai_res.intent == "complete_task" and ai_res.tasks:
            calendar = CalendarService(user_id=user_id)
            existing_tasks = calendar.list_tasks(show_completed=False)
            completed_reports = []

            for task_req in ai_res.tasks:
                target_title = task_req.title.lower()
                found = False
                for existing in existing_tasks:
                    if target_title in existing.get("title", "").lower():
                        calendar.complete_task(existing["id"])
                        completed_reports.append(
                            f"✅ Marked as done: *{existing.get('title')}*"
                        )
                        found = True
                        break
                if not found:
                    completed_reports.append(
                        f"❌ Could not find active task matching: *{task_req.title}*"
                    )

            await status_message.edit_text(
                "\n".join(completed_reports), parse_mode="Markdown"
            )
            return

        # ── Create intent ─────────────────────────────────────────────
        events = ai_res.events or []
        tasks = ai_res.tasks or []

        if not events and not tasks:
            await status_message.edit_text(
                "❌ No events or tasks found in this message."
            )
            return

        await status_message.edit_text(
            f"AI parsed {len(events)} event(s) and {len(tasks)} task(s). Processing..."
        )

        # Enrich every event and task with the verbatim source message so it
        # is permanently stored in Google Calendar / Tasks.
        source_block = f"\n\n---\nSource message:\n{text.strip()}"
        events = [
            e.model_copy(update={"description": (e.description or "") + source_block})
            for e in events
        ]
        tasks = [
            t.model_copy(update={"notes": (t.notes or "") + source_block})
            for t in tasks
        ]

        calendar = CalendarService(user_id=user_id)
        success_reports = []

        # Process Tasks
        existing_tasks = calendar.list_tasks(show_completed=False)
        existing_titles = {t.get("title", "").strip().lower() for t in existing_tasks}

        for task in tasks:
            task_title_key = task.title.strip().lower()
            if task_title_key in existing_titles:
                report = f"⚠️ *Task already exists*: {task.title} (skipped duplicate)"
            else:
                calendar.create_task(
                    title=task.title, notes=task.notes, due_date=task.due_date
                )
                report = f"✅ *Task created*: {task.title}"
                if task.due_date:
                    report += f" (Due: {task.due_date})"
            success_reports.append(report)

        for idx, event in enumerate(events, start=1):
            candidate, is_dup = calendar.find_reschedule_candidate(event)

            if is_dup:
                time_str = format_event_time_range(candidate, tz)
                tx_id = f"tx_{user_id}_{uuid.uuid4().hex}"
                context.user_data[tx_id] = {
                    "candidate_id": candidate["id"],
                    "event": event.model_dump(),
                    "old_time_str": time_str,
                }
                await status_message.get_bot().send_message(
                    chat_id=status_message.chat_id,
                    text=(
                        f"⚠️ *Duplicate Detected*\n\n"
                        f"An event named *{event.summary}* already exists at the same time.\n"
                        f"Do you want to create a new one anyway?\n"
                        f"• *Time*: {time_str}\n"
                    ),
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "➕ Create Anyway",
                                    callback_data=f"create_reschedule_{tx_id}",
                                ),
                                InlineKeyboardButton(
                                    "❌ Cancel",
                                    callback_data=f"cancel_reschedule_{tx_id}",
                                ),
                            ]
                        ]
                    ),
                )

            elif candidate:
                old_time_str = format_event_time_range(candidate, tz)
                new_time_str = format_event_time_range(event, tz)
                collisions = calendar.check_collisions(
                    event, exclude_event_id=candidate["id"]
                )

                tx_id = f"tx_{user_id}_{uuid.uuid4().hex}"
                context.user_data[tx_id] = {
                    "candidate_id": candidate["id"],
                    "event": event.model_dump(),
                    "old_time_str": old_time_str,
                }

                if collisions:
                    context.user_data[tx_id]["colliding_ids"] = [
                        c["id"] for c in collisions
                    ]
                    col_list_str = "\n".join(
                        f"• *{c.get('summary', '(No Title)')}* ({format_event_time_range(c, tz)})"
                        for c in collisions
                    )
                    keyboard = [
                        [
                            InlineKeyboardButton(
                                "🔄 Reschedule Anyway",
                                callback_data=f"confirm_reschedule_{tx_id}",
                            ),
                            InlineKeyboardButton(
                                "🤝 Merge All into One",
                                callback_data=f"mergeall_reschedule_{tx_id}",
                            ),
                        ],
                        [
                            InlineKeyboardButton(
                                "🗑️ Delete All Colliding",
                                callback_data=f"deleteall_reschedule_{tx_id}",
                            ),
                            InlineKeyboardButton(
                                "❌ Cancel", callback_data=f"cancel_reschedule_{tx_id}"
                            ),
                        ],
                    ]
                    prompt_text = (
                        f"⚠️ *Reschedule Collision Detected*\n\n"
                        f"Moving *{event.summary}* to {new_time_str} collides with:\n"
                        f"{col_list_str}\n\nSelect action:"
                    )
                else:
                    keyboard = [
                        [
                            InlineKeyboardButton(
                                "🔄 Yes, Reschedule",
                                callback_data=f"confirm_reschedule_{tx_id}",
                            ),
                            InlineKeyboardButton(
                                "➕ No, Create New",
                                callback_data=f"create_reschedule_{tx_id}",
                            ),
                        ],
                        [
                            InlineKeyboardButton(
                                "❌ Cancel", callback_data=f"cancel_reschedule_{tx_id}"
                            )
                        ],
                    ]
                    prompt_text = (
                        f"🔄 *Reschedule Confirmation*\n\n"
                        f"Do you want to reschedule *{event.summary}*?\n"
                        f"• *Old Time*: {old_time_str}\n"
                        f"• *New Time*: {new_time_str}\n"
                    )

                await status_message.get_bot().send_message(
                    chat_id=status_message.chat_id,
                    text=prompt_text,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )

            else:
                collisions = calendar.check_collisions(event)
                new_time_str = format_event_time_range(event, tz)

                if collisions:
                    tx_id = f"tx_{user_id}_{uuid.uuid4().hex}"
                    context.user_data[tx_id] = {
                        "event": event.model_dump(),
                        "colliding_ids": [c["id"] for c in collisions],
                    }
                    col_list_str = "\n".join(
                        f"• *{c.get('summary', '(No Title)')}* ({format_event_time_range(c, tz)})"
                        for c in collisions
                    )
                    await status_message.get_bot().send_message(
                        chat_id=status_message.chat_id,
                        text=(
                            f"⚠️ *Collision Detected*\n\n"
                            f"Creating *{event.summary}* ({new_time_str}) collides with:\n"
                            f"{col_list_str}\n\nSelect action:"
                        ),
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "➕ Create Anyway",
                                        callback_data=f"forcecreate_reschedule_{tx_id}",
                                    ),
                                    InlineKeyboardButton(
                                        "🤝 Merge All into One",
                                        callback_data=f"mergeall_reschedule_{tx_id}",
                                    ),
                                ],
                                [
                                    InlineKeyboardButton(
                                        "🗑️ Delete All Colliding",
                                        callback_data=f"deleteall_reschedule_{tx_id}",
                                    ),
                                    InlineKeyboardButton(
                                        "❌ Cancel",
                                        callback_data=f"cancel_reschedule_{tx_id}",
                                    ),
                                ],
                            ]
                        ),
                    )
                else:
                    event_link = calendar.create_event(event)
                    report = f"📅 *Event #{idx}: {event.summary}*\n"
                    if event.is_all_day:
                        report += f"• *Date*: {event.start_date}\n"
                    else:
                        report += f"• *Start*: {event.start_datetime}\n• *End*: {event.end_datetime}\n"
                    if event.location:
                        report += f"• *Location*: {event.location}\n"

                    report += f"🔗 [Open in Google Calendar]({event_link})"
                    success_reports.append(report)

        if success_reports:
            await status_message.edit_text(
                "\n\n".join(success_reports),
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        else:
            try:
                await status_message.delete()
            except Exception:
                await status_message.edit_text("Reschedule confirmations sent.")

    except FileNotFoundError:
        logger.error("Credentials missing for process_and_save")
        await status_message.edit_text(
            "❌ Failed: Google Calendar credentials not found.\nPlease run /auth to authorize.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔄 Retry", callback_data="retry")]]
            ),
        )
    except Exception as e:
        logger.exception("Failed to parse or create event")
        err_msg = "❌ Error processing your message. Please try again."
        if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
            err_msg = "❌ API Limit Exceeded: Google Gemini API quota has been exhausted. Please try again later."
        await status_message.edit_text(
            err_msg,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔄 Retry", callback_data="retry")]]
            ),
        )
