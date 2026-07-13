import logging
import os
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from google_auth_oauthlib.flow import InstalledAppFlow
from config import Config
from ai_parser import AIParserFactory
from calendar_service import CalendarService, get_timezone, SCOPES


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


def format_event_list(events: list, tz, start_date=None, end_date=None) -> str:
    """Format Google Calendar events grouped by date in Markdown, including blank dates."""
    grouped = {}

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

        if dt not in grouped:
            grouped[dt] = []
        grouped[dt].append(event)

    sorted_dates = sorted(grouped.keys())
    if not sorted_dates:
        return "📅 No events found in this period."

    output_parts = []
    for d in sorted_dates:
        date_header = d.strftime("%A, %b %d")
        day_events = []

        events_for_day = grouped[d]
        if not events_for_day:
            day_events.append("  • _No events_")
        else:
            for event in events_for_day:
                summary = event.get("summary", "(No Title)")
                start = event.get("start", {})
                html_link = event.get("htmlLink")

                summary_link = f"[{summary}]({html_link})" if html_link else summary

                if "date" in start:
                    day_events.append(f"  • *[All Day]* {summary_link}")
                else:
                    start_dt = datetime.fromisoformat(start["dateTime"]).astimezone(tz)
                    time_str = start_dt.strftime("%I:%M %p")
                    day_events.append(f"  • *[{time_str}]* {summary_link}")

        output_parts.append(f"📅 *{date_header}*\n" + "\n".join(day_events))

    return "\n\n".join(output_parts)


async def query_and_display(
    user_id: int,
    time_min: datetime,
    time_max: datetime,
    search_query: str,
    status_message,
    header_text: str = "Schedule",
) -> None:
    """Fetch and display calendar events for specified criteria."""
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
    except FileNotFoundError as e:
        logger.error(f"Credentials missing: {e}")
        await status_message.edit_text(
            "❌ Failed: Google Calendar credentials (`token.json`) not found.\n"
            "Please run `setup_oauth.py` to authorize."
        )
    except Exception as e:
        logger.exception("Failed to query events")
        await status_message.edit_text(f"❌ Error: {str(e)}")


async def command_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch and display schedule for today."""
    user_id = update.effective_user.id
    if user_id not in Config.ALLOWED_USER_IDS:
        logger.warning(f"Unauthorized today command attempt by User ID: {user_id}")
        await update.message.reply_text("Unauthorized.")
        return

    status_message = await update.message.reply_text("Fetching schedule for today...")
    tz = get_timezone(Config.TIMEZONE)
    now = datetime.now(tz)
    time_min = now.replace(hour=0, minute=0, second=0, microsecond=0)
    time_max = now.replace(hour=23, minute=59, second=59, microsecond=999999)
    await query_and_display(
        user_id, time_min, time_max, None, status_message, "Today's Schedule"
    )


async def command_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch and display schedule for tomorrow."""
    user_id = update.effective_user.id
    if user_id not in Config.ALLOWED_USER_IDS:
        logger.warning(f"Unauthorized tomorrow command attempt by User ID: {user_id}")
        await update.message.reply_text("Unauthorized.")
        return

    status_message = await update.message.reply_text(
        "Fetching schedule for tomorrow..."
    )
    tz = get_timezone(Config.TIMEZONE)
    now = datetime.now(tz)
    tomorrow = now + timedelta(days=1)
    time_min = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
    time_max = tomorrow.replace(hour=23, minute=59, second=59, microsecond=999999)
    await query_and_display(
        user_id, time_min, time_max, None, status_message, "Tomorrow's Schedule"
    )


