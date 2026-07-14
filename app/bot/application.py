"""Telegram Application factory: wires handlers, OAuth server, and bot commands."""

import asyncio
import logging

from telegram import BotCommand
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from app.bot.handlers.callbacks import handle_reschedule_callback, handle_retry
from app.bot.handlers.commands import (
    command_auth,
    command_schedule,
    command_start,
    command_status,
    command_today,
    command_tomorrow,
)
from app.bot.handlers.messages import handle_message
from app.bot.oauth.server import set_telegram_notifier, start_callback_server
from app.config import Config

logger = logging.getLogger(__name__)


async def post_init(application: Application) -> None:
    """Called once after the Application starts; injects notifier into OAuth server."""
    loop = asyncio.get_running_loop()
    set_telegram_notifier(application.bot.send_message, loop)

    commands = [
        BotCommand("today", "Show today's schedule"),
        BotCommand("tomorrow", "Show tomorrow's schedule"),
        BotCommand("schedule", "Show schedule for next 7 days"),
        BotCommand("list", "Show schedule for next 7 days"),
        BotCommand("status", "Check connected Google account/calendar"),
        BotCommand("auth", "Connect Google Calendar account"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Bot commands registered.")

    from app.bot.scheduler import start_scheduler

    asyncio.create_task(start_scheduler(application))


def build_application() -> Application:
    """Build and return the fully configured Telegram Application."""
    app = (
        Application.builder()
        .token(Config.TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", command_start))
    app.add_handler(CommandHandler("auth", command_auth))
    app.add_handler(CommandHandler("status", command_status))
    app.add_handler(CommandHandler("whoami", command_status))
    app.add_handler(CommandHandler("today", command_today))
    app.add_handler(CommandHandler("tomorrow", command_tomorrow))
    app.add_handler(CommandHandler("schedule", command_schedule))
    app.add_handler(CommandHandler("list", command_schedule))
    app.add_handler(CallbackQueryHandler(handle_retry, pattern="^retry$"))
    app.add_handler(
        CallbackQueryHandler(handle_reschedule_callback, pattern="^.*_reschedule_.*$")
    )
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_message
        )
    )

    return app


def run() -> None:
    """Validate config, start OAuth server, and start polling."""
    Config.validate()
    start_callback_server()
    app = build_application()
    logger.info("Bot started. Polling updates...")
    app.run_polling()
