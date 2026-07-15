"""AI parsing layer: schema models, provider implementations, factory."""

import json
import logging
from abc import ABC, abstractmethod
from typing import Optional, List

from pydantic import BaseModel, Field

from app.config import Config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema models
# ---------------------------------------------------------------------------


class ParsedEvent(BaseModel):
    summary: str = Field(description="Brief title of the event or task")
    is_all_day: bool = Field(
        description="True if all-day event (no specific time), False if it has a specific time"
    )
    start_datetime: Optional[str] = Field(
        None,
        description=(
            "ISO 8601 datetime (YYYY-MM-DDTHH:MM:SS) in user local timezone. "
            "Must be set if is_all_day is False."
        ),
    )
    end_datetime: Optional[str] = Field(
        None,
        description=(
            "ISO 8601 datetime (YYYY-MM-DDTHH:MM:SS) in user local timezone. "
            "Defaults to 1 hour after start_datetime. Must be set if is_all_day is False."
        ),
    )
    start_date: Optional[str] = Field(
        None, description="Date string (YYYY-MM-DD). Must be set if is_all_day is True."
    )
    end_date: Optional[str] = Field(
        None,
        description=(
            "Date string (YYYY-MM-DD). Optional if is_all_day is True. "
            "Note: Google Calendar all-day end date is exclusive."
        ),
    )
    location: Optional[str] = Field(None, description="Location of the event")
    description: Optional[str] = Field(
        None,
        description="Extra details, e.g. people involved, notes, task description.",
    )


class ParsedTask(BaseModel):
    title: str = Field(description="Title of the task")
    notes: Optional[str] = Field(None, description="Description or notes for the task")
    due_date: Optional[str] = Field(
        None, description="ISO 8601 datetime for when the task is due, if specified."
    )


class AIResponse(BaseModel):
    intent: str = Field(
        description="Must be 'create' (schedule new events/tasks), 'query' (search events), or 'complete_task' (mark task done)."
    )
    events: Optional[List[ParsedEvent]] = Field(
        default=None,
        description="Events to create. Use when intent is 'create' and user asks for calendar events.",
    )
    tasks: Optional[List[ParsedTask]] = Field(
        default=None,
        description="Tasks to create or complete. Use for to-dos, reminders, or tasks without a strict time block.",
    )
    query_time_min: Optional[str] = Field(
        default=None,
        description="ISO 8601 datetime in user local timezone. Use when intent is 'query'.",
    )
    query_time_max: Optional[str] = Field(
        default=None,
        description="ISO 8601 datetime in user local timezone. Use when intent is 'query'.",
    )
    query_search: Optional[str] = Field(
        default=None,
        description="Keyword search query. Use when intent is 'query' and user asks for a specific topic.",
    )


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class AIParser(ABC):
    @abstractmethod
    def parse_message(
        self, text: str, reference_time_str: str, timezone: str
    ) -> AIResponse:
        pass

    def _get_system_instruction(self, reference_time_str: str, timezone: str) -> str:
        return (
            "You are a calendar and task bot assistant. Determine the user's intent: "
            "'create' (to add new events or tasks), 'query' (to search/list schedule), or 'complete_task'.\n"
            f"Current local time: {reference_time_str}\n"
            f"User Timezone: {timezone}\n\n"
            "Rules for intent='create':\n"
            "1. Extract calendar event details into the 'events' list for blocked time.\n"
            "2. Extract to-dos and reminders into the 'tasks' list.\n"
            "3. Compute relative dates/times using the Current local time.\n\n"
            "Rules for intent='query':\n"
            "1. Set 'query_time_min' and/or 'query_time_max' to represent the requested range.\n"
            "2. Set 'query_search' if looking for a specific topic.\n\n"
            "Rules for intent='complete_task':\n"
            "1. Extract the task(s) to complete into the 'tasks' list with the 'title' matching the task to be marked done."
        )


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------


class GeminiParser(AIParser):
    def __init__(self, api_key: str, model_name: str) -> None:
        from google import genai

        self.client = genai.Client(api_key=api_key)
        self.model_name = model_name

    def parse_message(
        self, text: str, reference_time_str: str, timezone: str
    ) -> AIResponse:
        from google.genai import types

        system_instruction = self._get_system_instruction(reference_time_str, timezone)
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=text,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AIResponse,
                system_instruction=system_instruction,
                temperature=0.0,
            ),
        )
        data = json.loads(response.text)
        return AIResponse(**data)


class OpenAIParser(AIParser):
    def __init__(self, api_key: str, model_name: str) -> None:
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key)
        self.model_name = model_name

    def parse_message(
        self, text: str, reference_time_str: str, timezone: str
    ) -> AIResponse:
        system_instruction = self._get_system_instruction(reference_time_str, timezone)
        response = self.client.beta.chat.completions.parse(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": text},
            ],
            response_format=AIResponse,
            temperature=0.0,
        )
        return response.choices[0].message.parsed


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class AIParserFactory:
    @staticmethod
    def get_parser() -> AIParser:
        provider = Config.ACTIVE_AI_PROVIDER
        if provider == "gemini":
            return GeminiParser(Config.GEMINI_API_KEY, Config.GEMINI_MODEL)
        elif provider == "openai":
            return OpenAIParser(Config.OPENAI_API_KEY, Config.OPENAI_MODEL)
        raise ValueError(f"Unsupported AI provider: {provider}")
