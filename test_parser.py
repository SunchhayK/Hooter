import json
from unittest.mock import MagicMock
from ai_parser import GeminiParser, OpenAIParser, ParsedEvent, ParsedEvents


def test_pydantic_schema():
    """Verify ParsedEvent validates inputs correctly."""
    data = {
        "summary": "Doctor appointment",
        "is_all_day": False,
        "start_datetime": "2026-07-15T10:00:00",
        "end_datetime": "2026-07-15T11:00:00",
        "location": "Medical Center",
        "description": "With Dr. Smith",
    }
    event = ParsedEvent(**data)
    assert event.summary == "Doctor appointment"
    assert event.is_all_day is False
    assert event.start_datetime == "2026-07-15T10:00:00"
    print("✓ Pydantic schema validation works.")


def test_openai_parser_mock():
    """Mock OpenAI SDK output and verify parsing logic."""
    parser = OpenAIParser(api_key="mock-key", model_name="gpt-4o-mini")
    parser.client = MagicMock()

    # Mock completion object matching beta.chat.completions.parse structure
    mock_choice = MagicMock()
    mock_choice.message.parsed = ParsedEvents(
        events=[
            ParsedEvent(
                summary="Lunch with Sarah",
                is_all_day=False,
                start_datetime="2026-07-14T12:00:00",
                end_datetime="2026-07-14T13:00:00",
                description="Discuss thesis",
            )
        ]
    )
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    parser.client.beta.chat.completions.parse.return_value = mock_response

    result = parser.parse_message(
        text="Lunch with Sarah tomorrow at noon to discuss thesis",
        reference_time_str="2026-07-13 09:00:00 (Monday)",
        timezone="UTC",
    )

    assert len(result) == 1
    assert result[0].summary == "Lunch with Sarah"
    assert result[0].is_all_day is False
    assert result[0].start_datetime == "2026-07-14T12:00:00"
    assert result[0].description == "Discuss thesis"
    print("✓ OpenAI mock parsing logic works.")


def test_gemini_parser_mock():
    """Mock Gemini SDK output and verify parsing logic."""
    parser = GeminiParser(api_key="mock-key", model_name="gemini-2.5-flash")
    parser.client = MagicMock()

    mock_response = MagicMock()
    mock_response.text = json.dumps(
        {
            "events": [
                {
                    "summary": "Buy groceries",
                    "is_all_day": True,
                    "start_date": "2026-07-15",
                    "end_date": "2026-07-16",
                }
            ]
        }
    )

    parser.client.models.generate_content.return_value = mock_response

    result = parser.parse_message(
        text="Buy groceries this Wednesday",
        reference_time_str="2026-07-13 09:00:00 (Monday)",
        timezone="UTC",
    )

    assert len(result) == 1
    assert result[0].summary == "Buy groceries"
    assert result[0].is_all_day is True
    assert result[0].start_date == "2026-07-15"
    assert result[0].end_date == "2026-07-16"
    print("✓ Gemini mock parsing logic works.")


if __name__ == "__main__":
    print("Running self-checks...")
    test_pydantic_schema()
    test_openai_parser_mock()
    test_gemini_parser_mock()
    print("All checks passed successfully.")
