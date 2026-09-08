"""update_current_state: Dream's full-snapshot write into the Life Model.

current_state.md is a bounded snapshot (study/energy/pressure/phase), not a log.
Every write replaces the whole file atomically. This tool belongs to the Dream
Life Model pass only — the main agent can never write it (the deny-list and the
tool registry both keep it out of reach).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jenny.agent.tools.base import Tool, tool_parameters
from jenny.life_model.store import LifeModelStore


@tool_parameters({
    "type": "object",
    "properties": {
        "content": {
            "type": "string",
            "description": (
                "The full current_state snapshot. Observable, time-bounded facts "
                "only (study/energy/pressure/phase); never diagnoses or permanent "
                "personality labels."
            ),
        },
    },
    "required": ["content"],
})
class UpdateCurrentStateTool(Tool):
    # Dream-only: never loaded into the main agent's registry by ToolLoader.
    _scopes = set()

    def __init__(self, workspace: str | Path):
        self._store = LifeModelStore(workspace)

    @property
    def name(self) -> str:
        return "update_current_state"

    @property
    def description(self) -> str:
        return (
            "Replace the Life Model current_state snapshot with the given content. "
            "Write observable, time-bounded facts (study/energy/pressure/phase), "
            "never diagnoses or permanent personality labels."
        )

    async def execute(self, content: str, **kwargs: Any) -> str:
        self._store.update_current_state(content)
        return "Updated current_state."
