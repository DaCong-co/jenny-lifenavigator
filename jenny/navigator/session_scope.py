"""Lifecycle helper for the Navigator's own ephemeral LLM turns.

``process_direct_outcome(..., ephemeral=True)`` skips history *injection* but
still saves the session at the end of the turn. With a fixed internal session
key that means the Core's JSON and the Expression's text accumulate and are
replayed into the next prompt — the opposite of an ephemeral turn. Dream avoids
this by using a unique key per run and pruning it; the Navigator uses one stable
key per layer and clears it before each turn, which is cheaper and bounded.
"""

from __future__ import annotations

from typing import Any

from loguru import logger


def reset_internal_session(agent: Any, session_key: str) -> None:
    """Start the next internal turn from an empty, saved session.

    Best-effort by design: a broken session store must not prevent the Navigator
    from thinking, and the next turn will try again.
    """
    sessions = getattr(agent, "sessions", None)
    if sessions is None:
        return
    try:
        session = sessions.get_or_create(session_key)
        session.clear()
        sessions.save(session)
    except Exception:
        logger.opt(exception=True).debug(
            "Navigator: could not reset internal session {}", session_key,
        )
