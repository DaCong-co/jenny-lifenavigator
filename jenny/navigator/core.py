"""Navigator Core: the one place where an LLM looks at the aggregated state.

Input is *curated evidence*, never raw sensors: the aggregated snapshot, recent
events, the Life Model, the Environment Model, recent interventions and a short
slice of conversation. Output is structured JSON — facts, hypotheses, confidence,
conflicts, a recommended action — and only then does a separate expression layer
write natural language.

The Core runs with an **empty tool registry**. It can think and speak through
the layers around it, but it cannot act, write files, or bypass permissions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from loguru import logger

from jenny.navigator.models import (
    DIMENSION_KEYS,
    HypothesisDraft,
    NavigatorDecision,
    NavigatorEvent,
    OutcomeRecord,
    RecommendedAction,
    SelfModelNote,
    StateSnapshot,
    iso,
    make_id,
    utc_now,
)
from jenny.utils.prompt_templates import render_template

CORE_SESSION_KEY = "internal:navigator_core"
MAX_EVENTS_IN_PROMPT = 15
MAX_OUTCOMES_IN_PROMPT = 10
MAX_HYPOTHESES_IN_PROMPT = 10
MAX_NOTES_IN_PROMPT = 8
MAX_CONVERSATION_IN_PROMPT = 12


async def _silent(*_args: Any, **_kwargs: Any) -> None:
    return None


@dataclass(frozen=True, slots=True)
class CoreInputs:
    """All curated evidence handed to the Core. No raw observation payloads."""

    snapshot: StateSnapshot
    recent_events: tuple[NavigatorEvent, ...] = ()
    outcomes: tuple[OutcomeRecord, ...] = ()
    hypotheses: tuple[Any, ...] = ()
    self_notes: tuple[SelfModelNote, ...] = ()
    recent_conversation: tuple[dict[str, str], ...] = ()
    life_model: dict[str, str] = field(default_factory=dict)
    environment_model: str = ""
    trigger_reasons: tuple[str, ...] = ()


class NavigatorCore:
    """Structured analysis through one silent, tool-less LLM turn."""

    def __init__(self) -> None:
        pass

    async def analyze(self, agent: Any, inputs: CoreInputs) -> NavigatorDecision:
        prompt = self.build_prompt(inputs)
        allowed = self.allowed_evidence(inputs)
        raw = await self._run(agent, prompt)
        return self.parse_decision(raw, allowed)

    def build_prompt(self, inputs: CoreInputs) -> str:
        template = render_template("agent/navigator_core.md", strip=True)
        snapshot = inputs.snapshot
        lines = [
            template,
            "",
            "## Aggregated state",
            "",
            f"Window: {snapshot.window_start} .. {snapshot.window_end}",
            "",
            "Why this analysis was triggered: "
            + ("; ".join(inputs.trigger_reasons) or "(no reasons recorded)"),
            "",
            _format_snapshot(snapshot),
            "",
            "## Recent events",
            "",
            _format_events(inputs.recent_events),
            "",
            "## Recent Navigator interventions",
            "",
            _format_outcomes(inputs.outcomes),
            "",
            "## Active hypotheses (not facts)",
            "",
            _format_hypotheses(inputs.hypotheses),
            "",
            "## Navigator self-model",
            "",
            _format_notes(inputs.self_notes),
            "",
            "## Life Model",
            "",
            f"### Life goals\n{_bounded(inputs.life_model.get('life_goals'), 4000) or '(none)'}",
            "",
            f"### Current state (model-maintained, not a fact)\n"
            f"{_bounded(inputs.life_model.get('current_state'), 4000) or '(none)'}",
            "",
            f"### Confirmed patterns\n"
            f"{_bounded(inputs.life_model.get('patterns'), 4000) or '(none)'}",
            "",
            f"### Communication preferences\n"
            f"{_bounded(inputs.life_model.get('expression'), 2000) or '(none)'}",
            "",
            "## Environment Model",
            "",
            _bounded(inputs.environment_model, 4000) or "(none)",
            "",
            "## Recent conversation",
            "",
            _format_conversation(inputs.recent_conversation),
            "",
            "## Evidence ids you may cite",
            "",
            ", ".join(sorted(self.allowed_evidence(inputs))[:200]) or "(none)",
            "",
            "## Output contract",
            "",
            "Return ONE JSON object with the keys from the instructions above. "
            "No prose, no markdown fence. Facts stay facts, hypotheses stay "
            "hypotheses, and every cited id must come from the evidence list.",
            "",
        ]
        return "\n".join(lines)

    @staticmethod
    def allowed_evidence(inputs: CoreInputs) -> set[str]:
        allowed: set[str] = set(inputs.snapshot.evidence_ids)
        for dimension_key in DIMENSION_KEYS:
            allowed.update(inputs.snapshot.dimension(dimension_key).evidence_ids)
        for event in inputs.recent_events:
            allowed.add(event.id)
            allowed.update(event.source_ids)
        for outcome in inputs.outcomes:
            allowed.add(outcome.id)
            allowed.update(outcome.evidence_ids)
        for hypothesis in inputs.hypotheses:
            hypothesis_id = getattr(hypothesis, "id", "")
            if hypothesis_id:
                allowed.add(str(hypothesis_id))
        return allowed

    async def _run(self, agent: Any, prompt: str) -> str:
        from jenny.agent.tools.registry import ToolRegistry
        from jenny.session.turn_visibility import TurnVisibility

        outcome = await agent.process_direct_outcome(
            prompt,
            session_key=CORE_SESSION_KEY,
            ephemeral=True,
            persist_user_message=False,
            tools=ToolRegistry(),
            visibility=TurnVisibility.SILENT,
            on_progress=_silent,
        )
        return str(getattr(outcome, "final_text", "") or "")

    def parse_decision(
        self, raw: str, allowed: set[str], *, now: Any = None,
    ) -> NavigatorDecision:
        """Parse the model's JSON, filter evidence ids, never invent facts."""
        from jenny.utils.helpers import strip_think

        cleaned = strip_think(raw or "").strip()
        decision_id = make_id("dec", cleaned[:4000], now or utc_now())
        timestamp = iso(now)
        if not cleaned:
            return self._failed(decision_id, timestamp, "empty response", cleaned)

        candidate = _extract_json(cleaned)
        if candidate is None:
            return self._failed(decision_id, timestamp, "no JSON object in response", cleaned)
        if not isinstance(candidate, dict):
            return self._failed(decision_id, timestamp, "JSON is not an object", cleaned)

        try:
            decision = NavigatorDecision.from_dict({
                **candidate,
                "id": decision_id,
                "timestamp": timestamp,
                "parse_ok": True,
                "raw_excerpt": cleaned[:500],
            })
        except Exception as exc:  # noqa: BLE001 - parse failures must not escape
            logger.opt(exception=True).warning("Navigator Core: invalid decision payload")
            return self._failed(decision_id, timestamp, f"{type(exc).__name__}", cleaned)

        reasoning: list[str] = []
        invalid: list[str] = []
        for evidence_id in decision.reasoning_evidence_ids:
            if evidence_id in allowed:
                reasoning.append(evidence_id)
            else:
                invalid.append(evidence_id)

        hypotheses: list[HypothesisDraft] = []
        for draft in decision.hypotheses:
            valid_evidence = tuple(item for item in draft.evidence_ids if item in allowed)
            invalid.extend(item for item in draft.evidence_ids if item not in allowed)
            if not valid_evidence:
                # A hypothesis with no traceable evidence is not a hypothesis.
                continue
            hypotheses.append(HypothesisDraft(
                statement=draft.statement,
                confidence=draft.confidence,
                evidence_ids=valid_evidence,
            ))

        action = decision.recommended_action
        if action.kind not in ("observe", "discuss", "suggest", "act"):
            action = RecommendedAction(kind="observe", description=action.description)

        return NavigatorDecision(
            id=decision.id,
            timestamp=decision.timestamp,
            observed_facts=decision.observed_facts,
            hypotheses=tuple(hypotheses),
            confidence=decision.confidence,
            state_assessment=decision.state_assessment,
            conflicts=decision.conflicts,
            recommended_action=action,
            need_user_discussion=decision.need_user_discussion,
            reasoning_evidence_ids=tuple(reasoning),
            invalid_evidence_ids=tuple(dict.fromkeys(invalid))[:20],
            parse_ok=True,
            raw_excerpt=decision.raw_excerpt,
        )

    @staticmethod
    def _failed(decision_id: str, timestamp: str, reason: str, raw: str) -> NavigatorDecision:
        logger.warning("Navigator Core: keeping silence ({})", reason)
        return NavigatorDecision(
            id=decision_id,
            timestamp=timestamp,
            confidence="low",
            state_assessment=f"(analysis unavailable: {reason})",
            recommended_action=RecommendedAction(kind="observe"),
            need_user_discussion=False,
            parse_ok=False,
            raw_excerpt=(raw or "")[:500],
        )


