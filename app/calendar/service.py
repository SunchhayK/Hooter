"""Google Calendar service: credential management and API operations."""

import logging
import os
import re
import zoneinfo
from datetime import datetime, timedelta, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.ai.parser import ParsedEvent
from app.config import Config

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
]


def get_timezone(tz_str: str) -> timezone | zoneinfo.ZoneInfo:
    """Parse timezone string. Supports IANA name or simple GMT/UTC offsets."""
    tz_str = tz_str.strip()
    try:
        return zoneinfo.ZoneInfo(tz_str)
    except zoneinfo.ZoneInfoNotFoundError:
        match = re.match(r"^(?:UTC|GMT)?([+-])(\d{1,2})(?::?(\d{2}))?$", tz_str)
        if match:
            sign = 1 if match.group(1) == "+" else -1
            hours = int(match.group(2))
            minutes = int(match.group(3)) if match.group(3) else 0
            return timezone(timedelta(hours=sign * hours, minutes=sign * minutes))

        logger.warning(f"Timezone '{tz_str}' not recognized. Falling back to UTC.")
        return timezone.utc


def _write_token(path: str, json_str: str) -> None:
    """Write a token file with 0o600 permissions (owner read/write only)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json_str)


class CalendarService:
    """Wraps the Google Calendar API for a specific user's credentials."""

    def __init__(self, user_id: int = None, token_path: str = None) -> None:
        if token_path is not None:
            self.token_path = token_path
        elif user_id is not None:
            user_token = f"tokens/token_{user_id}.json"
            self.token_path = (
                user_token if os.path.exists(user_token) else "tokens/token.json"
            )
        else:
            self.token_path = "tokens/token.json"

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
                _write_token(self.token_path, creds.to_json())
            else:
                raise FileNotFoundError(
                    f"Valid credentials not found at {self.token_path}. "
                    "Please run /auth in Telegram or scripts/setup_oauth.py to authorize."
                )
        return creds

    def create_event(self, event: ParsedEvent) -> str:
        """Create a calendar event. Returns the HTML link."""
        tz = get_timezone(Config.TIMEZONE)
        body = {
            "summary": event.summary,
            "description": event.description or "",
            "location": event.location or "",
        }

        if event.is_all_day:
            body["start"] = {"date": event.start_date}
            body["end"] = {"date": event.end_date or event.start_date}
        else:
            try:
                start_dt = datetime.fromisoformat(event.start_datetime).replace(tzinfo=tz)
                end_dt = datetime.fromisoformat(event.end_datetime).replace(tzinfo=tz)
            except ValueError as e:
                logger.error(
                    f"Failed to parse datetime from AI: start={event.start_datetime}, end={event.end_datetime}"
                )
                raise ValueError(f"Invalid datetime format received from AI: {e}")

            body["start"] = {"dateTime": start_dt.isoformat()}
            body["end"] = {"dateTime": end_dt.isoformat()}

        logger.info(f"Creating event: {body}")
        created = (
            self.service.events()
            .insert(calendarId=Config.GOOGLE_CALENDAR_ID, body=body)
            .execute()
        )
        return created.get("htmlLink", "")

    def find_reschedule_candidate(self, event: ParsedEvent) -> tuple[dict | None, bool]:
        """Find existing event with the same summary; return (candidate, is_duplicate)."""
        tz = get_timezone(Config.TIMEZONE)

        if event.is_all_day:
            start_dt = datetime.combine(
                datetime.strptime(event.start_date, "%Y-%m-%d").date(),
                datetime.min.time(),
            ).replace(tzinfo=tz)
        else:
            try:
                start_dt = datetime.fromisoformat(event.start_datetime).replace(tzinfo=tz)
            except ValueError as e:
                logger.error(f"Failed to parse datetime: start={event.start_datetime}")
                raise ValueError(f"Invalid datetime format: {e}")

        now = datetime.now(tz)
        candidates = self.list_events(
            time_min=now - timedelta(days=30),
            time_max=now + timedelta(days=90),
            search_query=event.summary,
        )

        best_candidate = None
        min_diff = None
        new_summary = event.summary.strip().lower()

        for c in candidates:
            if c.get("summary", "").strip().lower() != new_summary:
                continue

            c_start = c.get("start", {})
            if "date" in c_start:
                c_s_dt = datetime.combine(
                    datetime.strptime(c_start["date"], "%Y-%m-%d").date(),
                    datetime.min.time(),
                ).replace(tzinfo=tz)
            else:
                c_s_dt = datetime.fromisoformat(
                    c_start["dateTime"].replace("Z", "+00:00")
                ).astimezone(tz)

            diff = abs((c_s_dt - start_dt).total_seconds())
            if min_diff is None or diff < min_diff:
                min_diff = diff
                best_candidate = c

        if best_candidate:
            c_start = best_candidate.get("start", {})
            is_dup = False
            if event.is_all_day and "date" in c_start:
                is_dup = c_start["date"] == event.start_date
            elif not event.is_all_day and "dateTime" in c_start:
                c_s_dt = datetime.fromisoformat(
                    c_start["dateTime"].replace("Z", "+00:00")
                ).astimezone(tz)
                is_dup = c_s_dt == start_dt
            return best_candidate, is_dup

        return None, False

    def reschedule_event(self, event_id: str, event: ParsedEvent) -> str:
        """Update existing event times (and optional fields). Returns HTML link."""
        tz = get_timezone(Config.TIMEZONE)
        body: dict = {"summary": event.summary}
        if event.description is not None:
            body["description"] = event.description
        if event.location is not None:
            body["location"] = event.location

        if event.is_all_day:
            body["start"] = {"date": event.start_date}
            body["end"] = {"date": event.end_date or event.start_date}
        else:
            try:
                start_dt = datetime.fromisoformat(event.start_datetime).replace(tzinfo=tz)
                end_dt = datetime.fromisoformat(event.end_datetime).replace(tzinfo=tz)
            except ValueError as e:
                logger.error(
                    f"Failed to parse datetime for reschedule: start={event.start_datetime}, end={event.end_datetime}"
                )
                raise ValueError(f"Invalid datetime format: {e}")

            body["start"] = {"dateTime": start_dt.isoformat()}
            body["end"] = {"dateTime": end_dt.isoformat()}

        updated = (
            self.service.events()
            .patch(calendarId=Config.GOOGLE_CALENDAR_ID, eventId=event_id, body=body)
            .execute()
        )
        return updated.get("htmlLink", "")

    def check_collisions(self, event: ParsedEvent, exclude_event_id: str = None) -> list:
        """Return events that overlap with the given event's time range."""
        tz = get_timezone(Config.TIMEZONE)

        if event.is_all_day:
            start_dt = datetime.combine(
                datetime.strptime(event.start_date, "%Y-%m-%d").date(),
                datetime.min.time(),
            ).replace(tzinfo=tz)
            end_date_str = event.end_date or event.start_date
            if end_date_str == event.start_date and not event.end_date:
                end_date = (
                    datetime.strptime(event.start_date, "%Y-%m-%d").date()
                    + timedelta(days=1)
                )
            else:
                end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
            end_dt = datetime.combine(end_date, datetime.min.time()).replace(tzinfo=tz)
        else:
            try:
                start_dt = datetime.fromisoformat(event.start_datetime).replace(tzinfo=tz)
                end_dt = datetime.fromisoformat(event.end_datetime).replace(tzinfo=tz)
            except ValueError as e:
                logger.error(f"Failed to parse datetime: start={event.start_datetime}")
                raise ValueError(f"Invalid datetime format: {e}")

        collisions = []
        for ext in self.list_events(time_min=start_dt, time_max=end_dt):
            if exclude_event_id and ext.get("id") == exclude_event_id:
                continue

            ext_start = ext.get("start", {})
            ext_end = ext.get("end", {})

            if "date" in ext_start:
                ext_s_dt = datetime.combine(
                    datetime.strptime(ext_start["date"], "%Y-%m-%d").date(),
                    datetime.min.time(),
                ).replace(tzinfo=tz)
                ext_e_dt = datetime.combine(
                    datetime.strptime(
                        ext_end.get("date", ext_start["date"]), "%Y-%m-%d"
                    ).date(),
                    datetime.min.time(),
                ).replace(tzinfo=tz)
            else:
                ext_s_dt = datetime.fromisoformat(
                    ext_start["dateTime"].replace("Z", "+00:00")
                )
                ext_e_dt = datetime.fromisoformat(
                    ext_end["dateTime"].replace("Z", "+00:00")
                )

            if start_dt < ext_e_dt and ext_s_dt < end_dt:
                collisions.append(ext)

        return collisions

    def list_events(
        self,
        time_min: datetime = None,
        time_max: datetime = None,
        search_query: str = None,
        max_results: int = 10,
    ) -> list:
        """List events from Google Calendar."""
        params = {
            "calendarId": Config.GOOGLE_CALENDAR_ID,
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
        result = self.service.events().list(**params).execute()
        return result.get("items", [])

    def delete_event(self, event_id: str) -> None:
        """Delete an event by ID."""
        logger.info(f"Deleting event: {event_id}")
        self.service.events().delete(
            calendarId=Config.GOOGLE_CALENDAR_ID, eventId=event_id
        ).execute()

    def get_event(self, event_id: str) -> dict:
        """Get event details by ID."""
        return (
            self.service.events()
            .get(calendarId=Config.GOOGLE_CALENDAR_ID, eventId=event_id)
            .execute()
        )

    def patch_event(self, event_id: str, body: dict) -> dict:
        """Patch specific fields of an event."""
        return (
            self.service.events()
            .patch(
                calendarId=Config.GOOGLE_CALENDAR_ID, eventId=event_id, body=body
            )
            .execute()
        )

    def get_connection_status(self) -> dict:
        """Return details about the currently authorized user/calendar."""
        res: dict = {}

        try:
            about = self.service.about().get(fields="user").execute()
            user_info = about.get("user", {})
            res["user_email"] = user_info.get("emailAddress", "Unknown")
            res["user_name"] = user_info.get("displayName", "Unknown")
        except HttpError as e:
            if e.resp.status == 403:
                res["user_email"] = "Run /auth again (New Scopes Needed)"
                res["user_name"] = "Run /auth again (New Scopes Needed)"
            else:
                res["user_email"] = f"Error: {e.resp.status}"
                res["user_name"] = "Unknown"
        except Exception:
            res["user_email"] = "Unknown"
            res["user_name"] = "Unknown"

        try:
            cal = (
                self.service.calendars()
                .get(calendarId=Config.GOOGLE_CALENDAR_ID)
                .execute()
            )
            res["calendar_summary"] = cal.get("summary", "Unknown")
            res["calendar_id"] = cal.get("id", Config.GOOGLE_CALENDAR_ID)
            res["calendar_timezone"] = cal.get("timeZone", "Unknown")
        except HttpError as e:
            if e.resp.status == 403:
                res["calendar_summary"] = "Run /auth again (New Scopes Needed)"
            else:
                res["calendar_summary"] = f"Error: {e.resp.status}"
            res["calendar_id"] = Config.GOOGLE_CALENDAR_ID
            res["calendar_timezone"] = "Unknown"
        except Exception:
            res["calendar_summary"] = "Error fetching details"
            res["calendar_id"] = Config.GOOGLE_CALENDAR_ID
            res["calendar_timezone"] = "Unknown"

        return res
