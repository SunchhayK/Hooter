import asyncio
import logging
import os
import time
from http.server import BaseHTTPRequestHandler

# Allow insecure transport for OAuth localhost redirect URI only.
# If GOOGLE_REDIRECT_URI is changed to HTTPS, remove this.
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
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


# Globals for automatic OAuth callback server
# Each entry: state -> (user_id, flow, chat_id, inserted_at_timestamp)
PENDING_STATES: dict = {}
PENDING_STATES_TTL_SECONDS = 600  # 10 minutes
application_instance = None


def _prune_pending_states() -> None:
    """Remove expired OAuth state entries."""
    cutoff = time.time() - PENDING_STATES_TTL_SECONDS
    expired = [k for k, v in PENDING_STATES.items() if v[3] < cutoff]
    for k in expired:
        PENDING_STATES.pop(k, None)
    if expired:
        logger.info(f"Pruned {len(expired)} expired OAuth state(s)")


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        logger.info(f"HTTP Server - {format % args}")

    def do_GET(self):
        import urllib.parse

        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)

        state_list = params.get("state")
        code_list = params.get("code")

        state = state_list[0] if state_list else None
        code = code_list[0] if code_list else None

        if not state or not code:
            self.send_response(400)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h1>Error</h1><p>Missing state or code parameter.</p>")
            return

        _prune_pending_states()
        val = PENDING_STATES.pop(state, None)
        if not val:
            self.send_response(400)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<h1>Error</h1><p>Invalid or expired state session. Please run /auth again.</p>"
            )
            return

        user_id, flow, chat_id, _inserted_at = val

        try:
            # Reconstruct the redirect URL Google redirected to
            auth_response = f"{Config.GOOGLE_REDIRECT_URI.rstrip('/')}{self.path}"

            flow.fetch_token(authorization_response=auth_response)
            creds = flow.credentials

            token_path = f"tokens/token_{user_id}.json"
            os.makedirs(os.path.dirname(token_path), exist_ok=True)
            # 0o600: owner read/write only — prevent other processes reading tokens
            fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as token:
                token.write(creds.to_json())

            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><head><title>Success</title></head>"
                b'<body style="font-family: Arial, sans-serif; text-align: center; padding-top: 50px;">'
                b'<h1 style="color: #4CAF50;">\xe2\x9c\x85 Authentication Successful!</h1>'
                b"<p>Your Google Calendar account is now connected.</p>"
                b"<p>You can close this tab and return to Telegram.</p>"
                b"</body></html>"
            )

            # Send Telegram success notification
            if application_instance:
                asyncio.run_coroutine_threadsafe(
                    application_instance.bot.send_message(
                        chat_id=chat_id,
                        text="✅ *Success!* Your Google Calendar has been connected automatically.",
                        parse_mode="Markdown",
                    ),
                    application_instance.loop,
                )

        except Exception as e:
            import html

            logger.exception("Failed to exchange code in HTTP server")
            self.send_response(500)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            safe_err = html.escape(str(e))
            self.wfile.write(
                f"<h1>Error</h1><p>Failed to exchange authorization code: {safe_err}</p>".encode(
                    "utf-8"
                )
            )

            if application_instance:
                asyncio.run_coroutine_threadsafe(
                    application_instance.bot.send_message(
                        chat_id=chat_id,
                        text="❌ *Failed to connect Google Calendar.* Please run /auth again.",
                        parse_mode="Markdown",
                    ),
                    application_instance.loop,
                )


def start_callback_server():
    import urllib.parse
    from http.server import ThreadingHTTPServer
    import threading

    parsed = urllib.parse.urlparse(Config.GOOGLE_REDIRECT_URI)
    port = parsed.port or 6767

    # Bind to 0.0.0.0 so Docker port mapping (host:6767 -> container:6767) can reach the server.
    # 127.0.0.1 would silently drop all mapped traffic because Docker routes via the container's
    # eth0 interface, not loopback.
    # Security: the cryptographically-random state token (TTL 10 min, single-use) acts as CSRF
    # protection — an attacker cannot forge a valid callback without knowing the state.
    server = ThreadingHTTPServer(("0.0.0.0", port), OAuthCallbackHandler)
    logger.info(f"Starting Google OAuth callback server on 0.0.0.0:{port}...")
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()


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
            "❌ Failed: Google Calendar credentials (`tokens/token.json`) not found.\n"
            "Please run `setup_oauth.py` to authorize."
        )
    except Exception as e:
        logger.exception("Failed to query events")
        await status_message.edit_text("❌ Error fetching events. Please try again.")


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