def _extract_json(text: str) -> Any:
    """Tolerant JSON extraction: fenced blocks, prose around an object, repairs."""
    candidates: list[str] = []
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            body = "\n".join(
                line for line in lines[1:] if not line.strip().startswith("```")
            )
            candidates.append(body.strip())
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        candidates.append(stripped[start:end + 1])
    candidates.append(stripped)
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except (TypeError, ValueError):
            continue
        try:
            import json_repair
            return json_repair.loads(candidate)
        except Exception:  # noqa: BLE001 - a repair failure is just a parse failure
            continue
    return None


# -- prompt rendering helpers ------------------------------------------------


def _bounded(text: Any, limit: int) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _format_snapshot(snapshot: StateSnapshot) -> str:
    blocks: list[str] = []
    for key in DIMENSION_KEYS:
        dimension = snapshot.dimension(key)
        facts = "\n".join(f"- {fact}" for fact in dimension.facts) or "- (no facts)"
        evidence = (
            f"\n- evidence ids: {', '.join(dimension.evidence_ids[:12])}"
            if dimension.evidence_ids else ""
        )
        blocks.append(
            f"### {key} — {dimension.change}\n{facts}{evidence}"
        )
    return "\n\n".join(blocks)


def _format_events(events: Iterable[NavigatorEvent]) -> str:
    rows = []
    for event in list(events)[-MAX_EVENTS_IN_PROMPT:]:
        rows.append(
            f"- [{event.id}] {event.timestamp} ({event.type}) {event.summary}"
            + (
                f" | evidence: {', '.join(event.source_ids[:5])}"
                if event.source_ids else ""
            )
        )
    return "\n".join(rows) or "(none)"


