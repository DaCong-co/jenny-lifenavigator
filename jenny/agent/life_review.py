"""life_review: the periodic, user-facing reflection pass.

Reads the Life Model and recent events, then decides whether to message the
user. Defaults to silence; only a clear signal (sustained goal drift, a
high-value event, or an explicit user request to be reminded) escalates to a
``message``. It never writes the model — current_state, candidates and events
are all read-only here (v1.5: "life_review 只读可能说").

Runs as a weekly cron job (see ``CronDispatcher._run_life_review``). v1 is
cron-only — there is no ``/life_review`` command.
"""

from __future__ import annotations

from typing import Any

from jenny.life_model.store import LifeModelStore
from jenny.utils.prompt_templates import render_template

LIFE_REVIEW_SESSION_KEY = "internal:life_review"
_MAX_EVENTS_IN_PROMPT = 50


async def _silent(*_args: Any, **_kwargs: Any) -> None:
    return None


def build_life_review_prompt(store: LifeModelStore) -> str:
    events = store.list_events()[:_MAX_EVENTS_IN_PROMPT]
    events_text = "\n".join(
        f"- {e.id} [{e.timestamp}] ({e.kind}) {e.first_line}" for e in events
    ) or "(none)"
    context = store.read_context_files()
    template = render_template("agent/life_review.md", strip=True)
    return (
        f"{template}\n\n"
        "## Life Model\n\n"
        f"### Life Goals\n{context['life_goals'] or '(none)'}\n\n"
        f"### Current State\n{context['current_state'] or '(none)'}\n\n"
        f"### Confirmed Patterns\n{context['patterns'] or '(none)'}\n\n"
        f"### Communication Preferences\n{context['expression'] or '(none)'}\n\n"
        f"## Recent Events\n\n{events_text}\n"
    )


async def run_life_review(
    agent: Any,
    store: LifeModelStore,
    *,
    channel: str = "websocket",
    chat_id: str = "default",
) -> None:
    """Run one review turn: silent by default, escalating only via ``message``."""
    from jenny.session.turn_visibility import TurnVisibility
    from jenny.webui.metadata import WEBUI_MESSAGE_SOURCE_METADATA_KEY

    prompt = build_life_review_prompt(store)
    await agent.process_direct_outcome(
        prompt,
        session_key=LIFE_REVIEW_SESSION_KEY,
        channel=channel,
        chat_id=chat_id,
        on_progress=_silent,
        visibility=TurnVisibility.SILENT,
        metadata={WEBUI_MESSAGE_SOURCE_METADATA_KEY: {"kind": "life_review"}},
    )
