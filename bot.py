import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from config import Config
from ai_parser import AIParserFactory
from calendar_service import CalendarService, get_timezone

# Setup logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    user_id = update.effective_user.id
    if user_id not in Config.ALLOWED_USER_IDS:
        logger.warning(f"Unauthorized start attempt by User ID: {user_id}")
        await update.message.reply_text("Unauthorized. You cannot use this bot.")
        return

    await update.message.reply_text(
        "Hi! Forward any schedule update or task message to me. "
        "I will parse it using AI and save it to your Google Calendar."
    )


async def process_and_save(text: str, status_message) -> None:
    """Core logic to parse text with AI and save to Google Calendar."""
    try:
        # Get active parser
        parser = AIParserFactory.get_parser()

        # Get current local time in configured timezone
        tz = get_timezone(Config.TIMEZONE)
        now = datetime.now(tz)
        reference_time_str = now.strftime("%Y-%m-%d %H:%M:%S (%A)")

        # Parse the message
        events = parser.parse_message(text, reference_time_str, Config.TIMEZONE)

        if not events:
            await status_message.edit_text("❌ No events found in this message.")
            return

        await status_message.edit_text(
            f"AI parsed {len(events)} event(s). Adding to Google Calendar..."
        )

        calendar = CalendarService()
        success_reports = []

        for idx, event in enumerate(events, start=1):
            event_link = calendar.create_event(event)

            # Format report for this event
            report = f"📅 *Event #{idx}: {event.summary}*\n"
            if event.is_all_day:
                report += f"• *Date*: {event.start_date}\n"
            else:
                report += (
                    f"• *Start*: {event.start_datetime}\n"
                    f"• *End*: {event.end_datetime}\n"
                )
            if event.location:
                report += f"• *Location*: {event.location}\n"
            if event.description:
                report += f"• *Description*: {event.description}\n"
            report += f"🔗 [Open in Google Calendar]({event_link})"

            success_reports.append(report)

        final_response = "\n\n".join(success_reports)
        await status_message.edit_text(final_response, parse_mode="Markdown")

    except FileNotFoundError as e:
        logger.error(f"Credentials missing: {e}")
        keyboard = [[InlineKeyboardButton("🔄 Retry", callback_data="retry")]]
        await status_message.edit_text(
            "❌ Failed: Google Calendar credentials (`token.json`) not found.\n"
            "Please run `setup_oauth.py` to authorize.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as e:
        logger.exception("Failed to parse or create event")
        keyboard = [[InlineKeyboardButton("🔄 Retry", callback_data="retry")]]
        await status_message.edit_text(
            f"❌ Error: {str(e)}", reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming messages (text and captions) and forward to AI / GCal."""
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

    status_message = await message.reply_text("Parsing message with AI...")
    await process_and_save(text, status_message)


async def handle_retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Retry inline button clicks."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if user_id not in Config.ALLOWED_USER_IDS:
        logger.warning(f"Unauthorized retry attempt by User ID: {user_id}")
        return

    original_message = query.message.reply_to_message
    if not original_message:
        await query.message.reply_text(
            "❌ Error: Original message not found. Cannot retry."
        )
        return

    text = original_message.text or original_message.caption
    if not text:
        await query.message.reply_text(
            "❌ Error: Original message has no text. Cannot retry."
        )
        return

    status_message = query.message
    # Remove the retry button while retrying to prevent double click
    await status_message.edit_text("Parsing message with AI (Retry)...")
    await process_and_save(text, status_message)


def main() -> None:
    Config.validate()

    # Initialize Application
    application = Application.builder().token(Config.TELEGRAM_BOT_TOKEN).build()

    # Register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_retry, pattern="^retry$"))
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_message
        )
    )

    # Start the bot
    logger.info("Bot started. Polling updates...")
    application.run_polling()


if __name__ == "__main__":
    main()