def _format_outcomes(outcomes: Iterable[OutcomeRecord]) -> str:
    rows = []
    for outcome in list(outcomes)[-MAX_OUTCOMES_IN_PROMPT:]:
        rows.append(
            f"- [{outcome.id}] {outcome.action_kind}: "
            f"reaction={outcome.reaction}, effectiveness={outcome.effectiveness}; "
            f"what was said: {outcome.delivered_text[:160] or '(nothing)'}"
        )
    return "\n".join(rows) or "(none)"


def _format_hypotheses(hypotheses: Iterable[Any]) -> str:
    rows = []
    for hypothesis in list(hypotheses)[-MAX_HYPOTHESES_IN_PROMPT:]:
        rows.append(
            f"- [{getattr(hypothesis, 'id', '?')}] "
            f"({getattr(hypothesis, 'confidence', '?')}/"
            f"{getattr(hypothesis, 'status', '?')}) "
            f"{getattr(hypothesis, 'statement', '')}"
        )
    return "\n".join(rows) or "(none)"


def _format_notes(notes: Iterable[SelfModelNote]) -> str:
    rows = [
        f"- ({note.kind}) {note.statement}"
        for note in list(notes)[-MAX_NOTES_IN_PROMPT:]
    ]
    return "\n".join(rows) or "(none)"


def _format_conversation(messages: Iterable[dict[str, str]]) -> str:
    rows = []
    for message in list(messages)[-MAX_CONVERSATION_IN_PROMPT:]:
        role = str(message.get("role", "?"))
        content = str(message.get("content", "")).strip().replace("\n", " ")
        if content:
            rows.append(f"- {role}: {content[:300]}")
    return "\n".join(rows) or "(none)"
