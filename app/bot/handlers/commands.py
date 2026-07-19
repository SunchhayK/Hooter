"""Telegram command handlers: /start, /today, /tomorrow, /schedule, /auth, /status."""

import logging
import os
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import ContextTypes
from google_auth_oauthlib.flow import Flow

from app.bot.event_processor import query_and_display
from app.bot.formatters import build_tasks_keyboard
from app.bot.oauth.manager import OAuthManager
from app.calendar.service import CalendarService, get_timezone, SCOPES
from app.config import Config

logger = logging.getLogger(__name__)

_UNAUTHORIZED_MSG = "Unauthorized."


def _check_auth(user_id: int, action: str) -> bool:
    """Return True if authorized, log a warning otherwise."""
    if user_id not in Config.ALLOWED_USER_IDS:
        logger.warning(f"Unauthorized {action} attempt by User ID: {user_id}")
        return False
    return True


async def command_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not _check_auth(user_id, "start"):
        await update.message.reply_text(_UNAUTHORIZED_MSG)
        return
    await update.message.reply_text(
        "Hi! Forward any schedule update or task message to me. "
        "I will parse it using AI and save it to your Google Calendar."
    )


async def command_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not _check_auth(user_id, "today"):
        await update.message.reply_text(_UNAUTHORIZED_MSG)
        return
    status_message = await update.message.reply_text("Fetching schedule for today...")
    tz = get_timezone(Config.TIMEZONE)
    now = datetime.now(tz)
    await query_and_display(
        user_id,
        now.replace(hour=0, minute=0, second=0, microsecond=0),
        now.replace(hour=23, minute=59, second=59, microsecond=999999),
        None,
        status_message,
        "Today's Schedule",
    )


async def command_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not _check_auth(user_id, "tomorrow"):
        await update.message.reply_text(_UNAUTHORIZED_MSG)
        return
    status_message = await update.message.reply_text("Fetching schedule for tomorrow...")
    tz = get_timezone(Config.TIMEZONE)
    tomorrow = datetime.now(tz) + timedelta(days=1)
    await query_and_display(
        user_id,
        tomorrow.replace(hour=0, minute=0, second=0, microsecond=0),
        tomorrow.replace(hour=23, minute=59, second=59, microsecond=999999),
        None,
        status_message,
        "Tomorrow's Schedule",
    )


async def command_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not _check_auth(user_id, "schedule"):
        await update.message.reply_text(_UNAUTHORIZED_MSG)
        return
    status_message = await update.message.reply_text("Fetching upcoming schedule...")
    tz = get_timezone(Config.TIMEZONE)
    now = datetime.now(tz)
    await query_and_display(
        user_id, now, now + timedelta(days=7), None, status_message, "Upcoming Schedule (7 Days)"
    )


async def command_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not _check_auth(user_id, "auth"):
        await update.message.reply_text(_UNAUTHORIZED_MSG)
        return

    credentials_file = "credentials.json"
    if not os.path.exists(credentials_file):
        await update.message.reply_text(
            "❌ Error: `credentials.json` not found on server.\n"
            "Please upload the credentials file to the server first."
        )
        return

    try:
        flow = Flow.from_client_secrets_file(
            credentials_file, scopes=SCOPES, redirect_uri=Config.GOOGLE_REDIRECT_URI
        )
        auth_url, state = flow.authorization_url(
            prompt="select_account consent", access_type="offline"
        )

        OAuthManager.add(state, user_id, flow, update.message.chat_id)
        context.user_data["oauth_flow"] = flow

        await update.message.reply_text(
            "🔑 *Google Calendar Authentication*\n\n"
            f"1. Click [this link]({auth_url}) to log in and authorize.\n\n"
            "⚠️ *Note*: If you see an 'Access blocked' or 'Unverified app' error, "
            "add your Google account as a *Test User* in Google Cloud Console "
            "(OAuth consent screen).\n\n"
            "2. After authorizing, Google will redirect back automatically. "
            "The bot will notify you here once connected! ✅",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except Exception:
        logger.exception("Failed to start OAuth flow")
        await update.message.reply_text(
            "❌ Failed to initiate OAuth. Check bot logs for details."
        )


async def command_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not _check_auth(user_id, "status"):
        await update.message.reply_text(_UNAUTHORIZED_MSG)
        return

    status_message = await update.message.reply_text("Checking connection status...")
    try:
        calendar = CalendarService(user_id=user_id)
        info = calendar.get_connection_status()

        user_token = f"tokens/token_{user_id}.json"
        token_type = "Personal Account" if os.path.exists(user_token) else "Default Shared Account"

        await status_message.edit_text(
            "🔌 *Google Calendar Connection Status*\n\n"
            f"• *Auth Level*: `{token_type}`\n"
            f"• *User Email*: `{info.get('user_email')}`\n"
            f"• *User Name*: `{info.get('user_name')}`\n"
            f"• *Calendar Name*: `{info.get('calendar_summary')}`\n"
            f"• *Calendar ID*: `{info.get('calendar_id')}`\n"
            f"• *Timezone*: `{info.get('calendar_timezone')}`\n",
            parse_mode="Markdown",
        )
    except FileNotFoundError:
        await status_message.edit_text(
            "❌ Status: Not Connected.\nPlease run /auth to authorize your Google Calendar."
        )
    except Exception:
        logger.exception("Failed to get connection status")
        await status_message.edit_text("❌ Error checking status. Please try again.")


async def command_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not _check_auth(user_id, "tasks"):
        await update.message.reply_text(_UNAUTHORIZED_MSG)
        return

    status_message = await update.message.reply_text("Fetching your tasks...")
    try:
        calendar = CalendarService(user_id=user_id)
        tasks = calendar.list_tasks(show_completed=False)
        text, reply_markup = build_tasks_keyboard(tasks)

        await status_message.edit_text(
            text, parse_mode="Markdown", reply_markup=reply_markup
        )
    except FileNotFoundError:
        await status_message.edit_text(
            "❌ Not Connected.\nPlease run /auth to authorize your Google Calendar."
        )
    except Exception:
        logger.exception("Failed to list tasks")
        await status_message.edit_text("❌ Error fetching tasks. Please try again.")


