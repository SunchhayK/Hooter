"""OAuth callback HTTP server: receives Google's redirect and finalizes the token exchange."""

import asyncio
import html
import logging
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from app.bot.oauth.manager import OAuthManager
from app.calendar.service import _write_token
from app.config import Config

logger = logging.getLogger(__name__)

# Injected by Application.post_init so the HTTP thread can send Telegram messages.
_send_telegram_message: Callable | None = None
_event_loop = None


def set_telegram_notifier(send_fn: Callable, loop) -> None:
    """Called once after the Telegram Application starts."""
    global _send_telegram_message, _event_loop
    _send_telegram_message = send_fn
    _event_loop = loop


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args) -> None:  # noqa: A002
        logger.info(f"HTTP Server - {format % args}")

    # Static files served from the working directory.
    # Explicit whitelist prevents path traversal.
    _STATIC: dict[str, str] = {
        "/": "index.html",
        "/index.html": "index.html",
        "/privacy.html": "privacy.html",
        "/terms.html": "terms.html",
        "/sitemap.xml": "sitemap.xml",
    }
    _MIME: dict[str, str] = {
        ".html": "text/html; charset=utf-8",
        ".xml": "application/xml; charset=utf-8",
    }

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/oauth/callback":
            self._handle_oauth(parsed)
            return

        # Google site-verification file (dynamic name, fixed prefix)
        if parsed.path.startswith("/google") and parsed.path.endswith(".html"):
            filename = parsed.path.lstrip("/")
            if os.path.isfile(filename) and "/" not in filename:
                self._serve_file(filename, "text/html; charset=utf-8")
                return

        filename = self._STATIC.get(parsed.path)
        if filename and os.path.isfile(filename):
            ext = os.path.splitext(filename)[1]
            mime = self._MIME.get(ext, "text/plain")
            self._serve_file(filename, mime)
            return

        self.send_response(404)
        self.end_headers()

    def _serve_file(self, path: str, content_type: str) -> None:
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_oauth(self, parsed) -> None:

        params = urllib.parse.parse_qs(parsed.query)

        state = (params.get("state") or [None])[0]
        code = (params.get("code") or [None])[0]

        if not state or not code:
            self._error(400, "Missing state or code parameter.")
            return

        val = OAuthManager.pop(state)
        if not val:
            self._error(400, "Invalid or expired state session. Please run /auth again.")
            return

        user_id, flow, chat_id, _inserted_at = val

        try:
            # Build the full authorization response URL Google sent us
            auth_response = f"{Config.GOOGLE_REDIRECT_URI.rstrip('/')}?{parsed.query}"
            flow.fetch_token(authorization_response=auth_response)
            creds = flow.credentials

            token_path = f"tokens/token_{user_id}.json"
            _write_token(token_path, creds.to_json())

            self._success()
            self._notify(chat_id, "✅ *Success!* Your Google Calendar has been connected automatically.")

        except Exception as e:
            logger.exception("Failed to exchange code in HTTP server")
            safe_err = html.escape(str(e))
            self._error(500, f"Failed to exchange authorization code: {safe_err}")
            self._notify(chat_id, "❌ *Failed to connect Google Calendar.* Please run /auth again.")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _error(self, status: int, message: str) -> None:
        body = f"<h1>Error</h1><p>{message}</p>".encode("utf-8")
        self.send_response(status)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def _success(self) -> None:
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

    def _notify(self, chat_id: int, text: str) -> None:
        if _send_telegram_message and _event_loop:
            asyncio.run_coroutine_threadsafe(
                _send_telegram_message(chat_id=chat_id, text=text, parse_mode="Markdown"),
                _event_loop,
            )


def start_callback_server() -> None:
    """Start the OAuth callback HTTP server in a background daemon thread."""
    parsed = urllib.parse.urlparse(Config.GOOGLE_REDIRECT_URI)
    port = parsed.port or 6767

    # Bind to 0.0.0.0 so Docker port mapping (host:port -> container:port) can reach the server.
    # 127.0.0.1 would silently drop mapped traffic because Docker routes via the container's eth0.
    # Security: the cryptographically-random state token (TTL 10 min, single-use) acts as CSRF
    # protection — an attacker cannot forge a valid callback without knowing the state.
    server = ThreadingHTTPServer(("0.0.0.0", port), OAuthCallbackHandler)
    logger.info(f"Starting Google OAuth callback server on 0.0.0.0:{port}...")
    threading.Thread(target=server.serve_forever, daemon=True).start()
