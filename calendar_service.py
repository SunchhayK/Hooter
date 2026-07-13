import os
import logging
import zoneinfo
import re
from datetime import datetime, timezone, timedelta
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from ai_parser import ParsedEvent
from config import Config

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
                os.makedirs(os.path.dirname(self.token_path), exist_ok=True)
                fd = os.open(self.token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as token:
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

    def find_reschedule_candidate(self, event: ParsedEvent) -> tuple[dict | None, bool]:
        """Find an existing event with the same summary to check for duplicate or reschedule.

        Returns:
            (candidate_event_dict, is_duplicate)
        """
        timezone_str = Config.TIMEZONE
        tz = get_timezone(timezone_str)

        # Parse new event times
        if event.is_all_day:
            start_dt = datetime.combine(
                datetime.strptime(event.start_date, "%Y-%m-%d").date(),
                datetime.min.time(),
            ).replace(tzinfo=tz)
        else:
            try:
                start_dt = datetime.fromisoformat(event.start_datetime).replace(
                    tzinfo=tz
                )
            except ValueError as e:
                logger.error(f"Failed to parse datetime: start={event.start_datetime}")
                raise ValueError(f"Invalid datetime format: {e}")

        # Search window: [-30, +90] days
        now = datetime.now(tz)
        search_min = now - timedelta(days=30)
        search_max = now + timedelta(days=90)

        candidates = self.list_events(
            time_min=search_min,
            time_max=search_max,
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
                c_s_date = datetime.strptime(c_start["date"], "%Y-%m-%d").date()
                c_s_dt = datetime.combine(c_s_date, datetime.min.time()).replace(
                    tzinfo=tz
                )
            else:
                c_s_dt = datetime.fromisoformat(
                    c_start["dateTime"].replace("Z", "+00:00")
                ).astimezone(tz)

            # Check absolute difference to find closest match
            diff = abs((c_s_dt - start_dt).total_seconds())

            if min_diff is None or diff < min_diff:
                min_diff = diff
                best_candidate = c

        if best_candidate:
            # Check if it is a duplicate
            c_start = best_candidate.get("start", {})
            is_dup = False
            if event.is_all_day and "date" in c_start:
                if c_start["date"] == event.start_date:
                    is_dup = True
            elif not event.is_all_day and "dateTime" in c_start:
                c_s_dt = datetime.fromisoformat(
                    c_start["dateTime"].replace("Z", "+00:00")
                ).astimezone(tz)
                if c_s_dt == start_dt:
                    is_dup = True

            return best_candidate, is_dup

        return None, False

    def reschedule_event(self, event_id: str, event: ParsedEvent) -> str:
        """Update existing event start/end times (and optional fields) and return HTML link."""
        calendar_id = Config.GOOGLE_CALENDAR_ID
        timezone_str = Config.TIMEZONE

        body = {
            "summary": event.summary,
        }
        if event.description is not None:
            body["description"] = event.description
        if event.location is not None:
            body["location"] = event.location

        if event.is_all_day:
            body["start"] = {"date": event.start_date}
            end_date = event.end_date or event.start_date
            body["end"] = {"date": end_date}
        else:
            tz = get_timezone(timezone_str)
            try:
                start_dt = datetime.fromisoformat(event.start_datetime).replace(
                    tzinfo=tz
                )
                end_dt = datetime.fromisoformat(event.end_datetime).replace(tzinfo=tz)
            except ValueError as e:
                logger.error(
                    f"Failed to parse datetime for reschedule: start={event.start_datetime}, end={event.end_datetime}"
                )
                raise ValueError(f"Invalid datetime format: {e}")

            body["start"] = {"dateTime": start_dt.isoformat()}
            body["end"] = {"dateTime": end_dt.isoformat()}

        updated_event = (
            self.service.events()
            .patch(calendarId=calendar_id, eventId=event_id, body=body)
            .execute()
        )

        return updated_event.get("htmlLink", "")

    def check_collisions(
        self, event: ParsedEvent, exclude_event_id: str = None
    ) -> list:
        """Check for events overlapping with the new event's time range."""
        timezone_str = Config.TIMEZONE
        tz = get_timezone(timezone_str)

        if event.is_all_day:
            start_dt = datetime.combine(
                datetime.strptime(event.start_date, "%Y-%m-%d").date(),
                datetime.min.time(),
            ).replace(tzinfo=tz)
            end_date_str = event.end_date or event.start_date
            if end_date_str == event.start_date and not event.end_date:
                end_date = datetime.strptime(
                    event.start_date, "%Y-%m-%d"
                ).date() + timedelta(days=1)
            else:
                end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
            end_dt = datetime.combine(end_date, datetime.min.time()).replace(tzinfo=tz)
        else:
            try:
                start_dt = datetime.fromisoformat(event.start_datetime).replace(
                    tzinfo=tz
                )
                end_dt = datetime.fromisoformat(event.end_datetime).replace(tzinfo=tz)
            except ValueError as e:
                logger.error(f"Failed to parse datetime: start={event.start_datetime}")
                raise ValueError(f"Invalid datetime format: {e}")

        existing_events = self.list_events(time_min=start_dt, time_max=end_dt)
        collisions = []

        for ext in existing_events:
            if exclude_event_id and ext.get("id") == exclude_event_id:
                continue

            ext_start = ext.get("start", {})
            ext_end = ext.get("end", {})

            if "date" in ext_start:
                ext_s_date = datetime.strptime(ext_start["date"], "%Y-%m-%d").date()
                ext_e_date = datetime.strptime(
                    ext_end.get("date", ext_start["date"]), "%Y-%m-%d"
                ).date()
                ext_s_dt = datetime.combine(ext_s_date, datetime.min.time()).replace(
                    tzinfo=tz
                )
                ext_e_dt = datetime.combine(ext_e_date, datetime.min.time()).replace(
                    tzinfo=tz
                )
            else:
                ext_s_dt = datetime.fromisoformat(
                    ext_start["dateTime"].replace("Z", "+00:00")
                )
                ext_e_dt = datetime.fromisoformat(
                    ext_end["dateTime"].replace("Z", "+00:00")
                )

            # Verify overlap
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

    def delete_event(self, event_id: str) -> None:
        """Delete an event by ID from Google Calendar."""
        calendar_id = Config.GOOGLE_CALENDAR_ID
        logger.info(f"Deleting event: {event_id}")
        self.service.events().delete(calendarId=calendar_id, eventId=event_id).execute()

    def get_event(self, event_id: str) -> dict:
        """Get details of an event by ID."""
        calendar_id = Config.GOOGLE_CALENDAR_ID
        return (
            self.service.events()
            .get(calendarId=calendar_id, eventId=event_id)
            .execute()
        )

    def patch_event(self, event_id: str, body: dict) -> dict:
        """Patch fields of an event by ID."""
        calendar_id = Config.GOOGLE_CALENDAR_ID
        return (
            self.service.events()
            .patch(calendarId=calendar_id, eventId=event_id, body=body)
            .execute()
        )

    def get_connection_status(self) -> dict:
        """Get details of the currently authorized user/calendar."""
        calendar_id = Config.GOOGLE_CALENDAR_ID
        res = {}
        try:
            about_res = self.service.about().get(fields="user").execute()
            user_info = about_res.get("user", {})
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
            cal_res = self.service.calendars().get(calendarId=calendar_id).execute()
            res["calendar_summary"] = cal_res.get("summary", "Unknown")
            res["calendar_id"] = cal_res.get("id", calendar_id)
            res["calendar_timezone"] = cal_res.get("timeZone", "Unknown")
        except HttpError as e:
            if e.resp.status == 403:
                res["calendar_summary"] = "Run /auth again (New Scopes Needed)"
                res["calendar_id"] = calendar_id
                res["calendar_timezone"] = "Unknown"
            else:
                res["calendar_summary"] = f"Error: {e.resp.status}"
                res["calendar_id"] = calendar_id
                res["calendar_timezone"] = "Unknown"
        except Exception as e:
            res["calendar_summary"] = "Error fetching details"
            res["calendar_id"] = calendar_id
            res["calendar_timezone"] = "Unknown"
            res["error"] = str(e)

        return res
