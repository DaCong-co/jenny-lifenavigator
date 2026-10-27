"""Expression layer: turn a structured decision into one human message.

The Core decides *what* is true and whether a conversation is warranted. This
layer decides *how* to say it, under the user's communication preferences and
the recent context. It runs as a second, silent, tool-less turn and returns plain
text; the permission layer has already authorised speaking before it is called.

Natural language is the last step on purpose: the structured decision stays
auditable even when the wording changes.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from loguru import logger

from jenny.navigator.models import NavigatorDecision, OutcomeRecord, utc_now
from jenny.utils.prompt_templates import render_template

EXPRESSION_SESSION_KEY = "internal:navigator_expression"
MAX_MESSAGE_CHARS = 600

_BOILERPLATE = (
    "as an ai", "i am an ai", "i'm an ai", "as a language model",
    "作为ai", "作为人工智能", "语言模型",
)


async def _silent(*_args: Any, **_kwargs: Any) -> None:
    return None


class ExpressionLayer:
    """Compose the final message; never decide whether to speak."""

    async def compose(
        self,
        agent: Any,
        decision: NavigatorDecision,
        *,
        expression_preferences: str = "",
        recent_conversation: Iterable[dict[str, str]] = (),
        previous_outcomes: Iterable[OutcomeRecord] = (),
        now: Any = None,
    ) -> str | None:
        prompt = self.build_prompt(
            decision,
            expression_preferences=expression_preferences,
            recent_conversation=recent_conversation,
            previous_outcomes=previous_outcomes,
            now=now,
        )
        raw = await self._run(agent, prompt)
        return self.sanitize(raw)

    def build_prompt(
        self,
        decision: NavigatorDecision,
        *,
        expression_preferences: str = "",
        recent_conversation: Iterable[dict[str, str]] = (),
        previous_outcomes: Iterable[OutcomeRecord] = (),
        now: Any = None,
    ) -> str:
        template = render_template("agent/navigator_expression.md", strip=True)
        conversation = "\n".join(
            f"- {message.get('role', '?')}: {str(message.get('content', ''))[:240]}"
            for message in list(recent_conversation)[-8:]
            if str(message.get("content", "")).strip()
        ) or "(none)"
        past = "\n".join(
            f"- {outcome.action_kind}: said \"{outcome.delivered_text[:120]}\"; "
            f"reaction={outcome.reaction}, effectiveness={outcome.effectiveness}"
            for outcome in list(previous_outcomes)[-5:]
        ) or "(none)"
        action = decision.recommended_action
        return "\n".join([
            template,
            "",
            "## Structured decision",
            "",
            f"- observed facts: {'; '.join(decision.observed_facts) or '(none)'}",
            f"- state assessment: {decision.state_assessment or '(none)'}",
            f"- conflicts: {'; '.join(decision.conflicts) or '(none)'}",
            f"- confidence: {decision.confidence}",
            f"- proposed action kind: {action.kind}",
            f"- proposed action: {action.description or '(none)'}",
            f"- need user discussion: {decision.need_user_discussion}",
            "",
            "## Communication preferences (user-owned)",
            "",
            (expression_preferences or "(none)")[:2000],
            "",
            "## Recent conversation",
            "",
            conversation,
            "",
            "## Recent Navigator interventions",
            "",
            past,
            "",
            "Return ONLY the message the user will read. No preamble.",
            "",
        ])

    async def _run(self, agent: Any, prompt: str) -> str:
        from jenny.agent.tools.registry import ToolRegistry
        from jenny.session.turn_visibility import TurnVisibility

        outcome = await agent.process_direct_outcome(
            prompt,
            session_key=EXPRESSION_SESSION_KEY,
            ephemeral=True,
            persist_user_message=False,
            tools=ToolRegistry(),
            visibility=TurnVisibility.SILENT,
            on_progress=_silent,
        )
        return str(getattr(outcome, "final_text", "") or "")

    @staticmethod
    def sanitize(raw: str) -> str | None:
        from jenny.utils.helpers import strip_think

        text = strip_think(raw or "").strip()
        if not text:
            return None
        if text.startswith("```") and text.endswith("```"):
            text = text.strip("`").strip()
        text = re.sub(r"^[#>*\-\s]+", "", text)
        text = text.strip().strip("\"'“”‘’ ").strip()
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        lowered = text.casefold()
        if any(marker in lowered for marker in _BOILERPLATE):
            logger.warning("Navigator expression: dropped model boilerplate")
            return None
        if not text:
            return None
        if len(text) > MAX_MESSAGE_CHARS:
            cut = text[:MAX_MESSAGE_CHARS]
            boundary = max(cut.rfind("。"), cut.rfind("."), cut.rfind("！"), cut.rfind("?"))
            text = cut[:boundary + 1].strip() if boundary >= 40 else cut.rstrip()
        return text or None


def expression_inputs(
    decision: NavigatorDecision,
    *,
    expression_preferences: str = "",
    recent_conversation: Iterable[dict[str, str]] = (),
    previous_outcomes: Iterable[OutcomeRecord] = (),
) -> dict[str, Any]:
    """Small helper for logging/tests: the exact inputs the layer saw."""
    return {
        "decision_id": decision.id,
        "expression_preferences": expression_preferences,
        "recent_conversation": list(recent_conversation),
        "previous_outcomes": [outcome.id for outcome in previous_outcomes],
        "composed_at": utc_now().isoformat(timespec="seconds"),
    }
