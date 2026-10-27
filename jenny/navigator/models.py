"""Value types for the Life Navigator runtime.

The runtime keeps four layers of data apart, because mixing them is how an
inference silently becomes a "fact" about the user:

    Observation  ->  Event  ->  StateSnapshot  ->  Hypothesis
    (raw, local)     (meaningful change)  (aggregated, evidence-linked)

Nothing in this module performs I/O or calls an LLM. Every type is
JSON-serialisable through ``to_dict`` / ``from_dict`` so the stores stay plain
JSONL files on the phone.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

# The seven dimensions of the user state model. They are deliberately *not*
# collapsed into a score: a single number would hide exactly the conflicts the
# Navigator exists to notice.
DIMENSION_KEYS: tuple[str, ...] = (
    "body_state",
    "emotional_state",
    "behavioral_state",
    "cognitive_state",
    "environment_state",
    "development_state",
    "navigator_effectiveness",
)

DIMENSION_LABELS: dict[str, str] = {
    "body_state": "Body",
    "emotional_state": "Emotional / language",
    "behavioral_state": "Behavior",
    "cognitive_state": "Cognition / decisions",
    "environment_state": "Environment",
    "development_state": "Goals / development",
    "navigator_effectiveness": "Navigator effectiveness",
}

# ``unknown`` is not a soft ``stable``: it means there is no evidence at all.
CHANGE_UNKNOWN = "unknown"
CHANGE_STABLE = "stable"
CHANGE_CHANGED = "changed"

CONFIDENCE_LEVELS: tuple[str, ...] = ("low", "medium", "high")

HYPOTHESIS_STATUSES: tuple[str, ...] = (
    "observing",      # proposed, not enough evidence yet
    "supported",      # more evidence, still a hypothesis
    "rejected",       # evidence went against it
    "invalidated",    # a new judgment or a user correction killed it
)

ACTION_KINDS: tuple[str, ...] = ("observe", "discuss", "suggest", "act")
ACTION_RISKS: tuple[str, ...] = ("none", "low", "medium", "high")

OUTCOME_STATUSES: tuple[str, ...] = ("pending", "evaluated")
OUTCOME_REACTIONS: tuple[str, ...] = (
    "unknown", "engaged", "acknowledged", "rejected", "corrected", "no_response",
)
OUTCOME_EFFECTIVENESS: tuple[str, ...] = (
    "unknown", "effective", "ineffective", "side_effect",
)


class TriggerPriority(str, Enum):
    """Scheduling priority of a Navigator analysis. **Not** user severity."""

    NO_TRIGGER = "no_trigger"
    LOW_PRIORITY = "low_priority"
    NORMAL = "normal"
    IMPORTANT = "important"
    URGENT = "urgent"

    @property
    def rank(self) -> int:
        return _PRIORITY_RANK[self.value]

    def at_least(self, other: "TriggerPriority") -> bool:
        return self.rank >= other.rank


_PRIORITY_RANK: dict[str, int] = {
    TriggerPriority.NO_TRIGGER.value: 0,
    TriggerPriority.LOW_PRIORITY.value: 1,
    TriggerPriority.NORMAL.value: 2,
    TriggerPriority.IMPORTANT.value: 3,
    TriggerPriority.URGENT.value: 4,
}


def priority_from(value: str | None) -> TriggerPriority:
    try:
        return TriggerPriority(str(value))
    except ValueError:
        return TriggerPriority.NO_TRIGGER


def _clean_confidence(value: Any, default: str = "low") -> str:
    text = str(value or "").strip().lower()
    return text if text in CONFIDENCE_LEVELS else default


def _clean_action_kind(value: Any, default: str = "observe") -> str:
    text = str(value or "").strip().lower()
    return text if text in ACTION_KINDS else default


def _clean_action_risk(value: Any, default: str = "none") -> str:
    text = str(value or "").strip().lower()
    return text if text in ACTION_RISKS else default


def _clean_status(value: Any, default: str = "observing") -> str:
    text = str(value or "").strip().lower()
    return text if text in HYPOTHESIS_STATUSES else default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes", "y", "on"):
            return True
        if text in ("false", "0", "no", "n", "off", ""):
            return False
    if value is None:
        return default
    return bool(value)


def _as_tuple(value: Any, *, limit: int = 50) -> tuple[str, ...]:
    """Coerce a JSON value into a bounded tuple of non-empty strings."""
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return tuple(out)


def _clip(text: Any, limit: int) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    """ISO-8601 UTC with a ``Z`` suffix; lexicographically sortable."""
    value = dt or utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    # Life Model event ids use ``YYYYMMDD-HHMMSS`` (local, no timezone). Accept
    # them so the runtime can reason over both stores without a second parser.
    if re.fullmatch(r"\d{8}-\d{6}", text):
        try:
            naive = datetime.strptime(text, "%Y%m%d-%H%M%S")
        except ValueError:
            return None
        return naive.astimezone()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def content_id(text: str, *, length: int = 12) -> str:
    normalized = "\n".join(line.rstrip() for line in str(text).strip().splitlines())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:length]


def make_id(prefix: str, payload: str, when: datetime | None = None) -> str:
    stamp = (when or utc_now()).astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{prefix}_{stamp}_{content_id(payload, length=8)}"


def local_hour(dt: datetime, timezone_name: str | None) -> int:
    if not timezone_name:
        return dt.astimezone().hour
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:  # pragma: no cover - Python 3.11 always has zoneinfo
        return dt.astimezone().hour
    try:
        return dt.astimezone(ZoneInfo(timezone_name)).hour
    except (ZoneInfoNotFoundError, ValueError):
        return dt.astimezone().hour


def local_date(dt: datetime, timezone_name: str | None) -> str:
    if not timezone_name:
        return dt.astimezone().date().isoformat()
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:  # pragma: no cover
        return dt.astimezone().date().isoformat()
    try:
        return dt.astimezone(ZoneInfo(timezone_name)).date().isoformat()
    except (ZoneInfoNotFoundError, ValueError):
        return dt.astimezone().date().isoformat()


# ---------------------------------------------------------------------------
# Observation


@dataclass(frozen=True, slots=True)
class Observation:
    """One raw, local observation. Never sent to the LLM as-is.

    ``value`` carries the source-specific payload (message length, sleep
    minutes, battery, …). ``metadata`` carries provenance only (session key,
    channel, redaction flag). Both are bounded by the perception layer.
    """

    id: str
    timestamp: str
    source: str
    kind: str
    value: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "source": self.source,
            "kind": self.kind,
            "value": dict(self.value),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Observation":
        raw_value = data.get("value")
        raw_metadata = data.get("metadata")
        return cls(
            id=str(data.get("id", "")),
            timestamp=str(data.get("timestamp", "")),
            source=str(data.get("source", "")),
            kind=str(data.get("kind", "")),
            value=dict(raw_value) if isinstance(raw_value, dict) else {},
            metadata=dict(raw_metadata) if isinstance(raw_metadata, dict) else {},
        )


# ---------------------------------------------------------------------------
# Event


@dataclass(frozen=True, slots=True)
class NavigatorEvent:
    """Something that happened, stated as a fact and linked to observations."""

    id: str
    timestamp: str
    type: str
    summary: str
    source_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "type": self.type,
            "summary": self.summary,
            "source_ids": list(self.source_ids),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NavigatorEvent":
        raw_metadata = data.get("metadata")
        return cls(
            id=str(data.get("id", "")),
            timestamp=str(data.get("timestamp", "")),
            type=str(data.get("type", "")),
            summary=str(data.get("summary", "")),
            source_ids=_as_tuple(data.get("source_ids")),
            metadata=dict(raw_metadata) if isinstance(raw_metadata, dict) else {},
        )


# ---------------------------------------------------------------------------
# State snapshot


@dataclass(frozen=True, slots=True)
class Dimension:
    """One dimension of the user state: facts + evidence + whether it moved.

    ``change`` is a *measurement* (``unknown`` / ``stable`` / ``changed``), not
    an evaluation. It says the numbers moved, never that the user is worse.
    """

    key: str
    facts: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    change: str = CHANGE_UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": DIMENSION_LABELS.get(self.key, self.key),
            "facts": list(self.facts),
            "evidence_ids": list(self.evidence_ids),
            "change": self.change,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Dimension":
        raw_change = str(data.get("change", CHANGE_UNKNOWN))
        if raw_change not in (CHANGE_UNKNOWN, CHANGE_STABLE, CHANGE_CHANGED):
            raw_change = CHANGE_UNKNOWN
        return cls(
            key=str(data.get("key", "")),
            facts=_as_tuple(data.get("facts"), limit=20),
            evidence_ids=_as_tuple(data.get("evidence_ids")),
            change=raw_change,
        )


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    """Aggregated, evidence-linked state at one instant.

    No overall score exists on purpose. ``metrics`` holds raw deterministic
    numbers used by the trigger engine, never shown to the user as a grade.
    """

    timestamp: str
    window_start: str
    window_end: str
    dimensions: dict[str, Dimension] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    evidence_ids: tuple[str, ...] = ()

    def dimension(self, key: str) -> Dimension:
        return self.dimensions.get(key) or Dimension(key=key)

    @property
    def changed_dimensions(self) -> tuple[str, ...]:
        return tuple(
            key for key in DIMENSION_KEYS
            if self.dimension(key).change == CHANGE_CHANGED
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "dimensions": {key: dim.to_dict() for key, dim in self.dimensions.items()},
            "metrics": dict(self.metrics),
            "evidence_ids": list(self.evidence_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StateSnapshot":
        raw_dims = data.get("dimensions") or {}
        metrics = data.get("metrics")
        dimensions: dict[str, Dimension] = {}
        if isinstance(raw_dims, dict):
            for key in DIMENSION_KEYS:
                raw = raw_dims.get(key)
                if isinstance(raw, dict):
                    dimensions[key] = Dimension.from_dict(raw)
        return cls(
            timestamp=str(data.get("timestamp", "")),
            window_start=str(data.get("window_start", "")),
            window_end=str(data.get("window_end", "")),
            dimensions=dimensions,
            metrics=dict(metrics) if isinstance(metrics, dict) else {},
            evidence_ids=_as_tuple(data.get("evidence_ids"), limit=200),
        )


# ---------------------------------------------------------------------------
# Hypothesis


@dataclass(frozen=True, slots=True)
class Hypothesis:
    """An inference with evidence and a confidence. Never auto-promoted to fact."""

    id: str
    statement: str
    evidence_ids: tuple[str, ...] = ()
    confidence: str = "low"
    status: str = "observing"
    created_at: str = ""
    updated_at: str = ""
    origin: str = "navigator"
    decision_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "statement": self.statement,
            "evidence_ids": list(self.evidence_ids),
            "confidence": self.confidence,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "origin": self.origin,
            "decision_id": self.decision_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Hypothesis":
        return cls(
            id=str(data.get("id", "")),
            statement=str(data.get("statement", "")),
            evidence_ids=_as_tuple(data.get("evidence_ids")),
            confidence=_clean_confidence(data.get("confidence")),
            status=_clean_status(data.get("status")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            origin=str(data.get("origin", "navigator")),
            decision_id=str(data.get("decision_id", "")),
        )


@dataclass(frozen=True, slots=True)
class HypothesisDraft:
    statement: str
    confidence: str = "low"
    evidence_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "statement": self.statement,
            "confidence": self.confidence,
            "evidence_ids": list(self.evidence_ids),
        }


# ---------------------------------------------------------------------------
# Trigger decision


@dataclass(frozen=True, slots=True)
class TriggerDecision:
    """The answer to one question: is a thought worth an LLM call right now?

    ``allow_speak`` is separate from ``priority``: an analysis can be worth
    running while the user must not be interrupted (a correction, a pending
    outcome evaluation, or a user-initiated turn the main agent is already
    answering).
    """

    priority: TriggerPriority = TriggerPriority.NO_TRIGGER
    reasons: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    allow_speak: bool = False
    throttled: bool = False
    next_eligible_at: str | None = None

    @property
    def should_analyze(self) -> bool:
        return self.priority.rank >= TriggerPriority.NORMAL.rank

    def to_dict(self) -> dict[str, Any]:
        return {
            "priority": self.priority.value,
            "reasons": list(self.reasons),
            "evidence_ids": list(self.evidence_ids),
            "allow_speak": self.allow_speak,
            "throttled": self.throttled,
            "next_eligible_at": self.next_eligible_at,
        }


# ---------------------------------------------------------------------------
# Navigator decision


@dataclass(frozen=True, slots=True)
class RecommendedAction:
    kind: str = "observe"
    description: str = ""
    risk: str = "none"
    reversible: bool = True
    requires_confirmation: bool = False

    @classmethod
    def from_dict(cls, data: Any) -> "RecommendedAction":
        if not isinstance(data, dict):
            return cls()
        kind = _clean_action_kind(data.get("kind"))
        risk = _clean_action_risk(data.get("risk"))
        reversible = _as_bool(data.get("reversible"), True)
        return cls(
            kind=kind,
            description=_clip(data.get("description"), 500),
            risk=risk,
            reversible=reversible,
            requires_confirmation=_as_bool(data.get("requires_confirmation"), False),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "description": self.description,
            "risk": self.risk,
            "reversible": self.reversible,
            "requires_confirmation": self.requires_confirmation,
        }


@dataclass(frozen=True, slots=True)
class NavigatorDecision:
    """Structured output of the Navigator Core, before any natural language."""

    id: str
    timestamp: str
    observed_facts: tuple[str, ...] = ()
    hypotheses: tuple[HypothesisDraft, ...] = ()
    confidence: str = "low"
    state_assessment: str = ""
    conflicts: tuple[str, ...] = ()
    recommended_action: RecommendedAction = field(default_factory=RecommendedAction)
    need_user_discussion: bool = False
    reasoning_evidence_ids: tuple[str, ...] = ()
    invalid_evidence_ids: tuple[str, ...] = ()
    parse_ok: bool = True
    raw_excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "observed_facts": list(self.observed_facts),
            "hypotheses": [h.to_dict() for h in self.hypotheses],
            "confidence": self.confidence,
            "state_assessment": self.state_assessment,
            "conflicts": list(self.conflicts),
            "recommended_action": self.recommended_action.to_dict(),
            "need_user_discussion": self.need_user_discussion,
            "reasoning_evidence_ids": list(self.reasoning_evidence_ids),
            "invalid_evidence_ids": list(self.invalid_evidence_ids),
            "parse_ok": self.parse_ok,
            "raw_excerpt": self.raw_excerpt,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NavigatorDecision":
        raw_hypotheses = data.get("hypotheses") or []
        hypotheses: list[HypothesisDraft] = []
        if isinstance(raw_hypotheses, list):
            for raw in raw_hypotheses[:10]:
                if not isinstance(raw, dict):
                    continue
                statement = _clip(raw.get("statement"), 400)
                if not statement:
                    continue
                hypotheses.append(HypothesisDraft(
                    statement=statement,
                    confidence=_clean_confidence(raw.get("confidence")),
                    evidence_ids=_as_tuple(raw.get("evidence_ids")),
                ))
        return cls(
            id=str(data.get("id", "")),
            timestamp=str(data.get("timestamp", "")),
            observed_facts=_as_tuple(data.get("observed_facts"), limit=20),
            hypotheses=tuple(hypotheses),
            confidence=_clean_confidence(data.get("confidence")),
            state_assessment=_clip(data.get("state_assessment"), 1500),
            conflicts=_as_tuple(data.get("conflicts"), limit=10),
            recommended_action=RecommendedAction.from_dict(data.get("recommended_action")),
            need_user_discussion=_as_bool(data.get("need_user_discussion"), False),
            reasoning_evidence_ids=_as_tuple(data.get("reasoning_evidence_ids")),
            invalid_evidence_ids=_as_tuple(data.get("invalid_evidence_ids")),
            parse_ok=_as_bool(data.get("parse_ok"), True),
            raw_excerpt=str(data.get("raw_excerpt", ""))[:500],
        )


# ---------------------------------------------------------------------------
# Outcomes and self-model


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    """What the Navigator did, how the user reacted, and whether it helped."""

    id: str
    intervention_id: str
    decision_id: str
    action_kind: str
    action_description: str
    delivered_text: str
    created_at: str
    window_end: str
    status: str = "pending"
    reaction: str = "unknown"
    effectiveness: str = "unknown"
    evidence_ids: tuple[str, ...] = ()
    note: str = ""
    evaluated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "intervention_id": self.intervention_id,
            "decision_id": self.decision_id,
            "action_kind": self.action_kind,
            "action_description": self.action_description,
            "delivered_text": self.delivered_text,
            "created_at": self.created_at,
            "window_end": self.window_end,
            "status": self.status,
            "reaction": self.reaction,
            "effectiveness": self.effectiveness,
            "evidence_ids": list(self.evidence_ids),
            "note": self.note,
            "evaluated_at": self.evaluated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OutcomeRecord":
        status = str(data.get("status", "pending"))
        reaction = str(data.get("reaction", "unknown"))
        effectiveness = str(data.get("effectiveness", "unknown"))
        return cls(
            id=str(data.get("id", "")),
            intervention_id=str(data.get("intervention_id", "")),
            decision_id=str(data.get("decision_id", "")),
            action_kind=_clean_action_kind(data.get("action_kind")),
            action_description=str(data.get("action_description", "")),
            delivered_text=str(data.get("delivered_text", "")),
            created_at=str(data.get("created_at", "")),
            window_end=str(data.get("window_end", "")),
            status=status if status in OUTCOME_STATUSES else "pending",
            reaction=reaction if reaction in OUTCOME_REACTIONS else "unknown",
            effectiveness=(
                effectiveness if effectiveness in OUTCOME_EFFECTIVENESS else "unknown"
            ),
            evidence_ids=_as_tuple(data.get("evidence_ids")),
            note=str(data.get("note", "")),
            evaluated_at=str(data.get("evaluated_at", "")),
        )


@dataclass(frozen=True, slots=True)
class SelfModelNote:
    """A note about the *Navigator's own* judgement, never about the user."""

    id: str
    created_at: str
    kind: str
    statement: str
    evidence_ids: tuple[str, ...] = ()
    confidence: str = "medium"
    active: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "kind": self.kind,
            "statement": self.statement,
            "evidence_ids": list(self.evidence_ids),
            "confidence": self.confidence,
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SelfModelNote":
        return cls(
            id=str(data.get("id", "")),
            created_at=str(data.get("created_at", "")),
            kind=str(data.get("kind", "")),
            statement=str(data.get("statement", "")),
            evidence_ids=_as_tuple(data.get("evidence_ids")),
            confidence=_clean_confidence(data.get("confidence"), "medium"),
            active=_as_bool(data.get("active"), True),
        )


@dataclass(frozen=True, slots=True)
class RuleVersion:
    """One controlled change of the Navigator's own behaviour rules."""

    version: int
    effective_time: str
    old_rule: dict[str, Any] = field(default_factory=dict)
    new_rule: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    evidence: tuple[str, ...] = ()
    discussion_result: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "effective_time": self.effective_time,
            "old_rule": dict(self.old_rule),
            "new_rule": dict(self.new_rule),
            "reason": self.reason,
            "evidence": list(self.evidence),
            "discussion_result": self.discussion_result,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuleVersion":
        try:
            version = int(data.get("version", 0))
        except (TypeError, ValueError):
            version = 0
        raw_old = data.get("old_rule")
        raw_new = data.get("new_rule")
        return cls(
            version=max(0, version),
            effective_time=str(data.get("effective_time", "")),
            old_rule=dict(raw_old) if isinstance(raw_old, dict) else {},
            new_rule=dict(raw_new) if isinstance(raw_new, dict) else {},
            reason=str(data.get("reason", "")),
            evidence=_as_tuple(data.get("evidence")),
            discussion_result=str(data.get("discussion_result", "")),
        )
