"""Unit tests for AI parser and CalendarService logic."""

import json
from unittest.mock import MagicMock

from app.ai.parser import GeminiParser, OpenAIParser, ParsedEvent, AIResponse


def test_pydantic_schema():
    """Verify ParsedEvent validates inputs correctly."""
    event = ParsedEvent(
        summary="Doctor appointment",
        is_all_day=False,
        start_datetime="2026-07-15T10:00:00",
        end_datetime="2026-07-15T11:00:00",
        location="Medical Center",
        description="With Dr. Smith",
    )
    assert event.summary == "Doctor appointment"
    assert event.is_all_day is False
    assert event.start_datetime == "2026-07-15T10:00:00"
    print("✓ Pydantic schema validation works.")


def test_openai_parser_mock():
    """Mock OpenAI SDK output and verify parsing logic."""
    parser = OpenAIParser(api_key="mock-key", model_name="gpt-4o-mini")
    parser.client = MagicMock()

    mock_choice = MagicMock()
    mock_choice.message.parsed = AIResponse(
        intent="create",
        events=[
            ParsedEvent(
                summary="Lunch with Sarah",
                is_all_day=False,
                start_datetime="2026-07-14T12:00:00",
                end_datetime="2026-07-14T13:00:00",
                description="Discuss thesis",
            )
        ],
    )
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    parser.client.beta.chat.completions.parse.return_value = mock_response

    result = parser.parse_message(
        text="Lunch with Sarah tomorrow at noon to discuss thesis",
        reference_time_str="2026-07-13 09:00:00 (Monday)",
        timezone="UTC",
    )

    assert result.intent == "create"
    assert len(result.events) == 1
    assert result.events[0].summary == "Lunch with Sarah"
    assert result.events[0].description == "Discuss thesis"
    print("✓ OpenAI mock parsing logic works.")


def test_gemini_parser_mock():
    """Mock Gemini SDK output and verify parsing logic."""
    parser = GeminiParser(api_key="mock-key", model_name="gemini-2.5-flash")
    parser.client = MagicMock()

    mock_response = MagicMock()
    mock_response.text = json.dumps({
        "intent": "create",
        "events": [
            {
                "summary": "Buy groceries",
                "is_all_day": True,
                "start_date": "2026-07-15",
                "end_date": "2026-07-16",
            }
        ],
    })
    parser.client.models.generate_content.return_value = mock_response

    result = parser.parse_message(
        text="Buy groceries this Wednesday",
        reference_time_str="2026-07-13 09:00:00 (Monday)",
        timezone="UTC",
    )

    assert result.intent == "create"
    assert result.events[0].summary == "Buy groceries"
    assert result.events[0].is_all_day is True
    print("✓ Gemini mock parsing logic works.")


def test_query_intent_mock():
    """Verify parser returns query parameters for query-based user requests."""
    parser = OpenAIParser(api_key="mock-key", model_name="gpt-4o-mini")
    parser.client = MagicMock()

    mock_choice = MagicMock()
    mock_choice.message.parsed = AIResponse(
        intent="query",
        query_time_min="2026-07-13T00:00:00",
        query_time_max="2026-07-13T23:59:59",
    )
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    parser.client.beta.chat.completions.parse.return_value = mock_response

    result = parser.parse_message(
        text="what do I have to do today?",
        reference_time_str="2026-07-13 09:00:00 (Monday)",
        timezone="UTC",
    )

    assert result.intent == "query"
    assert result.query_time_min == "2026-07-13T00:00:00"
    print("✓ Query intent parsing logic works.")


def test_reschedule_duplicate_and_collision():
    from unittest import mock
    from app.calendar.service import CalendarService

    with (
        mock.patch("app.calendar.service.CalendarService._load_credentials"),
        mock.patch("app.calendar.service.build"),
    ):
        service = CalendarService(user_id=999)
        service.list_events = MagicMock()

        with mock.patch("app.calendar.service.Config") as mock_config:
            mock_config.TIMEZONE = "UTC"
            mock_config.GOOGLE_CALENDAR_ID = "primary"

            event = ParsedEvent(
                summary="Meeting with Bob",
                is_all_day=False,
                start_datetime="2026-07-15T10:00:00",
                end_datetime="2026-07-15T11:00:00",
            )
            service.list_events.return_value = [
                {
                    "id": "123",
                    "summary": "Meeting with Bob",
                    "start": {"dateTime": "2026-07-15T10:00:00Z"},
                    "end": {"dateTime": "2026-07-15T11:00:00Z"},
                }
            ]
            candidate, is_dup = service.find_reschedule_candidate(event)
            assert is_dup is True
            assert candidate["id"] == "123"

            event_new = ParsedEvent(
                summary="Meeting with Bob",
                is_all_day=False,
                start_datetime="2026-07-15T14:00:00",
                end_datetime="2026-07-15T15:00:00",
            )
            candidate, is_dup = service.find_reschedule_candidate(event_new)
            assert is_dup is False
            assert candidate["id"] == "123"

            service.list_events.return_value = [
                {
                    "id": "123",
                    "summary": "Meeting with Bob",
                    "start": {"dateTime": "2026-07-15T14:00:00Z"},
                    "end": {"dateTime": "2026-07-15T15:00:00Z"},
                },
                {
                    "id": "456",
                    "summary": "Lunch",
                    "start": {"dateTime": "2026-07-15T14:30:00Z"},
                    "end": {"dateTime": "2026-07-15T15:30:00Z"},
                },
            ]
            collisions = service.check_collisions(event_new, exclude_event_id="123")
            assert len(collisions) == 1
            assert collisions[0]["id"] == "456"
            print("✓ Reschedule, duplicate, and collision detection logic works.")


if __name__ == "__main__":
    print("Running self-checks...")
    test_pydantic_schema()
    test_openai_parser_mock()
    test_gemini_parser_mock()
    test_query_intent_mock()
    test_reschedule_duplicate_and_collision()
    print("All checks passed successfully.")
