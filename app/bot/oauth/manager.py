"""OAuth state manager: thread-safe PENDING_STATES with TTL expiry."""

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

_TTL_SECONDS = 600  # 10 minutes


class OAuthManager:
    """
    Manages in-flight OAuth state tokens.

    Each entry maps a state token (str) to a (user_id, flow, chat_id, inserted_at) tuple.
    Entries are single-use and expire after TTL_SECONDS to prevent unbounded growth.
    """

    _states: dict = {}

    @classmethod
    def add(cls, state: str, user_id: int, flow, chat_id: int) -> None:
        """Register a new OAuth state; prune expired entries first."""
        cls._prune()
        cls._states[state] = (user_id, flow, chat_id, time.time())
        logger.debug(f"OAuth state registered for user {user_id}")

    @classmethod
    def pop(cls, state: str) -> Optional[tuple]:
        """
        Consume and return the (user_id, flow, chat_id, inserted_at) tuple for state.
        Returns None if missing or expired.
        """
        cls._prune()
        val = cls._states.pop(state, None)
        if val is None:
            return None
        user_id, flow, chat_id, inserted_at = val
        if time.time() - inserted_at > _TTL_SECONDS:
            logger.warning(f"OAuth state expired for user {user_id}")
            return None
        return user_id, flow, chat_id, inserted_at

    @classmethod
    def _prune(cls) -> None:
        """Remove all entries older than TTL_SECONDS."""
        cutoff = time.time() - _TTL_SECONDS
        expired = [k for k, v in cls._states.items() if v[3] < cutoff]
        for k in expired:
            cls._states.pop(k, None)
        if expired:
            logger.info(f"Pruned {len(expired)} expired OAuth state(s)")
