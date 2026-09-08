"""record_event: the main agent's only write path into the Life Model.

The agent observes a salient life event (a goal change, a setback, a decision)
and records it here. Events are append-only and never enter the main context:
Dream distills them into ``current_state`` and pattern candidates on its own
cadence. This tool is the boundary between "the user said something" and "the
model noticed something worth keeping".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jenny.agent.tools.base import Tool, tool_parameters
from jenny.life_model.store import LifeModelStore, _VALID_EVENT_KINDS


@tool_parameters({
    "type": "object",
    "properties": {
        "content": {
            "type": "string",
            "description": (
                "What happened, as an observable fact that affects the user's future "
                "state. Not a chat-log line, not a diagnosis of the user's character."
            ),
        },
        "kind": {
            "type": "string",
            "enum": sorted(_VALID_EVENT_KINDS),
            "description": "The category of the event.",
        },
    },
    "required": ["content", "kind"],
})
class RecordEventTool(Tool):
    _scopes = {"core", "orchestrator"}

    def __init__(self, workspace: str | Path):
        self._store = LifeModelStore(workspace)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(workspace=ctx.workspace)

    @property
    def name(self) -> str:
        return "record_event"

    @property
    def description(self) -> str:
        return (
            "Record a salient life event into the Life Model (events/). Use only "
            "for things that affect the user's future state — a goal change, a "
            "setback, a milestone, a significant decision. This is not a chat log: "
            "do not record ordinary turns. Events are observed facts, never "
            "judgments about the user."
        )

    async def execute(self, content: str, kind: str, **kwargs: Any) -> str:
        event_id = self._store.write_event(content, kind=kind)
        return f"Recorded life event {event_id} ({kind})."


TOOLS = [RecordEventTool]
