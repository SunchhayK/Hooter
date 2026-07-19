"""Telegram text message handler."""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.bot.event_processor import process_and_save
from app.calendar.service import _write_token
from app.config import Config

logger = logging.getLogger(__name__)


def _extract_forward_sender(message) -> str | None:
    """Extract a human-readable sender name from a forwarded message.

    Returns None if the message is not forwarded.
    Priority: forward_origin (TG Bot API v6.5+) > forward_from user > forward_sender_name.
    """
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        # forward_origin is a MessageOrigin union type
        origin_type = getattr(origin, "type", None)
        if origin_type == "user":
            user = getattr(origin, "sender_user", None)
            if user:
                parts = filter(None, [user.first_name, user.last_name])
                return " ".join(parts).strip() or None
        if origin_type == "channel":
            chat = getattr(origin, "chat", None)
            if chat:
                return getattr(chat, "title", None)
        if origin_type == "chat":
            author = getattr(origin, "author_signature", None)
            chat = getattr(origin, "sender_chat", None)
            return author or (getattr(chat, "title", None) if chat else None)
        if origin_type == "hidden_user":
            return getattr(origin, "sender_user_name", None)

    # Fallback for older API versions
    fwd_from = getattr(message, "forward_from", None)
    if fwd_from:
        parts = filter(None, [fwd_from.first_name, fwd_from.last_name])
        return " ".join(parts).strip() or None

    return getattr(message, "forward_sender_name", None)


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

        status_message = await message.reply_text(
            "Exchanging authorization code...", reply_to_message_id=message.message_id
        )
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

    # If this is a forwarded message, prepend sender context so the AI
    # can embed it in the event description.
    forward_sender = _extract_forward_sender(message)
    if forward_sender:
        logger.info(f"Forwarded message from '{forward_sender}' for user {user_id}")
        cleaned_text = f"[Forwarded from: {forward_sender}]\n{cleaned_text}"

    status_message = await message.reply_text(
        "Parsing message with AI...", reply_to_message_id=message.message_id
    )
    await process_and_save(user_id, cleaned_text, status_message, context)
