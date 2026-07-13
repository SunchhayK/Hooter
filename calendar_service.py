import os
import logging
import zoneinfo
import re
from datetime import datetime, timezone, timedelta
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from ai_parser import ParsedEvent
from config import Config

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def get_timezone(tz_str: str) -> timezone | zoneinfo.ZoneInfo:
    """Parse timezone string. Supports IANA name or simple GMT/UTC offsets."""
    tz_str = tz_str.strip()
    try:
        return zoneinfo.ZoneInfo(tz_str)
    except zoneinfo.ZoneInfoNotFoundError:
        # Match GMT/UTC offset formats like GMT+7, UTC-5, +07:00, -5
        match = re.match(r"^(?:UTC|GMT)?([+-])(\d{1,2})(?::?(\d{2}))?$", tz_str)
        if match:
            sign = 1 if match.group(1) == "+" else -1
            hours = int(match.group(2))
            minutes = int(match.group(3)) if match.group(3) else 0
            return timezone(timedelta(hours=sign * hours, minutes=sign * minutes))

        logger.warning(f"Timezone '{tz_str}' not recognized. Falling back to UTC.")
        return timezone.utc


class CalendarService:
    def __init__(self, user_id: int = None, token_path: str = None):
        if token_path is not None:
            self.token_path = token_path
        elif user_id is not None:
            user_token = f"token_{user_id}.json"
            self.token_path = user_token if os.path.exists(user_token) else "token.json"
        else:
            self.token_path = "token.json"

        self.creds = self._load_credentials()
        self.service = build("calendar", "v3", credentials=self.creds)

    def _load_credentials(self) -> Credentials:
        creds = None
        if os.path.exists(self.token_path):
            creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.info("Google OAuth token expired. Refreshing...")
                creds.refresh(Request())
                with open(self.token_path, "w") as token:
                    token.write(creds.to_json())
            else:
                raise FileNotFoundError(
                    f"Valid credentials not found at {self.token_path}. "
                    "Please run /auth in Telegram or setup_oauth.py to authorize."
                )
        return creds

    def create_event(self, event: ParsedEvent) -> str:
        """Create a calendar event and return the HTML link to it."""
        calendar_id = Config.GOOGLE_CALENDAR_ID
        timezone_str = Config.TIMEZONE

        # Build Google Calendar event resource
        body = {
            "summary": event.summary,
            "description": event.description or "",
            "location": event.location or "",
        }

        if event.is_all_day:
            body["start"] = {"date": event.start_date}
            # If end_date is missing, default to same day
            end_date = event.end_date or event.start_date
            body["end"] = {"date": end_date}
        else:
            # Parse timezone to localize naive datetimes from AI
            tz = get_timezone(timezone_str)
            try:
                start_dt = datetime.fromisoformat(event.start_datetime).replace(
                    tzinfo=tz
                )
                end_dt = datetime.fromisoformat(event.end_datetime).replace(tzinfo=tz)
            except ValueError as e:
                logger.error(
                    f"Failed to parse datetime from AI: start={event.start_datetime}, end={event.end_datetime}"
                )
                raise ValueError(f"Invalid datetime format received from AI: {e}")

            # Google Calendar accepts ISO strings with timezone offsets
            body["start"] = {"dateTime": start_dt.isoformat()}
            body["end"] = {"dateTime": end_dt.isoformat()}

        logger.info(f"Creating event: {body}")
        created_event = (
            self.service.events().insert(calendarId=calendar_id, body=body).execute()
        )

        return created_event.get("htmlLink", "")

    def list_events(
        self,
        time_min: datetime = None,
        time_max: datetime = None,
        search_query: str = None,
        max_results: int = 10,
    ) -> list:
        """List events from Google Calendar based on parameters."""
        calendar_id = Config.GOOGLE_CALENDAR_ID

        params = {
            "calendarId": calendar_id,
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": max_results,
        }

        if time_min:
            params["timeMin"] = time_min.isoformat()
        if time_max:
            params["timeMax"] = time_max.isoformat()
        if search_query:
            params["q"] = search_query

        logger.info(
            f"Listing events: timeMin={time_min}, timeMax={time_max}, query={search_query}"
        )
        events_result = self.service.events().list(**params).execute()
        return events_result.get("items", [])