def format_event_time_range(event_dict_or_parsed_event, tz) -> str:
    """Format start/end time of event dict or ParsedEvent to a friendly string."""
    if hasattr(event_dict_or_parsed_event, "is_all_day"):
        if event_dict_or_parsed_event.is_all_day:
            return event_dict_or_parsed_event.start_date
        else:
            return f"{event_dict_or_parsed_event.start_datetime} to {event_dict_or_parsed_event.end_datetime}"
    else:
        start = event_dict_or_parsed_event.get("start", {})
        if "date" in start:
            return start["date"]
        else:
            dt = datetime.fromisoformat(
                start["dateTime"].replace("Z", "+00:00")
            ).astimezone(tz)
            return dt.strftime("%Y-%m-%d %H:%M:%S")


async def process_and_save(
    user_id: int, text: str, status_message, context: ContextTypes.DEFAULT_TYPE
) -> None:
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
            candidate, is_dup = calendar.find_reschedule_candidate(event)

            if is_dup:
                dup_link = candidate.get("htmlLink", "")
                time_str = format_event_time_range(candidate, tz)

                tx_id = f"tx_{user_id}_{int(time.time())}_{idx}"
                context.user_data[tx_id] = {
                    "candidate_id": candidate["id"],
                    "event": event.model_dump(),
                    "old_time_str": time_str,
                }

                prompt_text = (
                    f"⚠️ *Duplicate Detected*\n\n"
                    f"An event named *{event.summary}* already exists at the same time.\n"
                    f"Do you want to create a new one anyway?\n"
                    f"• *Time*: {time_str}\n"
                )

                keyboard = [
                    [
                        InlineKeyboardButton(
                            "➕ Create Anyway",
                            callback_data=f"create_reschedule_{tx_id}",
                        ),
                        InlineKeyboardButton(
                            "❌ Cancel", callback_data=f"cancel_reschedule_{tx_id}"
                        ),
                    ],
                ]

                await status_message.get_bot().send_message(
                    chat_id=status_message.chat_id,
                    text=prompt_text,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            elif candidate:
                old_time_str = format_event_time_range(candidate, tz)
                new_time_str = format_event_time_range(event, tz)

                collisions = calendar.check_collisions(
                    event, exclude_event_id=candidate["id"]
                )

                tx_id = f"tx_{user_id}_{int(time.time())}_{idx}"
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
                        [
                            f"• *{c.get('summary', '(No Title)')}* ({format_event_time_range(c, tz)})"
                            for c in collisions
                        ]
                    )
                    prompt_text = (
                        f"⚠️ *Reschedule Collision Detected*\n\n"
                        f"Moving *{event.summary}* to {new_time_str} collides with existing event(s):\n"
                        f"{col_list_str}\n\n"
                        f"Select action:"
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
                else:
                    prompt_text = (
                        f"🔄 *Reschedule Confirmation*\n\n"
                        f"Do you want to reschedule *{event.summary}*?\n"
                        f"• *Old Time*: {old_time_str}\n"
                        f"• *New Time*: {new_time_str}\n"
                    )
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

                await status_message.get_bot().send_message(
                    chat_id=status_message.chat_id,
                    text=prompt_text,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            else:
                # New event. Check for collisions.
                collisions = calendar.check_collisions(event)
                new_time_str = format_event_time_range(event, tz)

                if collisions:
                    tx_id = f"tx_{user_id}_{int(time.time())}_{idx}"
                    context.user_data[tx_id] = {
                        "event": event.model_dump(),
                        "colliding_ids": [c["id"] for c in collisions],
                    }

                    col_list_str = "\n".join(
                        [
                            f"• *{c.get('summary', '(No Title)')}* ({format_event_time_range(c, tz)})"
                            for c in collisions
                        ]
                    )
                    prompt_text = (
                        f"⚠️ *Collision Detected*\n\n"
                        f"Creating *{event.summary}* ({new_time_str}) collides with existing event(s):\n"
                        f"{col_list_str}\n\n"
                        f"Select action:"
                    )
                    keyboard = [
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
                                "❌ Cancel", callback_data=f"cancel_reschedule_{tx_id}"
                            ),
                        ],
                    ]

                    await status_message.get_bot().send_message(
                        chat_id=status_message.chat_id,
                        text=prompt_text,
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                    )
                else:
                    event_link = calendar.create_event(event)
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

        if success_reports:
            final_response = "\n\n".join(success_reports)
            await status_message.edit_text(
                final_response, parse_mode="Markdown", disable_web_page_preview=True
            )
        else:
            try:
                await status_message.delete()
            except Exception:
                await status_message.edit_text("Reschedule confirmations sent.")

    except FileNotFoundError as e:
        logger.error(f"Credentials missing: {e}")
        keyboard = [[InlineKeyboardButton("🔄 Retry", callback_data="retry")]]
        await status_message.edit_text(
            "❌ Failed: Google Calendar credentials (`tokens/token.json`) not found.\n"
            "Please run `setup_oauth.py` to authorize.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as e:
        logger.exception("Failed to parse or create event")
        keyboard = [[InlineKeyboardButton("🔄 Retry", callback_data="retry")]]
        await status_message.edit_text(
            "❌ Error processing your message. Please try again.",
            reply_markup=InlineKeyboardMarkup(keyboard),
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

    logger.info(f"Received message from User ID {user_id}: {text[:100]}")

    # Check if this is a Google OAuth callback redirect URL pasted manually.
    # Require it to look like a URL (starts with http) to avoid false positives.
    cleaned_text = text.strip()
    if cleaned_text.startswith("http") and (
        "localhost" in cleaned_text or "code=" in cleaned_text
    ):
        flow = context.user_data.get("oauth_flow")
        if not flow:
            # No active session — reject instead of constructing a stateless flow
            await message.reply_text(
                "❌ Session expired. Please run /auth again to start a new authentication session."
            )
            return

        status_message = await message.reply_text("Exchanging authorization code...")
        try:
            flow.fetch_token(authorization_response=cleaned_text)
            creds = flow.credentials
            token_path = f"tokens/token_{user_id}.json"
            os.makedirs(os.path.dirname(token_path), exist_ok=True)
            # 0o600: owner read/write only
            fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as token:
                token.write(creds.to_json())

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
    await process_and_save(user_id, text, status_message, context)


async def handle_reschedule_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle reschedule confirmation callback query."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if user_id not in Config.ALLOWED_USER_IDS:
        logger.warning(f"Unauthorized callback attempt by User ID: {user_id}")
        return

    data = query.data
    action, rest = (
        data.split("_reschedule_", 1) if "_reschedule_" in data else (None, None)
    )
    if not action or not rest:
        return

    parts = rest.split("_")
    if len(parts) >= 4:
        tx_id = "_".join(parts[:4])
        colliding_id = parts[4] if len(parts) > 4 else None
    else:
        tx_id = rest
        colliding_id = None

    tx_data = context.user_data.get(tx_id)
    if not tx_data:
        await query.message.edit_text("❌ Session expired or transaction not found.")
        return

    context.user_data.pop(tx_id, None)

    event_dict = tx_data["event"]
    from ai_parser import ParsedEvent

    event = ParsedEvent(**event_dict)
    candidate_id = tx_data.get("candidate_id")
    old_time_str = tx_data.get("old_time_str")

    calendar = CalendarService(user_id=user_id)

    try:
        if action == "confirm":
            event_link = calendar.reschedule_event(candidate_id, event)
            collisions = calendar.check_collisions(event, exclude_event_id=candidate_id)

            report = f"🔄 *Event Rescheduled: {event.summary}*\n"
            report += f"• *Old Time*: {old_time_str}\n"
            if event.is_all_day:
                report += f"• *New Date*: {event.start_date}\n"
            else:
                report += (
                    f"• *New Start*: {event.start_datetime}\n"
                    f"• *New End*: {event.end_datetime}\n"
                )
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
                report += (
                    f"• *Start*: {event.start_datetime}\n"
                    f"• *End*: {event.end_datetime}\n"
                )
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

            # Retrieve first colliding event details
            main_col_event = calendar.get_event(colliding_ids[0])
            summaries = [main_col_event.get("summary", "(No Title)")]
            descriptions = [main_col_event.get("description", "")]
            locations = [main_col_event.get("location", "")]

            # Process rest of the colliding events and delete them
            for other_id in colliding_ids[1:]:
                try:
                    other_event = calendar.get_event(other_id)
                    summaries.append(other_event.get("summary", "(No Title)"))
                    if other_event.get("description"):
                        descriptions.append(other_event["description"])
                    if other_event.get("location"):
                        locations.append(other_event["location"])
                    calendar.delete_event(other_id)
                except Exception as ex:
                    logger.warning(
                        f"Failed to fetch/delete colliding event {other_id}: {ex}"
                    )

            # Append new event details
            summaries.append(event.summary)
            if event.description:
                descriptions.append(event.description)
            if event.location:
                locations.append(event.location)

            # Build patched body
            merged_summary = " & ".join(filter(None, summaries))
            merged_desc = "\n---\nMerged event details:\n" + "\n---\n".join(
                filter(None, descriptions)
            )
            merged_loc = ", ".join(set(filter(None, locations)))

            body = {
                "summary": merged_summary,
                "description": merged_desc,
            }
            if merged_loc:
                body["location"] = merged_loc

            calendar.patch_event(main_col_event["id"], body)

            # If it was a reschedule candidate, delete the candidate
            if candidate_id:
                calendar.delete_event(candidate_id)
                report = (
                    f"🤝 *Events Merged*\n\n"
                    f"Merged details into *{main_col_event.get('summary')}*.\n"
                    f"Original candidate and colliding events were cleaned up."
                )
            else:
                report = (
                    f"🤝 *Events Merged*\n\n"
                    f"Merged details into *{main_col_event.get('summary')}*.\n"
                    f"Other colliding events were cleaned up."
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
                f"❌ Cancelled for *{event.summary}*.",
                parse_mode="Markdown",
            )

    except Exception as e:
        logger.exception("Failed to process reschedule callback")
        await query.message.edit_text("❌ Error processing action. Please try again.")


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
            credentials_file, scopes=SCOPES, redirect_uri=Config.GOOGLE_REDIRECT_URI
        )
        auth_url, state = flow.authorization_url(
            prompt="select_account consent", access_type="offline"
        )

        _prune_pending_states()
        PENDING_STATES[state] = (user_id, flow, update.message.chat_id, time.time())
        context.user_data["oauth_flow"] = flow

        message = (
            "🔑 *Google Calendar Authentication*\n\n"
            f"1. Click [this link]({auth_url}) to log in and authorize.\n\n"
            "⚠️ *Note*: If you see an 'Access blocked' or 'Unverified app' error, you must add your Google account as a *Test User* in your Google Cloud Console (under 'OAuth consent screen').\n\n"
            "2. After authorizing, Google will redirect to localhost/your redirect page automatically. If it succeeds, the bot will notify you here directly!\n\n"
            "3. If automatic redirection fails, you can still copy the *FULL* redirect URL from your browser's address bar and paste it here."
        )
        await update.message.reply_text(
            message, parse_mode="Markdown", disable_web_page_preview=True
        )
    except Exception as e:
        logger.exception("Failed to start OAuth flow")
        await update.message.reply_text(
            "❌ Failed to initiate OAuth. Check bot logs for details."
        )


async def command_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check current auth connection status."""
    user_id = update.effective_user.id
    if user_id not in Config.ALLOWED_USER_IDS:
        logger.warning(f"Unauthorized status command attempt by User ID: {user_id}")
        await update.message.reply_text("Unauthorized.")
        return

    status_message = await update.message.reply_text("Checking connection status...")
    try:
        calendar = CalendarService(user_id=user_id)
        info = calendar.get_connection_status()

        user_token = f"tokens/token_{user_id}.json"
        token_type = (
            "Personal Account"
            if os.path.exists(user_token)
            else "Default Shared Account"
        )

        msg = (
            "🔌 *Google Calendar Connection Status*\n\n"
            f"• *Auth Level*: `{token_type}`\n"
            f"• *User Email*: `{info.get('user_email')}`\n"
            f"• *User Name*: `{info.get('user_name')}`\n"
            f"• *Calendar Name*: `{info.get('calendar_summary')}`\n"
            f"• *Calendar ID*: `{info.get('calendar_id')}`\n"
            f"• *Timezone*: `{info.get('calendar_timezone')}`\n"
        )
        await status_message.edit_text(msg, parse_mode="Markdown")
    except FileNotFoundError as e:
        await status_message.edit_text(
            "❌ Status: Not Connected.\n"
            "Please run /auth to authorize your Google Calendar."
        )
    except Exception as e:
        logger.exception("Failed to get connection status")
        await status_message.edit_text("❌ Error checking status. Please try again.")


async def post_init(application: Application) -> None:
    """Set bot command menu."""
    global application_instance
    application_instance = application
    application_instance.loop = asyncio.get_running_loop()

    commands = [
        BotCommand("today", "Show today's schedule"),
        BotCommand("tomorrow", "Show tomorrow's schedule"),
        BotCommand("schedule", "Show schedule for next 7 days"),
        BotCommand("list", "Show schedule for next 7 days"),
        BotCommand("status", "Check connected Google account/calendar"),
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
    application.add_handler(CommandHandler("status", command_status))
    application.add_handler(CommandHandler("whoami", command_status))
    application.add_handler(CommandHandler("today", command_today))
    application.add_handler(CommandHandler("tomorrow", command_tomorrow))
    application.add_handler(CommandHandler("schedule", command_schedule))
    application.add_handler(CommandHandler("list", command_schedule))
    application.add_handler(CallbackQueryHandler(handle_retry, pattern="^retry$"))
    application.add_handler(
        CallbackQueryHandler(
            handle_reschedule_callback,
            pattern="^.*_reschedule_.*$",
        )
    )
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_message
        )
    )

    # Start the local OAuth callback server
    start_callback_server()

    # Start the bot
    logger.info("Bot started. Polling updates...")
    application.run_polling()


if __name__ == "__main__":
    main()