async def command_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch and display schedule for the next 7 days."""
    user_id = update.effective_user.id
    if user_id not in Config.ALLOWED_USER_IDS:
        logger.warning(f"Unauthorized schedule command attempt by User ID: {user_id}")
        await update.message.reply_text("Unauthorized.")
        return

    status_message = await update.message.reply_text("Fetching upcoming schedule...")
    tz = get_timezone(Config.TIMEZONE)
    now = datetime.now(tz)
    time_min = now
    time_max = now + timedelta(days=7)
    await query_and_display(
        user_id, time_min, time_max, None, status_message, "Upcoming Schedule (7 Days)"
    )


async def process_and_save(user_id: int, text: str, status_message) -> None:
    """Core logic to parse text with AI and either query or save to Google Calendar."""
    try:
        # Get active parser
        parser = AIParserFactory.get_parser()

        # Get current local time in configured timezone
        tz = get_timezone(Config.TIMEZONE)
        now = datetime.now(tz)
        reference_time_str = now.strftime("%Y-%m-%d %H:%M:%S (%A)")

        # Parse the message
        ai_res = parser.parse_message(text, reference_time_str, Config.TIMEZONE)

        if ai_res.intent == "query":
            # Extract query range
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

            # Default to from now onwards if no bounds given
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

            response = f"✨ *{header}*\n\n{formatted_list}"
            await status_message.edit_text(
                response, parse_mode="Markdown", disable_web_page_preview=True
            )
            return

        # Otherwise, intent == "create"
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
        await status_message.edit_text(
            final_response, parse_mode="Markdown", disable_web_page_preview=True
        )

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
    """Handle incoming messages (text and captions) and forward to AI / GCal or handle OAuth."""
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

    # Check if this is a Google OAuth callback redirect URL
    if text.startswith("http://localhost") or "code=" in text:
        flow = context.user_data.get("oauth_flow")
        if not flow:
            try:
                flow = InstalledAppFlow.from_client_secrets_file(
                    "credentials.json", scopes=SCOPES, redirect_uri="http://localhost"
                )
            except Exception as _:
                await message.reply_text(
                    "❌ Session expired or `credentials.json` missing. Please run /auth again."
                )
                return

        status_message = await message.reply_text("Exchanging authorization code...")
        try:
            flow.fetch_token(authorization_response=text)
            creds = flow.credentials
            token_path = f"token_{user_id}.json"
            with open(token_path, "w") as token:
                token.write(creds.to_json())

            context.user_data.pop("oauth_flow", None)
            await status_message.edit_text(
                "✅ Success! Your Google Calendar has been connected."
            )
        except Exception as e:
            logger.exception("OAuth code exchange failed")
            await status_message.edit_text(
                f"❌ Failed to exchange code: {str(e)}\n\nPlease run /auth and try again."
            )
        return

    status_message = await message.reply_text("Parsing message with AI...")
    await process_and_save(user_id, text, status_message)


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
    await process_and_save(user_id, text, status_message)


async def command_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send instructions and URL to authenticate Google Calendar."""
    user_id = update.effective_user.id
    if user_id not in Config.ALLOWED_USER_IDS:
        logger.warning(f"Unauthorized auth command attempt by User ID: {user_id}")
        await update.message.reply_text("Unauthorized.")
        return

    credentials_file = "credentials.json"
    if not os.path.exists(credentials_file):
        await update.message.reply_text(
            "❌ Error: `credentials.json` not found on server.\n"
            "Please upload the credentials file to the server first."
        )
        return

    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            credentials_file, scopes=SCOPES, redirect_uri="http://localhost"
        )
        auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")

        context.user_data["oauth_flow"] = flow

        message = (
            "🔑 *Google Calendar Authentication*\n\n"
            f"1. Click [this link]({auth_url}) to log in and authorize.\n\n"
            "2. After authorizing, your browser will redirect to a broken page "
            "(e.g., `http://localhost/?state=...&code=...`).\n\n"
            "3. Copy the *FULL* redirect URL from your browser's address bar and paste it here."
        )
        await update.message.reply_text(
            message, parse_mode="Markdown", disable_web_page_preview=True
        )
    except Exception as e:
        logger.exception("Failed to start OAuth flow")
        await update.message.reply_text(f"❌ Failed to initiate OAuth: {str(e)}")


async def post_init(application: Application) -> None:
    """Set bot command menu."""
    commands = [
        BotCommand("today", "Show today's schedule"),
        BotCommand("tomorrow", "Show tomorrow's schedule"),
        BotCommand("schedule", "Show schedule for next 7 days"),
        BotCommand("list", "Show schedule for next 7 days"),
        BotCommand("auth", "Connect Google Calendar account"),
    ]
    await application.bot.set_my_commands(commands)


def main() -> None:
    Config.validate()

    # Initialize Application
    application = (
        Application.builder()
        .token(Config.TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("auth", command_auth))
    application.add_handler(CommandHandler("today", command_today))
    application.add_handler(CommandHandler("tomorrow", command_tomorrow))
    application.add_handler(CommandHandler("schedule", command_schedule))
    application.add_handler(CommandHandler("list", command_schedule))
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
