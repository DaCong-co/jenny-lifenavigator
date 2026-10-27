"""Trigger engine: decide whether a thought is worth an LLM call right now.

The engine answers exactly one question — *is it worth letting the LLM look?* —
and never diagnoses the user. It is deterministic, cheap and testable without a
provider. Priorities are scheduling priorities, not severity:

    no_trigger < low_priority < normal < important < urgent

``low_priority`` means "recorded, not analysed"; ``normal`` and above mean "run
the Navigator Core". ``allow_speak`` is a second, independent answer: an
analysis can be worth running while the user must not be interrupted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

from jenny.navigator.models import (
    CHANGE_CHANGED,
    NavigatorEvent,
    OutcomeRecord,
    StateSnapshot,
    TriggerDecision,
    TriggerPriority,
    iso,
    local_date,
    local_hour,
    parse_iso,
)
from jenny.navigator.rules import NavigatorRules

_DRIFT_DIMENSIONS = (
    "body_state", "emotional_state", "behavioral_state", "cognitive_state",
)
_HIGH_VALUE_EVENT_TYPES = frozenset({
    "goal_change", "setback", "milestone", "environment_change",
    "intervention_rejected",
})
# Events that justify reopening a topic after the user said "I'm fine". A
# rejected Navigator intervention is not new evidence about the user's life.
_REOPENING_EVENT_TYPES = frozenset({
    "goal_change", "setback", "milestone", "environment_change",
})
_ANALYSIS_ONLY_REASONS = frozenset({
    "user_active", "user_correction", "pending_outcome", "user_dismissal",
})


@dataclass(frozen=True, slots=True)
class TriggerContext:
    """Everything the trigger needs, already read from the stores."""

    now: datetime
    rules: NavigatorRules
    timezone_name: str = ""
    recent_snapshots: tuple[StateSnapshot, ...] = ()
    recent_events: tuple[NavigatorEvent, ...] = ()
    pending_outcomes: tuple[OutcomeRecord, ...] = ()
    last_outcome: OutcomeRecord | None = None
    recent_user_texts: tuple[str, ...] = ()
    watch_topics: tuple[str, ...] = ()
    state: dict[str, Any] = field(default_factory=dict)


class TriggerEngine:
    """Cheap local pre-filter in front of the Navigator Core."""

    def evaluate(self, snapshot: StateSnapshot, context: TriggerContext) -> TriggerDecision:
        now = context.now
        rules = context.rules
        reasons: list[str] = []
        evidence: list[str] = []
        priority = TriggerPriority.NO_TRIGGER
        external_signal = False
        reopening_event = False
        no_immediate_retry = False

        def bump(level: TriggerPriority, reason: str, *ids: str) -> None:
            nonlocal priority, external_signal
            if level.rank > priority.rank:
                priority = level
            if reason not in reasons:
                reasons.append(reason)
            for item in ids:
                if item and item not in evidence:
                    evidence.append(item)
            if level.rank >= TriggerPriority.NORMAL.rank:
                external_signal = True

        changed = snapshot.changed_dimensions
        if len(changed) >= 2:
            bump(
                TriggerPriority.NORMAL,
                "multi-domain change: " + ", ".join(changed),
                *[e for key in changed
                  for e in snapshot.dimension(key).evidence_ids[:3]],
            )

        moved_days = self._drift_days(snapshot, context)
        if moved_days >= max(1, rules.drift_min_days):
            bump(
                TriggerPriority.IMPORTANT,
                f"sustained drift: dimensions moved on {moved_days} distinct day(s)",
                *snapshot.evidence_ids[:8],
            )

        for event in context.recent_events:
            when = parse_iso(event.timestamp)
            if when is None or (now - when) > timedelta(hours=24):
                continue
            if event.type in _HIGH_VALUE_EVENT_TYPES:
                if event.type in _REOPENING_EVENT_TYPES:
                    reopening_event = True
                bump(
                    TriggerPriority.IMPORTANT,
                    f"high-value event: {event.type}",
                    event.id, *event.source_ids[:3],
                )

        # Life Model goal/milestone events are aggregated into the snapshot, not
        # copied into navigator events; the metric is the bridge.
        if int(snapshot.metrics.get("goal_like_events_24h", 0) or 0) > 0:
            reopening_event = True
            bump(
                TriggerPriority.IMPORTANT,
                "goal/milestone/setback event in the last 24 hours",
                *snapshot.dimension("development_state").evidence_ids[:3],
            )

        last = context.last_outcome
        if last is not None and last.status == "evaluated":
            when = parse_iso(last.created_at)
            if when is not None and (now - when) <= timedelta(hours=48):
                if (
                    last.reaction in ("rejected", "corrected")
                    or last.effectiveness in ("ineffective", "side_effect")
                ):
                    no_immediate_retry = (now - when) <= timedelta(hours=6)
                    bump(
                        TriggerPriority.IMPORTANT,
                        f"previous intervention did not help ({last.reaction}/{last.effectiveness})",
                        last.id, *last.evidence_ids[:3],
                    )

        for outcome in context.pending_outcomes:
            when = parse_iso(outcome.window_end)
            if when is not None and when <= now:
                bump(
                    TriggerPriority.LOW_PRIORITY,
                    "outcome window due",
                    outcome.id,
                )

        watch_hit = self._watch_hit(context)
        if watch_hit:
            bump(
                TriggerPriority.IMPORTANT,
                f"watched topic mentioned: {watch_hit}",
                *snapshot.evidence_ids[:3],
            )
            if reopening_event:
                priority = TriggerPriority.URGENT
                if "urgent watched-topic event" not in reasons:
                    reasons.append("urgent watched-topic event")

        dismissal = self._recent_event(
            context, "user_dismissal", hours=max(1, rules.dismissal_cooldown_hours),
        )
        correction = self._recent_event(context, "user_correction", hours=24)
        if correction is not None:
            bump(
                TriggerPriority.LOW_PRIORITY,
                "user_correction",
                correction.id,
            )
        if dismissal is not None:
            bump(TriggerPriority.LOW_PRIORITY, "user_dismissal", dismissal.id)

        if context.recent_user_texts:
            if priority.rank < TriggerPriority.NORMAL.rank:
                bump(
                    TriggerPriority.LOW_PRIORITY,
                    "user_active",
                    *snapshot.evidence_ids[:3],
                )
            elif not external_signal:
                # A user message alone is not a reason to interrupt a
                # conversation the main agent is already having.
                reasons.append("user_active")

        if dismissal is not None and not reopening_event:
            if priority.rank > TriggerPriority.LOW_PRIORITY.rank:
                priority = TriggerPriority.LOW_PRIORITY
            reasons.append("respecting the user's 'I am fine / back off' message")

        if priority.rank < TriggerPriority.NORMAL.rank:
            return TriggerDecision(
                priority=priority,
                reasons=tuple(reasons),
                evidence_ids=tuple(evidence[:100]),
                allow_speak=False,
            )

        # Throttles: the engine may be willing, but the phone and the user are not.
        run = self._cooldown_block(context)
        if run is not None:
            if priority != TriggerPriority.URGENT:
                return TriggerDecision(
                    priority=TriggerPriority.LOW_PRIORITY,
                    reasons=tuple(reasons + [run[0]]),
                    evidence_ids=tuple(evidence[:100]),
                    allow_speak=False,
                    throttled=True,
                    next_eligible_at=run[1],
                )

        quiet = self._quiet_block(context)
        if quiet is not None and priority != TriggerPriority.URGENT:
            return TriggerDecision(
                priority=TriggerPriority.LOW_PRIORITY,
                reasons=tuple(reasons + ["quiet hours"]),
                evidence_ids=tuple(evidence[:100]),
                allow_speak=False,
                throttled=True,
                next_eligible_at=quiet,
            )

        allow_speak = external_signal
        if reasons and all(reason in _ANALYSIS_ONLY_REASONS for reason in reasons):
            allow_speak = False
        if no_immediate_retry:
            # Reconsider the approach, do not send another reminder six hours
            # after the last one was rejected.
            allow_speak = False
            reasons.append("no immediate retry after an ineffective intervention")
        if quiet is not None:
            # Urgent watched-topic only; the analysis can run, but it must not
            # wake the user outside the cooldown exception.
            allow_speak = False
            reasons.append("quiet hours (urgent analysis only)")
        return TriggerDecision(
            priority=priority,
            reasons=tuple(reasons),
            evidence_ids=tuple(evidence[:100]),
            allow_speak=allow_speak,
        )

    # -- helpers ------------------------------------------------------------

    def _drift_days(self, snapshot: StateSnapshot, context: TriggerContext) -> int:
        days: set[str] = set()
        # Snapshots are written by every cheap cycle (hourly by default), so a
        # "last N snapshots" slice can be shorter than one day. Group by local
        # day instead: the question is "did this move on N distinct days", not
        # "in the last N samples".
        snapshots = list(context.recent_snapshots)
        if not snapshots or snapshots[-1].timestamp != snapshot.timestamp:
            snapshots.append(snapshot)
        cutoff = context.now - timedelta(days=max(1, context.rules.drift_window_days))
        for item in snapshots[-300:]:
            when = parse_iso(item.timestamp)
            if when is None or when < cutoff:
                continue
            if any(
                item.dimension(key).change == CHANGE_CHANGED for key in _DRIFT_DIMENSIONS
            ):
                days.add(local_date(when, context.timezone_name))
        return len(days)

    def _recent_event(
        self, context: TriggerContext, event_type: str, *, hours: int,
    ) -> NavigatorEvent | None:
        for event in reversed(context.recent_events):
            if event.type != event_type:
                continue
            when = parse_iso(event.timestamp)
            if when is not None and (context.now - when) <= timedelta(hours=hours):
                return event
        return None

    @staticmethod
    def _watch_hit(context: TriggerContext) -> str:
        if not context.watch_topics or not context.recent_user_texts:
            return ""
        haystack = "\n".join(context.recent_user_texts[-10:]).casefold()
        for topic in context.watch_topics:
            needle = str(topic or "").strip()
            if needle and needle.casefold() in haystack:
                return needle
        return ""

    def _cooldown_block(self, context: TriggerContext) -> tuple[str, str] | None:
        rules = context.rules
        state = context.state or {}
        last = parse_iso(str(state.get("last_llm_at") or ""))
        if last is not None:
            next_at = last + timedelta(minutes=max(1, rules.min_minutes_between_analyses))
            if context.now < next_at:
                return ("cooldown between analyses", iso(next_at))
        day = local_date(context.now, context.timezone_name)
        runs = state.get("runs_by_day")
        today = int(runs.get(day, 0)) if isinstance(runs, dict) else 0
        if today >= max(1, rules.max_analyses_per_day):
            return ("daily analysis cap reached", iso(self._next_midnight(context)))
        return None

    def _quiet_block(self, context: TriggerContext) -> str | None:
        rules = context.rules
        hour = local_hour(context.now, context.timezone_name)
        start, end = int(rules.quiet_start_hour), int(rules.quiet_end_hour)
        if start == end:
            return None
        if start <= end:
            in_quiet = start <= hour < end
        else:
            in_quiet = hour >= start or hour < end
        if not in_quiet:
            return None
        return iso(self._quiet_end(context, end))

    def _quiet_end(self, context: TriggerContext, end_hour: int) -> datetime:
        hour = local_hour(context.now, context.timezone_name)
        days = 0 if hour < end_hour else 1
        target_date = (context.now + timedelta(days=days)).date()
        return self._local_datetime(context, target_date, time(hour=end_hour))

    def _next_midnight(self, context: TriggerContext) -> datetime:
        target_date = (context.now + timedelta(days=1)).date()
        return self._local_datetime(context, target_date, time.min)

    @staticmethod
    def _local_datetime(context: TriggerContext, date: Any, at: time) -> datetime:
        if not context.timezone_name:
            return datetime.combine(date, at).astimezone()
        try:
            from zoneinfo import ZoneInfo
            return datetime.combine(date, at, tzinfo=ZoneInfo(context.timezone_name))
        except Exception:
            return datetime.combine(date, at).astimezone()


def trigger_rules(config: Any) -> NavigatorRules:
    """Convenience for callers that only have the config object."""
    return NavigatorRules.from_config(config)
