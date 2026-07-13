"""Core AI → Calendar pipeline: query display and event creation."""

import logging
import time
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from app.ai.parser import AIParserFactory
from app.bot.formatters import format_event_list, format_event_time_range
from app.calendar.service import CalendarService, get_timezone
from app.config import Config

logger = logging.getLogger(__name__)


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
        reference_time_str = now.strftime("%Y-%m-%d %H:%M:%S (%A)")

        ai_res = parser.parse_message(text, reference_time_str, Config.TIMEZONE)

        # ── Query intent ──────────────────────────────────────────────
        if ai_res.intent == "query":
            time_min = None
            time_max = None
            if ai_res.query_time_min:
                time_min = datetime.fromisoformat(ai_res.query_time_min).replace(tzinfo=tz)
            if ai_res.query_time_max:
                time_max = datetime.fromisoformat(ai_res.query_time_max).replace(tzinfo=tz)
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

        # ── Create intent ─────────────────────────────────────────────
        events = ai_res.events
        if not events:
            await status_message.edit_text("❌ No events found in this message.")
            return

        await status_message.edit_text(
            f"AI parsed {len(events)} event(s). Adding to Google Calendar..."
        )

        calendar = CalendarService(user_id=user_id)
        success_reports = []

        for idx, event in enumerate(events, start=1):
            candidate, is_dup = calendar.find_reschedule_candidate(event)

            if is_dup:
                time_str = format_event_time_range(candidate, tz)
                tx_id = f"tx_{user_id}_{int(time.time())}_{idx}"
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
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("➕ Create Anyway", callback_data=f"create_reschedule_{tx_id}"),
                        InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_reschedule_{tx_id}"),
                    ]]),
                )

            elif candidate:
                old_time_str = format_event_time_range(candidate, tz)
                new_time_str = format_event_time_range(event, tz)
                collisions = calendar.check_collisions(event, exclude_event_id=candidate["id"])

                tx_id = f"tx_{user_id}_{int(time.time())}_{idx}"
                context.user_data[tx_id] = {
                    "candidate_id": candidate["id"],
                    "event": event.model_dump(),
                    "old_time_str": old_time_str,
                }

                if collisions:
                    context.user_data[tx_id]["colliding_ids"] = [c["id"] for c in collisions]
                    col_list_str = "\n".join(
                        f"• *{c.get('summary', '(No Title)')}* ({format_event_time_range(c, tz)})"
                        for c in collisions
                    )
                    keyboard = [
                        [
                            InlineKeyboardButton("🔄 Reschedule Anyway", callback_data=f"confirm_reschedule_{tx_id}"),
                            InlineKeyboardButton("🤝 Merge All into One", callback_data=f"mergeall_reschedule_{tx_id}"),
                        ],
                        [
                            InlineKeyboardButton("🗑️ Delete All Colliding", callback_data=f"deleteall_reschedule_{tx_id}"),
                            InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_reschedule_{tx_id}"),
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
                            InlineKeyboardButton("🔄 Yes, Reschedule", callback_data=f"confirm_reschedule_{tx_id}"),
                            InlineKeyboardButton("➕ No, Create New", callback_data=f"create_reschedule_{tx_id}"),
                        ],
                        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_reschedule_{tx_id}")],
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
                    tx_id = f"tx_{user_id}_{int(time.time())}_{idx}"
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
                        reply_markup=InlineKeyboardMarkup([
                            [
                                InlineKeyboardButton("➕ Create Anyway", callback_data=f"forcecreate_reschedule_{tx_id}"),
                                InlineKeyboardButton("🤝 Merge All into One", callback_data=f"mergeall_reschedule_{tx_id}"),
                            ],
                            [
                                InlineKeyboardButton("🗑️ Delete All Colliding", callback_data=f"deleteall_reschedule_{tx_id}"),
                                InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_reschedule_{tx_id}"),
                            ],
                        ]),
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
                    if event.description:
                        report += f"• *Description*: {event.description}\n"
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
    except Exception:
        logger.exception("Failed to parse or create event")
        await status_message.edit_text(
            "❌ Error processing your message. Please try again.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔄 Retry", callback_data="retry")]]
            ),
        )
