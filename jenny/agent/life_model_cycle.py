"""Life Model maintenance pass — the second half of a Dream cycle.

Pass A (memory) consolidates history into MEMORY/USER/SOUL. Pass B (this
module) maintains the Life Model: it reads events, updates current_state, and
proposes pattern candidates. Pass B is daily-gated (current_state mtime > 24h),
never talks to the user, and never touches the Dream cursor or the memory files.

It is additive and independent of the memory pass: no cursor, no review, no
stuck counter. If it fails, the memory pass result is unaffected.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from jenny.agent.tools.list_events import ListEventsTool
from jenny.agent.tools.pattern_candidate_manager import PatternCandidateManagerTool
from jenny.agent.tools.registry import ToolRegistry
from jenny.agent.tools.update_current_state import UpdateCurrentStateTool
from jenny.life_model.store import LifeModelStore
from jenny.utils.prompt_templates import render_template

LIFE_MODEL_PASS_INTERVAL_H = 24
_MAX_EVENTS_IN_PROMPT = 50


async def _silent(*_args: Any, **_kwargs: Any) -> None:
    return None


def build_life_model_tools(workspace: str | Path) -> ToolRegistry:
    """The restricted toolset for the Life Model pass: read events, write state."""
    tools = ToolRegistry()
    tools.register(ListEventsTool(workspace))
    tools.register(UpdateCurrentStateTool(workspace))
    tools.register(PatternCandidateManagerTool(workspace))
    return tools


def life_model_pass_due(store: LifeModelStore) -> bool:
    """True when current_state.md is old enough (or missing) to warrant a pass.

    Uses the file mtime as the "updated_at" signal, honoring the no-cursor
    boundary of LifeModelStore — no separate tracking state.
    """
    try:
        mtime = datetime.fromtimestamp(store.current_state_file.stat().st_mtime)
    except OSError:
        return True
    return datetime.now() - mtime >= timedelta(hours=LIFE_MODEL_PASS_INTERVAL_H)


def build_life_model_prompt(store: LifeModelStore) -> str:
    events = store.list_events()[:_MAX_EVENTS_IN_PROMPT]
    events_text = "\n".join(
        f"- {e.id} [{e.timestamp}] ({e.kind}) {e.first_line}" for e in events
    ) or "(none)"
    context = store.read_context_files()
    template = render_template("agent/life_model_update.md", strip=True)
    return (
        f"{template}\n\n"
        "## Current Life Model\n\n"
        f"### Life Goals\n{context['life_goals'] or '(none)'}\n\n"
        f"### Current State\n{context['current_state'] or '(none)'}\n\n"
        f"### Confirmed Patterns\n{context['patterns'] or '(none)'}\n\n"
        f"### Communication Preferences\n{context['expression'] or '(none)'}\n\n"
        f"## Recent Events\n\n{events_text}\n"
    )


async def run_life_model_pass(agent: Any, store: LifeModelStore) -> None:
    """Run the daily-gated Life Model pass, silently and ephemerally."""
    if not life_model_pass_due(store):
        logger.debug("Life Model pass: not due yet")
        return
    prompt = build_life_model_prompt(store)
    tools = build_life_model_tools(store.workspace)
    from jenny.agent.memory import MemoryStore

    await agent.process_direct(
        prompt,
        session_key=MemoryStore.dream_session_key(),
        ephemeral=True,
        tools=tools,
        on_progress=_silent,
    )
