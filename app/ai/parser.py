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
        self,
        text: str,
        reference_time_str: str,
        timezone: str,
        calendar_context: str = "",
    ) -> AIResponse:
        pass

    def _get_system_instruction(
        self, reference_time_str: str, timezone: str, calendar_context: str = ""
    ) -> str:
        return (
            "You are a calendar and task management assistant. Parse the user's message and respond with structured JSON.\n"
            f"Current local time: {reference_time_str}\n"
            f"User timezone: {timezone}\n\n"
            "## Intent classification (pick exactly one)\n"
            "- 'create': user wants to add events or tasks\n"
            "- 'query': user wants to view or search their schedule\n"
            "- 'complete_task': user wants to mark a task as done\n\n"
            "## Rules for intent='create'\n\n"
            "### Deciding: event vs task\n"
            "Put in EVENTS (has a fixed time block on the calendar):\n"
            "  - Meetings, appointments, sessions with a specific date/time\n"
            "  - If the event is uncertain or proposed, append ' (Proposed)' to the summary\n"
            "Put in TASKS (action items, things to do, deadlines):\n"
            "  - 'Next steps', action items, things to confirm, things to check\n"
            "  - Deadlines phrased as 'by X', 'within X', 'before X', 'no later than X'\n"
            "  - Reminders without a strict time block\n"
            "  - Do NOT create a task for every person mentioned; create one task per distinct action\n\n"
            "### Date/time rules\n"
            "  - Compute relative dates (today, tomorrow, next week) from Current local time\n"
            "  - Absolute dates (e.g. '24th July') use the current year unless context says otherwise\n"
            "  - A date range (e.g. '28th-31st July') → set start_date to the first date, end_date to the last date\n"
            "  - A deadline date → set as task due_date, NOT as an event\n\n"
            "### Meeting minutes pattern\n"
            "If the message resembles meeting notes:\n"
            "  - Scheduled meetings/sessions mentioned → EVENTS\n"
            "  - 'Next Step' / action items section → TASKS (one task per distinct action, not per person)\n"
            "  - Dates mentioned as 'proposed' or pending confirmation → EVENTS with '(Proposed)' suffix\n"
            "  - Deadlines ('provide date by X', 'within X') → TASKS with due_date set\n\n"
            "### Forwarded message rule\n"
            "If the message starts with '[Forwarded from: <Name>]', include 'Forwarded from: <Name>' "
            "as the first line of the description for every event and task created.\n\n"
            "## Rules for intent='query'\n"
            "  - Set query_time_min and/or query_time_max for the requested range\n"
            "  - Set query_search if looking for a specific topic\n\n"
            "## Rules for intent='complete_task'\n"
            "  - Extract the task(s) to complete into the 'tasks' list\n"
            "  - Use 'title' that matches the existing task name as closely as possible"
            + (
                f"\n\n{calendar_context}"
                "\n\nIMPORTANT: Use the calendar context above to resolve vague references, "
                "missing dates, or ambiguous event names. If the user's message refers to "
                "an event or task from the calendar context, use its exact title and date."
                if calendar_context
                else ""
            )
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
        self,
        text: str,
        reference_time_str: str,
        timezone: str,
        calendar_context: str = "",
    ) -> AIResponse:
        from google.genai import types

        system_instruction = self._get_system_instruction(
            reference_time_str, timezone, calendar_context
        )
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=text,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AIResponse,
                system_instruction=system_instruction,
                # temperature=0 + top_k=1 + top_p=1 = greedy decoding (fully deterministic)
                temperature=0.0,
                top_k=1,
                top_p=1.0,
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
        self,
        text: str,
        reference_time_str: str,
        timezone: str,
        calendar_context: str = "",
    ) -> AIResponse:
        system_instruction = self._get_system_instruction(
            reference_time_str, timezone, calendar_context
        )
        response = self.client.beta.chat.completions.parse(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": text},
            ],
            response_format=AIResponse,
            temperature=0.0,
            # seed pins the RNG so identical inputs reproduce identical outputs
            seed=0,
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
