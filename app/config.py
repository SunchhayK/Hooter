import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class Config:
    # Telegram Bot Token
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

    # Whitelisted Telegram User IDs
    ALLOWED_USER_IDS: list[int] = []
    _allowed_ids_str = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
    if _allowed_ids_str:
        try:
            ALLOWED_USER_IDS = [
                int(x.strip()) for x in _allowed_ids_str.split(",") if x.strip()
            ]
        except ValueError:
            raise ValueError(
                "TELEGRAM_ALLOWED_USER_IDS must be a comma-separated list of integers."
            )

    # Google Calendar
    GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
    TIMEZONE = os.getenv("TIMEZONE", "UTC")
    GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:6767")

    # AI Config
    ACTIVE_AI_PROVIDER = os.getenv("ACTIVE_AI_PROVIDER", "gemini").lower()

    # Provider keys & models
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    @classmethod
    def validate(cls) -> None:
        """Validate required config on startup. Raises ValueError on misconfiguration."""
        if not cls.TELEGRAM_BOT_TOKEN:
            raise ValueError("TELEGRAM_BOT_TOKEN is required in .env")
        if not cls.ALLOWED_USER_IDS:
            raise ValueError(
                "TELEGRAM_ALLOWED_USER_IDS is required and must not be empty"
            )

        if cls.ACTIVE_AI_PROVIDER == "gemini":
            if not cls.GEMINI_API_KEY:
                raise ValueError(
                    "GEMINI_API_KEY is required when active provider is gemini"
                )
        elif cls.ACTIVE_AI_PROVIDER == "openai":
            if not cls.OPENAI_API_KEY:
                raise ValueError(
                    "OPENAI_API_KEY is required when active provider is openai"
                )
        else:
            raise ValueError(
                f"Unknown ACTIVE_AI_PROVIDER: {cls.ACTIVE_AI_PROVIDER}. Use 'gemini' or 'openai'"
            )
