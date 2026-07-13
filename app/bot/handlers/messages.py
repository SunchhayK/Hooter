"""Telegram text message handler."""

import logging
import os

from telegram import Update
from telegram.ext import ContextTypes

from app.bot.event_processor import process_and_save
from app.calendar.service import _write_token
from app.config import Config

logger = logging.getLogger(__name__)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route incoming text/caption messages to OAuth exchange or AI → Calendar pipeline."""
    user_id = update.effective_user.id
    if user_id not in Config.ALLOWED_USER_IDS:
        logger.warning(f"Unauthorized message attempt by User ID: {user_id}")
        await update.message.reply_text("Unauthorized.")
        return

    message = update.message
    text = message.text or message.caption

    if not text:
        await message.reply_text("Please send text or a caption describing the event.")
        return

    logger.info(f"Received message from User ID {user_id}: {text[:100]}")

    # Check if this is a manually pasted Google OAuth redirect URL.
    # Require http prefix to avoid false positives like "what's error code=5".
    cleaned_text = text.strip()
    if cleaned_text.startswith("http") and (
        "localhost" in cleaned_text or "code=" in cleaned_text
    ):
        flow = context.user_data.get("oauth_flow")
        if not flow:
            await message.reply_text(
                "❌ Session expired. Please run /auth again to start a new authentication session."
            )
            return

        status_message = await message.reply_text("Exchanging authorization code...")
        try:
            flow.fetch_token(authorization_response=cleaned_text)
            creds = flow.credentials
            token_path = f"tokens/token_{user_id}.json"
            _write_token(token_path, creds.to_json())
            context.user_data.pop("oauth_flow", None)
            await status_message.edit_text(
                "✅ Success! Your Google Calendar has been connected."
            )
        except Exception:
            logger.exception("OAuth code exchange failed")
            await status_message.edit_text(
                "❌ Failed to exchange code. Please run /auth and try again."
            )
        return

    status_message = await message.reply_text("Parsing message with AI...")
    await process_and_save(user_id, text, status_message, context)
