"""State aggregator: deterministic, evidence-linked facts about the user.

The aggregator is the only place where observations become readable state. It
computes numbers and writes *facts with a time range and evidence ids*; it never
diagnoses. "3 acknowledgement-only replies in 7 days" is a fact; "the user is
resisting" is a hypothesis and belongs to the Navigator Core.

Seven dimensions, no overall score. A score would collapse exactly the conflicts
the Navigator exists to notice.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from jenny.life_model.store import EventRef, LifeModelStore
from jenny.navigator.models import (
    CHANGE_CHANGED,
    CHANGE_STABLE,
    CHANGE_UNKNOWN,
    DIMENSION_KEYS,
    Dimension,
    Observation,
    OutcomeRecord,
    StateSnapshot,
    iso,
    parse_iso,
    utc_now,
)
from jenny.navigator.perception import is_acknowledgement, is_short_reply
from jenny.navigator.store import NavigatorStore

_USER_MESSAGE = "user_message"
_HEALTH_SLEEP = "sleep"
_HEALTH_STEPS = "steps"
_HEALTH_HEART = "heart_rate"
_HEALTH_WORKOUT = frozenset({"activity", "workout"})
_ENVIRONMENT_KINDS = frozenset({
    "weather", "calendar", "environment", "location", "power", "network",
})

_DECISION_LIKE_LIFE_EVENTS = frozenset({"decision", "goal_change", "milestone", "setback"})
_GOAL_LIKE_LIFE_EVENTS = frozenset({"goal_change", "milestone", "setback"})


def _safe_ratio(part: int, whole: int) -> float:
    return round(part / whole, 3) if whole else 0.0


def _mean(values: Iterable[float]) -> float | None:
    numbers = [value for value in values if value is not None]
    return round(sum(numbers) / len(numbers), 2) if numbers else None


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _normalized_message(text: str) -> str:
    return re.sub(r"[\s，,。.!！?？~～、:：;；]+", "", (text or "").lower())


def _longest_streak(flags: list[bool]) -> int:
    best = current = 0
    for flag in flags:
        current = current + 1 if flag else 0
        best = max(best, current)
    return best


@dataclass(frozen=True, slots=True)
class ConversationMetrics:
    messages: int = 0
    short: int = 0
    ack: int = 0
    questions: int = 0
    night: int = 0
    ack_streak: int = 0
    corrections: int = 0
    dismissals: int = 0
    chars: int = 0
    active_days: int = 0
    active_hours: tuple[int, ...] = ()

    @property
    def short_ratio(self) -> float:
        return _safe_ratio(self.short, self.messages)

    @property
    def ack_ratio(self) -> float:
        return _safe_ratio(self.ack, self.messages)


@dataclass(frozen=True, slots=True)
class AggregationContext:
    """Everything the aggregator needs, gathered once (tests can build it directly)."""

    now: datetime
    window_start: datetime
    window_end: datetime
    prior_start: datetime
    observations: tuple[Observation, ...] = ()
    prior_observations: tuple[Observation, ...] = ()
    life_events: tuple[EventRef, ...] = ()
    prior_life_events: tuple[EventRef, ...] = ()
    outcomes: tuple[OutcomeRecord, ...] = ()
    current_state_age_hours: float | None = None
    life_goals_chars: int = 0
    patterns_chars: int = 0
    environment_model_chars: int = 0


@dataclass(slots=True)
class _Facts:
    facts: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    change: str = CHANGE_UNKNOWN


class StateAggregator:
    """Build a :class:`StateSnapshot` from observations, events and outcomes."""

    def __init__(
        self,
        store: NavigatorStore,
        *,
        workspace: str | Path | None = None,
        life_model: LifeModelStore | None = None,
        timezone_name: str | None = None,
        now_fn: Any = None,
    ) -> None:
        self.store = store
        self.workspace = Path(workspace) if workspace else store.workspace
        self.life_model = life_model or LifeModelStore(self.workspace)
        self.timezone_name = timezone_name or ""
        self._now_fn = now_fn or utc_now

    def build(self, *, window_days: int = 7, now: datetime | None = None) -> StateSnapshot:
        end = now or self._now_fn()
        if end.tzinfo is None:
            end = end.astimezone()
        start = end - timedelta(days=max(1, window_days))
        prior_start = start - timedelta(days=max(1, window_days))
        context = self._gather(start, end, prior_start)
        label = f"{start.date().isoformat()}..{end.date().isoformat()}"

        messages = [o for o in context.observations if o.kind == _USER_MESSAGE]
        prior_messages = [o for o in context.prior_observations if o.kind == _USER_MESSAGE]
        current = self._conversation_metrics(messages)
        prior = self._conversation_metrics(prior_messages)

        dimensions: dict[str, Dimension] = {}

        body = self._body_state(context, label)
        dimensions["body_state"] = Dimension(
            "body_state", tuple(body.facts), tuple(body.evidence), body.change,
        )

        emotional = self._emotional_state(context, current, prior, label)
        dimensions["emotional_state"] = Dimension(
            "emotional_state", tuple(emotional.facts), tuple(emotional.evidence), emotional.change,
        )

        behavioral = self._behavioral_state(context, current, prior, label)
        dimensions["behavioral_state"] = Dimension(
            "behavioral_state", tuple(behavioral.facts), tuple(behavioral.evidence),
            behavioral.change,
        )

        cognitive = self._cognitive_state(context, messages, label)
        dimensions["cognitive_state"] = Dimension(
            "cognitive_state", tuple(cognitive.facts), tuple(cognitive.evidence), cognitive.change,
        )

        environment = self._environment_state(context, label)
        dimensions["environment_state"] = Dimension(
            "environment_state", tuple(environment.facts), tuple(environment.evidence),
            environment.change,
        )

        development = self._development_state(context, label)
        dimensions["development_state"] = Dimension(
            "development_state", tuple(development.facts), tuple(development.evidence),
            development.change,
        )

        effectiveness = self._navigator_effectiveness(context, label)
        dimensions["navigator_effectiveness"] = Dimension(
            "navigator_effectiveness", tuple(effectiveness.facts),
            tuple(effectiveness.evidence), effectiveness.change,
        )

        evidence: list[str] = []
        for key in DIMENSION_KEYS:
            for item in dimensions[key].evidence_ids:
                if item not in evidence:
                    evidence.append(item)

        metrics: dict[str, Any] = {
            "user_messages": current.messages,
            "prior_user_messages": prior.messages,
            "short_replies": current.short,
            "short_ratio": current.short_ratio,
            "prior_short_ratio": prior.short_ratio,
            "ack_replies": current.ack,
            "ack_ratio": current.ack_ratio,
            "ack_streak": current.ack_streak,
            "night_messages": current.night,
            "questions": current.questions,
            "corrections": current.corrections,
            "dismissals": current.dismissals,
            "active_days": current.active_days,
            "life_model_events": len(context.life_events),
            "goal_like_events": sum(
                1 for event in context.life_events if event.kind in _GOAL_LIKE_LIFE_EVENTS
            ),
            # A high-value event is a reason to *think now*, not for the next
            # seven days. The trigger reads this 24h metric, not the window one.
            "goal_like_events_24h": sum(
                1 for event in context.life_events
                if event.kind in _GOAL_LIKE_LIFE_EVENTS
                and (when := parse_iso(event.timestamp)) is not None
                and when >= end - timedelta(hours=24)
            ),
            "interventions": len(context.outcomes),
            "rejected_outcomes": sum(
                1 for outcome in context.outcomes
                if outcome.reaction in ("rejected", "corrected")
                or outcome.effectiveness in ("ineffective", "side_effect")
            ),
            "window_days": max(1, window_days),
        }

        return StateSnapshot(
            timestamp=iso(end),
            window_start=iso(start),
            window_end=iso(end),
            dimensions=dimensions,
            metrics=metrics,
            evidence_ids=tuple(evidence[:300]),
        )

    # -- gathering ----------------------------------------------------------

    def _gather(
        self, start: datetime, end: datetime, prior_start: datetime,
    ) -> AggregationContext:
        observations = tuple(self.store.list_observations(since=start, until=end))
        prior_observations = tuple(
            self.store.list_observations(since=prior_start, until=start)
        )
        life_events_all = self._life_events()
        life_events = tuple(
            event for event in life_events_all if self._event_in_window(event, start, end)
        )
        prior_life_events = tuple(
            event for event in life_events_all
            if self._event_in_window(event, prior_start, start)
        )
        outcomes = tuple(self.store.list_outcomes(limit=200))
        outcomes = tuple(
            outcome for outcome in outcomes
            if (when := parse_iso(outcome.created_at)) is not None and start <= when <= end
        )
        try:
            age = (
                (end - datetime.fromtimestamp(
                    self.life_model.current_state_file.stat().st_mtime, tz=timezone.utc,
                )).total_seconds() / 3600
            )
        except OSError:
            age = None
        if age is not None:
            # A file written "in the future" is clock skew, not -14h of age.
            age = max(0.0, age)
        return AggregationContext(
            now=end,
            window_start=start,
            window_end=end,
            prior_start=prior_start,
            observations=observations,
            prior_observations=prior_observations,
            life_events=life_events,
            prior_life_events=prior_life_events,
            outcomes=outcomes,
            current_state_age_hours=round(age, 1) if age is not None else None,
            life_goals_chars=len(self.life_model.read_life_goals().strip()),
            patterns_chars=len(self.life_model.read_patterns().strip()),
            environment_model_chars=len(self.store.read_environment_model()),
        )

    def _life_events(self) -> tuple[EventRef, ...]:
        try:
            return tuple(self.life_model.list_events())
        except OSError:
            return ()

    @staticmethod
    def _event_in_window(event: EventRef, start: datetime, end: datetime) -> bool:
        when = parse_iso(event.timestamp)
        return when is not None and start <= when <= end

    @staticmethod
    def _conversation_metrics(messages: list[Observation]) -> ConversationMetrics:
        ack_flags: list[bool] = []
        days: set[str] = set()
        hours: set[int] = set()
        short = ack = questions = night = corrections = dismissals = chars = 0
        for message in messages:
            value = message.value
            text = str(value.get("text", ""))
            short += 1 if bool(value.get("is_short")) or is_short_reply(text) else 0
            ack_here = bool(value.get("is_ack")) or is_acknowledgement(text)
            ack += 1 if ack_here else 0
            ack_flags.append(ack_here)
            questions += 1 if value.get("is_question") else 0
            hour = int(value.get("local_hour", 0) or 0)
            hours.add(hour)
            night += 1 if hour < 6 else 0
            corrections += 1 if value.get("is_correction") else 0
            dismissals += 1 if value.get("is_dismissal") else 0
            chars += int(value.get("chars", len(text)) or 0)
            day = str(value.get("local_date", ""))
            if day:
                days.add(day)
        return ConversationMetrics(
            messages=len(messages),
            short=short,
            ack=ack,
            questions=questions,
            night=night,
            ack_streak=_longest_streak(ack_flags),
            corrections=corrections,
            dismissals=dismissals,
            chars=chars,
            active_days=len(days),
            active_hours=tuple(sorted(hours)),
        )

    # -- dimensions ---------------------------------------------------------

    def _body_state(self, context: AggregationContext, label: str) -> _Facts:
        relevant = [
            o for o in context.observations
            if o.kind in (_HEALTH_SLEEP, _HEALTH_STEPS, _HEALTH_HEART)
            or o.kind in _HEALTH_WORKOUT
        ]
        if not relevant:
            return _Facts(
                facts=[f"{label}: no body data observed (sleep, steps, heart rate, activity)"],
                change=CHANGE_UNKNOWN,
            )
        facts: list[str] = []
        evidence: list[str] = []
        change = CHANGE_UNKNOWN
        sleeps = self._sleep_hours(relevant)
        if sleeps:
            avg = _mean(sleeps)
            facts.append(
                f"{label}: {len(sleeps)} sleep record(s), average {avg}h"
            )
            evidence.extend(o.id for o in relevant if o.kind == _HEALTH_SLEEP)
            prior_sleeps = self._sleep_hours([
                o for o in context.prior_observations if o.kind == _HEALTH_SLEEP
            ])
            if avg is not None and prior_sleeps:
                prior_avg = _mean(prior_sleeps)
                if prior_avg is not None:
                    change = (
                        CHANGE_CHANGED if abs(avg - prior_avg) >= 0.75 else CHANGE_STABLE
                    )
            elif avg is not None:
                change = CHANGE_STABLE
        steps = [v for o in relevant if o.kind == _HEALTH_STEPS
                 if (v := _numeric(o.value.get("steps"))) is not None]
        if steps:
            facts.append(f"{label}: {len(steps)} step record(s), average {int(_mean(steps) or 0)}")
            evidence.extend(o.id for o in relevant if o.kind == _HEALTH_STEPS)
        hearts = [v for o in relevant if o.kind == _HEALTH_HEART
                  if (v := _numeric(o.value.get("bpm") or o.value.get("heart_rate"))) is not None]
        if hearts:
            facts.append(f"{label}: {len(hearts)} heart-rate record(s), average {int(_mean(hearts) or 0)} bpm")
            evidence.extend(o.id for o in relevant if o.kind == _HEALTH_HEART)
        workouts = [o for o in relevant if o.kind in _HEALTH_WORKOUT]
        if workouts:
            facts.append(f"{label}: {len(workouts)} activity/workout record(s)")
            evidence.extend(o.id for o in workouts)
        return _Facts(facts=facts, evidence=evidence, change=change)

    @staticmethod
    def _sleep_hours(observations: list[Observation]) -> list[float]:
        values: list[float] = []
        for observation in observations:
            if observation.kind != _HEALTH_SLEEP:
                continue
            hours = _numeric(observation.value.get("sleep_hours"))
            if hours is None:
                minutes = _numeric(observation.value.get("sleep_minutes"))
                hours = minutes / 60 if minutes is not None else None
            if hours is not None and 0 < hours <= 16:
                values.append(hours)
        return values

    def _emotional_state(
        self,
        context: AggregationContext,
        current: ConversationMetrics,
        prior: ConversationMetrics,
        label: str,
    ) -> _Facts:
        messages = [o for o in context.observations if o.kind == _USER_MESSAGE]
        if not current.messages:
            return _Facts(
                facts=[f"{label}: no user messages observed"],
                change=CHANGE_UNKNOWN,
            )
        facts = [
            f"{label}: {current.messages} user message(s); {current.short} short (<=8 chars); "
            f"{current.ack} acknowledgement-only; longest acknowledgement streak "
            f"{current.ack_streak}; {current.night} between 00:00 and 06:00"
        ]
        if current.ack >= 3:
            facts.append(
                f"{label}: {current.ack} acknowledgement-only replies "
                f"(repeated '知道了' style) — a repeated pattern, not a conclusion"
            )
        if current.corrections or current.dismissals:
            facts.append(
                f"{label}: {current.corrections} explicit correction(s), "
                f"{current.dismissals} explicit 'I am fine / back off' message(s)"
            )
        change = CHANGE_UNKNOWN
        if current.messages >= 3 and prior.messages >= 3:
            short_delta = current.short_ratio - prior.short_ratio
            ack_delta = current.ack_ratio - prior.ack_ratio
            change = (
                CHANGE_CHANGED
                if short_delta >= 0.25 or ack_delta >= 0.25
                else CHANGE_STABLE
            )
        evidence = [o.id for o in messages[-6:]]
        return _Facts(facts=facts, evidence=evidence, change=change)

    def _behavioral_state(
        self,
        context: AggregationContext,
        current: ConversationMetrics,
        prior: ConversationMetrics,
        label: str,
    ) -> _Facts:
        app_usage = [o for o in context.observations if o.kind in ("app_usage", "screen")]
        notifications = [o for o in context.observations if o.kind == "notification"]
        if not current.messages and not app_usage and not notifications:
            return _Facts(
                facts=[f"{label}: no conversation activity observed"],
                change=CHANGE_UNKNOWN,
            )
        facts = [
            f"{label}: {current.messages} user turn(s) over {current.active_days} active day(s); "
            f"active hours {list(current.active_hours)}; {current.night} message(s) after midnight"
        ]
        if app_usage:
            minutes = sum(
                value for o in app_usage
                if (value := _numeric(o.value.get("minutes"))) is not None
            )
            facts.append(
                f"{label}: {len(app_usage)} app-usage/screen record(s), "
                f"{int(minutes)} total minute(s)"
            )
        if notifications:
            facts.append(f"{label}: {len(notifications)} notification record(s)")
            sources = Counter(
                str(o.value.get("package", ""))
                for o in notifications if o.value.get("package")
            )
            if sources:
                facts.append(
                    f"{label}: notification sources: "
                    + ", ".join(f"{name} {count}" for name, count in sources.most_common(3))
                )
        change = CHANGE_UNKNOWN
        current_app_minutes = self._app_minutes(context.observations)
        prior_app_minutes = self._app_minutes(context.prior_observations)
        if current.messages >= 3 and prior.messages >= 3:
            ratio = current.messages / prior.messages
            change = CHANGE_CHANGED if ratio >= 1.6 or ratio <= 0.6 else CHANGE_STABLE
        if current_app_minutes >= 30.0 and prior_app_minutes >= 30.0:
            app_ratio = current_app_minutes / prior_app_minutes
            if app_ratio >= 1.6 or app_ratio <= 0.6:
                change = CHANGE_CHANGED
        messages = [o for o in context.observations if o.kind == _USER_MESSAGE]
        evidence = [o.id for o in messages[-6:]]
        evidence.extend(o.id for o in app_usage[-3:])
        evidence.extend(o.id for o in notifications[-3:])
        return _Facts(facts=facts, evidence=evidence, change=change)

    @staticmethod
    def _app_minutes(observations: Iterable[Observation]) -> float:
        total = 0.0
        for observation in observations:
            if observation.kind not in ("app_usage", "screen"):
                continue
            value = _numeric(observation.value.get("minutes"))
            if value is not None and value > 0:
                total += value
        return total

    def _cognitive_state(
        self, context: AggregationContext, messages: list[Observation], label: str,
    ) -> _Facts:
        facts: list[str] = []
        evidence: list[str] = []
        counts: Counter[str] = Counter()
        first_seen: dict[str, Observation] = {}
        for message in messages:
            text = _normalized_message(str(message.value.get("text", "")))
            if len(text) < 2:
                continue
            counts[text] += 1
            first_seen.setdefault(text, message)
        repeated = [(text, n) for text, n in counts.items() if n >= 2]
        if repeated:
            for text, n in sorted(repeated, key=lambda item: -item[1])[:3]:
                original = str(first_seen[text].value.get("text", text))[:40]
                facts.append(f"{label}: the same message was sent {n} times: \"{original}\"")
                evidence.append(first_seen[text].id)
        else:
            facts.append(f"{label}: no repeated identical user message")

        decisions = [
            event for event in context.life_events if event.kind in _DECISION_LIKE_LIFE_EVENTS
        ]
        facts.append(
            f"{label}: {len(decisions)} decision/goal event(s) recorded in the Life Model"
        )
        evidence.extend(event.id for event in decisions[:5])

        change = CHANGE_UNKNOWN
        if messages or decisions:
            change = CHANGE_CHANGED if repeated or decisions else CHANGE_STABLE
        return _Facts(facts=facts, evidence=evidence, change=change)

    def _environment_state(self, context: AggregationContext, label: str) -> _Facts:
        relevant = [o for o in context.observations if o.kind in _ENVIRONMENT_KINDS]
        events = [e for e in self.store.list_events(since=context.window_start, until=context.window_end)
                  if e.type == "environment_change"]
        if not relevant and not events:
            return _Facts(
                facts=[
                    f"{label}: no environment data observed; the Environment Model is "
                    f"{'present' if context.environment_model_chars else 'empty'}"
                ],
                change=CHANGE_UNKNOWN,
            )
        facts: list[str] = []
        evidence: list[str] = []
        by_kind = Counter(o.kind for o in relevant)
        for kind, count in sorted(by_kind.items()):
            facts.append(f"{label}: {count} {kind} observation(s)")
            evidence.extend(o.id for o in relevant if o.kind == kind)
        places = {
            str(o.value.get("place") or o.value.get("city") or "").strip()
            for o in relevant if o.kind == "location"
        }
        places.discard("")
        if places:
            facts.append(f"{label}: coarse location(s): {', '.join(sorted(places)[:5])}")
        power = [o for o in relevant if o.kind == "power"]
        if power:
            last = power[-1].value
            battery = last.get("battery_percent")
            facts.append(
                f"{label}: device battery {battery if battery is not None else '?'}%, "
                f"charging={bool(last.get('charging'))}, "
                f"screen_on={bool(last.get('screen_on'))}, "
                f"power_save={bool(last.get('power_save'))}, "
                f"network={last.get('network') or '?'}"
            )
        if events:
            facts.append(f"{label}: {len(events)} environment_change event(s)")
            evidence.extend(event.id for event in events[:5])
        return _Facts(facts=facts, evidence=evidence,
                      change=CHANGE_CHANGED if events else CHANGE_STABLE)

    def _development_state(self, context: AggregationContext, label: str) -> _Facts:
        facts: list[str] = []
        evidence: list[str] = []
        if context.life_goals_chars:
            facts.append(f"{label}: life_goals.md is written ({context.life_goals_chars} chars)")
        else:
            facts.append(f"{label}: life_goals.md is still empty")
        if context.patterns_chars:
            facts.append(f"{label}: patterns.md is written ({context.patterns_chars} chars)")
        goal_events = [e for e in context.life_events if e.kind in _GOAL_LIKE_LIFE_EVENTS]
        facts.append(f"{label}: {len(goal_events)} goal/milestone/setback event(s)")
        evidence.extend(event.id for event in goal_events[:5])
        if context.current_state_age_hours is None:
            facts.append(f"{label}: current_state.md never updated")
        else:
            facts.append(
                f"{label}: current_state.md last updated {context.current_state_age_hours}h ago"
            )
        change = CHANGE_UNKNOWN
        if goal_events:
            change = CHANGE_CHANGED
        elif context.life_goals_chars or context.current_state_age_hours is not None:
            change = CHANGE_STABLE
        return _Facts(facts=facts, evidence=evidence, change=change)

    def _navigator_effectiveness(
        self, context: AggregationContext, label: str,
    ) -> _Facts:
        if not context.outcomes:
            return _Facts(
                facts=[f"{label}: no Navigator intervention in this window"],
                change=CHANGE_UNKNOWN,
            )
        reactions = Counter(outcome.reaction for outcome in context.outcomes)
        effectiveness = Counter(outcome.effectiveness for outcome in context.outcomes)
        facts = [
            f"{label}: {len(context.outcomes)} intervention(s); reactions "
            + ", ".join(f"{name} {count}" for name, count in sorted(reactions.items())),
            f"{label}: effectiveness "
            + ", ".join(f"{name} {count}" for name, count in sorted(effectiveness.items())),
        ]
        change = CHANGE_UNKNOWN
        if context.outcomes:
            change = (
                CHANGE_CHANGED
                if (
                    reactions.get("rejected", 0)
                    or reactions.get("corrected", 0)
                    or effectiveness.get("ineffective", 0)
                    or effectiveness.get("side_effect", 0)
                )
                else CHANGE_STABLE
            )
        return _Facts(
            facts=facts,
            evidence=[outcome.id for outcome in context.outcomes[:10]],
            change=change,
        )


def snapshot_text(snapshot: StateSnapshot) -> str:
    """Render a snapshot for a prompt: facts only, with their evidence ids."""
    lines: list[str] = []
    for key in DIMENSION_KEYS:
        dimension = snapshot.dimension(key)
        lines.append(
            f"### {key} ({dimension.change})\n"
            + ("\n".join(f"- {fact}" for fact in dimension.facts) or "- (no facts)")
            + (
                f"\n- evidence: {', '.join(dimension.evidence_ids[:12])}"
                if dimension.evidence_ids else ""
            )
        )
    return "\n\n".join(lines)
