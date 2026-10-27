"""Agent hook that records user turns for the Navigator.

This hook is the "eyes" of the runtime on the conversation: one local
observation per turn, no LLM call, no delivery. Thinking happens in the hourly
cycle, not here — that is the "continuous perception, not continuous thought"
rule made mechanical.

It is not mounted on ephemeral runs (Dream, the gardener, the Navigator's own
Core/Expression turns), so internal work cannot recursively become user history.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from jenny.agent.hook import AgentHook, AgentRunHookContext


class NavigatorPerceptionHook(AgentHook):
    """Persist user/assistant turn metrics; never interprets or speaks."""

    def __init__(self, observer: Any) -> None:
        super().__init__()
        self._observer = observer

    def runs_when_ephemeral(self) -> bool:
        # An ephemeral turn is internal work with no user to observe.
        return False

    async def after_run(self, context: AgentRunHookContext) -> None:
        if self._observer is None:
            return
        session_key = context.session_key or ""
        if not session_key:
            return
        try:
            from jenny.session.keys import is_internal_session_key
            if is_internal_session_key(session_key):
                return
        except Exception:
            return
        user_text = _last_user_text(context.messages)
        if not user_text:
            return
        try:
            # Two small JSONL writes and a config read: cheap, but doing them on
            # the event loop would put file I/O in the middle of a user turn.
            await asyncio.to_thread(
                self._observer.observe_turn,
                session_key=session_key,
                user_text=user_text,
                assistant_text=context.final_content or "",
                now=None,
            )
        except Exception:
            # Observation is not worth failing a user turn for.
            logger.opt(exception=True).warning("Navigator: could not observe turn")


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    try:
        from jenny.session.history_meta import is_synthetic_history_row
    except Exception:  # pragma: no cover - defensive for partial installs
        def is_synthetic_history_row(_message: Any) -> bool:
            return False
    for message in reversed(messages or []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        # Only the newest user row is this turn. If it is synthetic (a subagent
        # result, an injection) or has no text (media-only), stop here rather
        # than recording an older message a second time.
        if is_synthetic_history_row(message):
            return ""
        return _content_text(message.get("content"))
    return ""


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts).strip()
    return ""
