"""Inline keyboard callback handlers: retry and reschedule actions."""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.ai.parser import ParsedEvent
from app.bot.event_processor import process_and_save
from app.bot.formatters import format_event_time_range
from app.calendar.service import CalendarService, get_timezone
from app.config import Config

logger = logging.getLogger(__name__)


async def handle_retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-run process_and_save on the original message."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if user_id not in Config.ALLOWED_USER_IDS:
        logger.warning(f"Unauthorized retry attempt by User ID: {user_id}")
        return

    original_message = query.message.reply_to_message
    if not original_message:
        await query.message.reply_text("❌ Error: Original message not found. Cannot retry.")
        return

    text = original_message.text or original_message.caption
    if not text:
        await query.message.reply_text("❌ Error: Original message has no text. Cannot retry.")
        return

    await query.message.edit_text("Parsing message with AI (Retry)...")
    await process_and_save(user_id, text, query.message, context)


async def handle_reschedule_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle reschedule/duplicate/collision confirmation callbacks."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if user_id not in Config.ALLOWED_USER_IDS:
        logger.warning(f"Unauthorized callback attempt by User ID: {user_id}")
        return

    data = query.data
    if "_reschedule_" not in data:
        return
    action, rest = data.split("_reschedule_", 1)
    if not action:
        return

    parts = rest.split("_")
    tx_id = "_".join(parts[:4]) if len(parts) >= 4 else rest

    tx_data = context.user_data.get(tx_id)
    if not tx_data:
        await query.message.edit_text("❌ Session expired or transaction not found.")
        return

    context.user_data.pop(tx_id, None)

    event = ParsedEvent(**tx_data["event"])
    candidate_id: str | None = tx_data.get("candidate_id")
    old_time_str: str | None = tx_data.get("old_time_str")

    tz = get_timezone(Config.TIMEZONE)
    calendar = CalendarService(user_id=user_id)

    try:
        if action == "confirm":
            event_link = calendar.reschedule_event(candidate_id, event)
            collisions = calendar.check_collisions(event, exclude_event_id=candidate_id)

            report = f"🔄 *Event Rescheduled: {event.summary}*\n• *Old Time*: {old_time_str}\n"
            if event.is_all_day:
                report += f"• *New Date*: {event.start_date}\n"
            else:
                report += f"• *New Start*: {event.start_datetime}\n• *New End*: {event.end_datetime}\n"
            report += f"🔗 [Open in Google Calendar]({event_link})"
            if collisions:
                col_links = [
                    f"[{c.get('summary', '(No Title)')}]({c.get('htmlLink', '')})"
                    for c in collisions
                ]
                report += f"\n⚠️ *Collides with:* {', '.join(col_links)}"
            await query.message.edit_text(
                report, parse_mode="Markdown", disable_web_page_preview=True
            )

        elif action in ("create", "forcecreate"):
            event_link = calendar.create_event(event)
            collisions = calendar.check_collisions(event)

            report = f"📅 *Event Created (New): {event.summary}*\n"
            if event.is_all_day:
                report += f"• *Date*: {event.start_date}\n"
            else:
                report += f"• *Start*: {event.start_datetime}\n• *End*: {event.end_datetime}\n"
            report += f"🔗 [Open in Google Calendar]({event_link})"
            if collisions:
                col_links = [
                    f"[{c.get('summary', '(No Title)')}]({c.get('htmlLink', '')})"
                    for c in collisions
                ]
                report += f"\n⚠️ *Collides with:* {', '.join(col_links)}"
            await query.message.edit_text(
                report, parse_mode="Markdown", disable_web_page_preview=True
            )

        elif action == "mergeall":
            colliding_ids = tx_data.get("colliding_ids", [])
            if not colliding_ids:
                await query.message.edit_text("❌ No colliding events to merge.")
                return

            main_event = calendar.get_event(colliding_ids[0])
            summaries = [main_event.get("summary", "(No Title)")]
            descriptions = [main_event.get("description", "")]
            locations = [main_event.get("location", "")]

            for other_id in colliding_ids[1:]:
                try:
                    other = calendar.get_event(other_id)
                    summaries.append(other.get("summary", "(No Title)"))
                    if other.get("description"):
                        descriptions.append(other["description"])
                    if other.get("location"):
                        locations.append(other["location"])
                    calendar.delete_event(other_id)
                except Exception as ex:
                    logger.warning(f"Failed to fetch/delete colliding event {other_id}: {ex}")

            summaries.append(event.summary)
            if event.description:
                descriptions.append(event.description)
            if event.location:
                locations.append(event.location)

            body: dict = {
                "summary": " & ".join(filter(None, summaries)),
                "description": "\n---\nMerged event details:\n" + "\n---\n".join(filter(None, descriptions)),
            }
            merged_loc = ", ".join(set(filter(None, locations)))
            if merged_loc:
                body["location"] = merged_loc

            calendar.patch_event(main_event["id"], body)

            if candidate_id:
                calendar.delete_event(candidate_id)
                report = (
                    f"🤝 *Events Merged*\n\n"
                    f"Merged details into *{main_event.get('summary')}*.\n"
                    "Original candidate and colliding events were cleaned up."
                )
            else:
                report = (
                    f"🤝 *Events Merged*\n\n"
                    f"Merged details into *{main_event.get('summary')}*.\n"
                    "Other colliding events were cleaned up."
                )
            await query.message.edit_text(report, parse_mode="Markdown")

        elif action == "deleteall":
            colliding_ids = tx_data.get("colliding_ids", [])
            for c_id in colliding_ids:
                try:
                    calendar.delete_event(c_id)
                except Exception as ex:
                    logger.warning(f"Failed to delete colliding event {c_id}: {ex}")

            if candidate_id:
                event_link = calendar.reschedule_event(candidate_id, event)
                report = (
                    f"🗑️ *Colliding Events Deleted & Rescheduled*\n\n"
                    f"Deleted {len(colliding_ids)} colliding event(s).\n"
                    f"Rescheduled *{event.summary}*.\n"
                    f"🔗 [Open in Google Calendar]({event_link})"
                )
            else:
                event_link = calendar.create_event(event)
                report = (
                    f"🗑️ *Colliding Events Deleted & Created*\n\n"
                    f"Deleted {len(colliding_ids)} colliding event(s).\n"
                    f"Created new event *{event.summary}*.\n"
                    f"🔗 [Open in Google Calendar]({event_link})"
                )
            await query.message.edit_text(
                report, parse_mode="Markdown", disable_web_page_preview=True
            )

        elif action == "cancel":
            await query.message.edit_text(
                f"❌ Cancelled for *{event.summary}*.", parse_mode="Markdown"
            )

    except Exception:
        logger.exception("Failed to process reschedule callback")
        await query.message.edit_text("❌ Error processing action. Please try again.")
