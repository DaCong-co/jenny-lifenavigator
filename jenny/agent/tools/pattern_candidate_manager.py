"""pattern_candidate_manager: Dream's entry editor for pattern candidates.

pattern_candidates.md is an entry collection, not a file to rewrite: each
candidate is a hypothesis with evidence, a confidence, a status, and a
time-bounded observed_period. Promotion to patterns.md is v2 — this tool only
manages candidates, it never turns one into a confirmed pattern.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jenny.agent.tools.base import Tool, tool_parameters
from jenny.life_model.store import _VALID_CANDIDATE_STATUSES, LifeModelStore

_VALID_CONFIDENCE = ("low", "medium", "high")


@tool_parameters({
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["add", "update_status", "remove"],
            "description": "Which candidate operation to perform.",
        },
        "candidate": {
            "type": "string",
            "description": "The hypothesis text (for add).",
        },
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Independent evidence items, from >=2 different contexts (for add). "
                "Two chat messages in the same hour are NOT independent evidence."
            ),
        },
        "observed_period": {
            "type": "string",
            "description": "Time range the candidate covers, e.g. '2026-08-01 ~ 2026-09-03' (for add).",
        },
        "confidence": {
            "type": "string",
            "enum": list(_VALID_CONFIDENCE),
            "description": "How confident the observation is (for add).",
        },
        "candidate_id": {
            "type": "string",
            "description": "The candidate id (for update_status / remove).",
        },
        "status": {
            "type": "string",
            "enum": sorted(_VALID_CANDIDATE_STATUSES),
            "description": "New status (for update_status).",
        },
    },
    "required": ["action"],
})
class PatternCandidateManagerTool(Tool):
    # Dream-only: never loaded into the main agent's registry by ToolLoader.
    _scopes = set()

    def __init__(self, workspace: str | Path):
        self._store = LifeModelStore(workspace)

    @property
    def name(self) -> str:
        return "pattern_candidate_manager"

    @property
    def description(self) -> str:
        return (
            "Manage pattern candidates in the Life Model. Add a candidate "
            "(hypothesis + evidence + confidence + observed_period), change its "
            "status (observing/supported/rejected), or remove it. Candidates are "
            "never auto-promoted to confirmed patterns."
        )

    async def execute(self, action: str, **kwargs: Any) -> str:
        if action == "add":
            candidate_id = self._store.add_pattern_candidate(
                kwargs.get("candidate") or "",
                kwargs.get("evidence") or [],
                observed_period=kwargs.get("observed_period") or "",
                confidence=kwargs.get("confidence") or "low",
            )
            return f"Added candidate {candidate_id}."
        if action == "update_status":
            self._store.update_pattern_candidate_status(
                kwargs.get("candidate_id") or "",
                kwargs.get("status") or "",
            )
            return f"Updated candidate {kwargs.get('candidate_id')} status."
        if action == "remove":
            self._store.remove_pattern_candidate(kwargs.get("candidate_id") or "")
            return f"Removed candidate {kwargs.get('candidate_id')}."
        raise ValueError(f"unknown action {action!r}")
