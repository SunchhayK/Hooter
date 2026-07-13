"""Entry point for the Telegram Calendar Bot."""

# Allow insecure transport for OAuth localhost redirect URI only.
# If GOOGLE_REDIRECT_URI is changed to HTTPS, remove this.
import os
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

import logging

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

from app.bot.application import run

if __name__ == "__main__":
    run()
