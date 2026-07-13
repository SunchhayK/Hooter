import json
import logging
from abc import ABC, abstractmethod
from typing import Optional, List
from pydantic import BaseModel, Field
from config import Config

logger = logging.getLogger(__name__)


class ParsedEvent(BaseModel):
    summary: str = Field(description="Brief title of the event or task")
    is_all_day: bool = Field(
        description="True if all-day event (no specific time), False if it has a specific time"
    )
    start_datetime: Optional[str] = Field(
        None,
        description="ISO 8601 datetime (YYYY-MM-DDTHH:MM:SS) in user local timezone. Must be set if is_all_day is False.",
    )
    end_datetime: Optional[str] = Field(
        None,
        description="ISO 8601 datetime (YYYY-MM-DDTHH:MM:SS) in user local timezone. If not specified, default to 1 hour after start_datetime. Must be set if is_all_day is False.",
    )
    start_date: Optional[str] = Field(
        None, description="Date string (YYYY-MM-DD). Must be set if is_all_day is True."
    )
    end_date: Optional[str] = Field(
        None,
        description="Date string (YYYY-MM-DD). Optional if is_all_day is True. Note: Google Calendar all-day end date is exclusive (e.g. for a single day event on 2026-07-15, end_date must be 2026-07-16).",
    )
    location: Optional[str] = Field(None, description="Location of the event")
    description: Optional[str] = Field(
        None,
        description="Extra details, e.g. people involved (who), notes, description of tasks (how).",
    )


class ParsedEvents(BaseModel):
    events: List[ParsedEvent] = Field(
        description="List of events extracted from the user's message. Can be empty if no events found."
    )


class AIParser(ABC):
    @abstractmethod
    def parse_message(
        self, text: str, reference_time_str: str, timezone: str
    ) -> List[ParsedEvent]:
        pass

    def _get_system_instruction(self, reference_time_str: str, timezone: str) -> str:
        return (
            f"You are a scheduler assistant. Extract ALL calendar event details from the user's message.\n"
            f"Current local time: {reference_time_str}\n"
            f"User Timezone: {timezone}\n\n"
            f"Rules:\n"
            f"1. Compute relative dates/times (e.g., 'tomorrow', 'next Wed at 3pm') using the Current local time.\n"
            f"2. If no year is specified, assume it refers to the closest future occurrence of that date.\n"
            f"3. Extract MULTIPLE events if the message contains more than one schedule or class update.\n"
            f"4. Return the exact JSON structure matching the ParsedEvents schema containing the list of events."
        )


class GeminiParser(AIParser):
    def __init__(self, api_key: str, model_name: str):
        from google import genai

        self.client = genai.Client(api_key=api_key)
        self.model_name = model_name

    def parse_message(
        self, text: str, reference_time_str: str, timezone: str
    ) -> List[ParsedEvent]:
        from google.genai import types

        system_instruction = self._get_system_instruction(reference_time_str, timezone)

        response = self.client.models.generate_content(
            model=self.model_name,
            contents=text,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ParsedEvents,
                system_instruction=system_instruction,
                temperature=0.0,
            ),
        )

        data = json.loads(response.text)
        parsed = ParsedEvents(**data)
        return parsed.events


class OpenAIParser(AIParser):
    def __init__(self, api_key: str, model_name: str):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key)
        self.model_name = model_name

    def parse_message(
        self, text: str, reference_time_str: str, timezone: str
    ) -> List[ParsedEvent]:
        system_instruction = self._get_system_instruction(reference_time_str, timezone)

        response = self.client.beta.chat.completions.parse(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": text},
            ],
            response_format=ParsedEvents,
            temperature=0.0,
        )

        return response.choices[0].message.parsed.events


class AIParserFactory:
    @staticmethod
    def get_parser() -> AIParser:
        provider = Config.ACTIVE_AI_PROVIDER
        if provider == "gemini":
            return GeminiParser(Config.GEMINI_API_KEY, Config.GEMINI_MODEL)
        elif provider == "openai":
            return OpenAIParser(Config.OPENAI_API_KEY, Config.OPENAI_MODEL)
        else:
            raise ValueError(f"Unsupported AI provider: {provider}")


if __name__ == "__main__":
    print("AI parser schema defined successfully.")
