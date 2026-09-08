"""list_events: Dream's read window into the event log (Life Model).

Read-only. Lists event metadata newest-first, or opens specific events in full
when ids are given (the same two-step pattern as ``recall``). The main agent
does NOT have this tool: events are excluded from the main context on purpose,
so a live conversation never gets a "psychologist's file" view of the past.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jenny.agent.tools.base import Tool, tool_parameters
from jenny.life_model.store import LifeModelStore


@tool_parameters({
    "type": "object",
    "properties": {
        "ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Event ids to open in full. Omit to list all event metadata first.",
        },
    },
})
class ListEventsTool(Tool):
    # Dream-only: never loaded into the main agent's registry by ToolLoader.
    _scopes = set()

    def __init__(self, workspace: str | Path):
        self._store = LifeModelStore(workspace)

    @property
    def name(self) -> str:
        return "list_events"

    @property
    def description(self) -> str:
        return (
            "List the Life Model event log (events/), newest first, or open "
            "specific events in full by id. Read-only. Use it to review salient "
            "events before updating current_state or proposing pattern candidates."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, ids: list[str] | None = None, **kwargs: Any) -> str:
        if ids:
            return self._open(ids)
        return self._list()

    def _list(self) -> str:
        refs = self._store.list_events()
        if not refs:
            return "No life events recorded yet."
        lines = [f"- {r.id} [{r.timestamp}] ({r.kind}) {r.first_line}" for r in refs]
        return "\n".join(lines)

    def _open(self, ids: list[str]) -> str:
        parts: list[str] = []
        for event_id in ids:
            body = self._store.read_event(event_id)
            if body:
                parts.append(f"[{event_id}]\n{body}")
            else:
                parts.append(f"No event with id {event_id}.")
        return "\n\n".join(parts)
