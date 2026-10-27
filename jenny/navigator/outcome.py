"""Outcome tracker and Navigator self-model.

The Navigator must be observed too: what it did, how the user reacted, whether
reality improved, and whether there was a side effect. This module turns that
into evidence and, when the evidence says the Navigator itself misjudged, into a
note about the *Navigator* — never into a fact about the user.

Case F from the plan lives here: when the user explicitly corrects the
Navigator, the current hypotheses are invalidated immediately and the mistake is
recorded as the Navigator's own misjudgement.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Iterable

from jenny.navigator.models import (
    NavigatorDecision,
    NavigatorEvent,
    Observation,
    OutcomeRecord,
    SelfModelNote,
    iso,
    make_id,
    parse_iso,
    utc_now,
)
from jenny.navigator.perception import (
    is_acknowledgement,
    is_correction,
    is_dismissal,
    is_short_reply,
)
from jenny.navigator.store import NavigatorStore

_REJECTION_PATTERNS = (
    "不用了", "不需要", "没必要", "不想听", "不想说", "不想谈", "别提醒", "别再",
    "别说了", "烦死", "很烦", "说了也没用", "别管我", "别烦我",
    "no need", "don't remind", "do not remind", "stop reminding", "not interested",
    "annoying", "leave me alone",
)


class OutcomeTracker:
    """Record interventions, evaluate their window, maintain the self-model."""

    def __init__(self, store: NavigatorStore, *, now_fn: Any = None) -> None:
        self.store = store
        self._now_fn = now_fn or utc_now

    # -- recording ----------------------------------------------------------

    def record_intervention(
        self,
        decision: NavigatorDecision,
        *,
        delivered_text: str,
        action_kind: str,
        action_description: str,
        window_hours: int = 48,
        now: datetime | None = None,
    ) -> OutcomeRecord:
        when = now or self._now_fn()
        intervention_id = make_id("int", f"{decision.id}:{delivered_text[:200]}", when)
        record = OutcomeRecord(
            id=make_id("out", intervention_id, when),
            intervention_id=intervention_id,
            decision_id=decision.id,
            action_kind=action_kind,
            action_description=action_description[:500],
            delivered_text=delivered_text[:1500],
            created_at=iso(when),
            window_end=iso(when + timedelta(hours=max(1, window_hours))),
            status="pending",
        )
        self.store.add_outcome(record)
        return record

    def handle_correction(
        self,
        *,
        user_text: str,
        observation_id: str = "",
        event: NavigatorEvent | None = None,
        now: datetime | None = None,
    ) -> int:
        """Invalidate current judgement and record the Navigator's own mistake.

        Returns the number of hypotheses invalidated. Nothing is written about
        the user: the note says the Navigator misread the situation.
        """
        when = now or self._now_fn()
        reason = "explicit user correction"
        invalidated = self.store.invalidate_hypotheses(reason=reason)
        evidence = tuple(
            item for item in (observation_id, event.id if event else "") if item
        )
        note = SelfModelNote(
            id=make_id("smn", f"misjudgement:{user_text[:200]}:{iso(when)}", when),
            created_at=iso(when),
            kind="misjudgement",
            statement=(
                "The user explicitly corrected the Navigator: "
                f"\"{user_text[:200]}\". The previous inference was the Navigator's "
                "error, not a fact about the user. Do not restate it."
            ),
            evidence_ids=evidence,
            confidence="high",
        )
        self.store.add_self_model_note(note)
        self._close_latest_pending_as(
            reaction="corrected",
            effectiveness="side_effect",
            note="The user corrected the Navigator during the outcome window.",
            evidence=evidence,
            now=when,
        )
        return invalidated

    def handle_dismissal(
        self,
        *,
        user_text: str,
        observation_id: str = "",
        event: NavigatorEvent | None = None,
        now: datetime | None = None,
    ) -> None:
        """Record a boundary without treating it as a user fact."""
        when = now or self._now_fn()
        evidence = tuple(
            item for item in (observation_id, event.id if event else "") if item
        )
        note = SelfModelNote(
            id=make_id("smn", f"boundary:{user_text[:200]}:{iso(when)}", when),
            created_at=iso(when),
            kind="user_boundary",
            statement=(
                "The user asked the Navigator to back off / said they are fine: "
                f"\"{user_text[:200]}\". Stay quiet until there is materially new "
                "evidence; do not repeat this line of intervention."
            ),
            evidence_ids=evidence,
            confidence="high",
        )
        self.store.add_self_model_note(note)

    # -- evaluation ---------------------------------------------------------

    def evaluate_pending(
        self,
        *,
        now: datetime | None = None,
        observations: Iterable[Observation] | None = None,
        events: Iterable[NavigatorEvent] | None = None,
    ) -> list[OutcomeRecord]:
        """Evaluate every outcome whose window has elapsed (or that was corrected)."""
        when = now or self._now_fn()
        observation_list = list(observations) if observations is not None else (
            self.store.list_observations(limit=2000)
        )
        event_list = list(events) if events is not None else self.store.list_events(limit=500)
        evaluated: list[OutcomeRecord] = []
        for outcome in self.store.pending_outcomes():
            created = parse_iso(outcome.created_at)
            window_end = parse_iso(outcome.window_end)
            if created is None or window_end is None:
                continue
            if when < window_end:
                continue
            reaction, evidence, note = self._classify_reaction(
                outcome, created, window_end, observation_list, event_list,
            )
            effectiveness = self._effectiveness(reaction)
            updated = replace(
                outcome,
                status="evaluated",
                reaction=reaction,
                effectiveness=effectiveness,
                evidence_ids=tuple(dict.fromkeys([*outcome.evidence_ids, *evidence]))[:20],
                note=note,
                evaluated_at=iso(when),
            )
            self.store.update_outcome(updated)
            evaluated.append(updated)
            if (
                updated.reaction in ("rejected", "corrected")
                or updated.effectiveness in ("ineffective", "side_effect")
            ):
                self.store.add_event(NavigatorEvent(
                    id=make_id("evt", f"intervention_rejected:{updated.id}", when),
                    timestamp=iso(when),
                    type="intervention_rejected",
                    summary=(
                        f"Navigator intervention {updated.id} was "
                        f"{updated.reaction} ({updated.effectiveness}) in its outcome window."
                    ),
                    source_ids=(updated.id,),
                ))
            self._maybe_add_repetition_note(updated, when)
        return evaluated

    def _classify_reaction(
        self,
        outcome: OutcomeRecord,
        created: datetime,
        window_end: datetime,
        observations: list[Observation],
        events: list[NavigatorEvent],
    ) -> tuple[str, tuple[str, ...], str]:
        messages = [
            observation for observation in observations
            if observation.kind == "user_message"
            and (when := parse_iso(observation.timestamp)) is not None
            and created < when <= window_end
        ]
        correction_events = [
            event for event in events
            if event.type == "user_correction"
            and (when := parse_iso(event.timestamp)) is not None
            and created < when <= window_end
        ]
        if correction_events:
            return (
                "corrected",
                tuple(event.id for event in correction_events[:3]),
                "The user explicitly corrected the Navigator after this intervention.",
            )
        if not messages:
            return "no_response", (), "No user message arrived inside the outcome window."

        texts = [str(message.value.get("text", "")) for message in messages]
        if any(is_correction(text) or bool(message.value.get("is_correction"))
               for text, message in zip(texts, messages)):
            return (
                "corrected",
                tuple(message.id for message in messages[-3:]),
                "A user message corrected the Navigator inside the outcome window.",
            )
        if any(
            is_dismissal(text) or bool(message.value.get("is_dismissal"))
            for text, message in zip(texts, messages)
        ) or any(
            any(pattern in text.lower() for pattern in _REJECTION_PATTERNS)
            for text in texts
        ):
            return (
                "rejected",
                tuple(message.id for message in messages[-3:]),
                f"The user pushed back in {len(messages)} message(s) inside the window.",
            )
        engaged = any(
            int(message.value.get("chars", 0) or 0) > 15
            or bool(message.value.get("is_question"))
            for message in messages
        )
        if engaged:
            return (
                "engaged",
                tuple(message.id for message in messages[-3:]),
                f"The user engaged with the intervention in {len(messages)} message(s).",
            )
        if all(
            bool(message.value.get("is_ack"))
            or bool(message.value.get("is_short"))
            or is_acknowledgement(text)
            or is_short_reply(text)
            for text, message in zip(texts, messages)
        ):
            return (
                "acknowledged",
                tuple(message.id for message in messages[-3:]),
                f"{len(messages)} short/acknowledgement repl(ies); no visible change.",
            )
        return (
            "acknowledged",
            tuple(message.id for message in messages[-3:]),
            f"{len(messages)} reply/replies inside the window.",
        )

    @staticmethod
    def _effectiveness(reaction: str) -> str:
        if reaction == "corrected":
            return "side_effect"
        if reaction == "rejected":
            return "ineffective"
        # Engagement is not improvement; acknowledging is not compliance.
        return "unknown"

    def _close_latest_pending_as(
        self,
        *,
        reaction: str,
        effectiveness: str,
        note: str,
        evidence: tuple[str, ...],
        now: datetime,
    ) -> None:
        pending = self.store.pending_outcomes()
        if not pending:
            return
        latest = pending[-1]
        created = parse_iso(latest.created_at)
        if created is not None and (now - created) > timedelta(days=7):
            return
        self.store.update_outcome(replace(
            latest,
            status="evaluated",
            reaction=reaction,
            effectiveness=effectiveness,
            evidence_ids=tuple(dict.fromkeys([*latest.evidence_ids, *evidence]))[:20],
            note=note,
            evaluated_at=iso(now),
        ))

    def _maybe_add_repetition_note(
        self, outcome: OutcomeRecord, now: datetime,
    ) -> None:
        """Two bad outcomes of the same kind become one self-model note.

        This is the "do not interpret rejection as a user problem" rule turned
        into a mechanism: the note is about the Navigator's approach and lowers
        its weight in the next Core prompt.
        """
        if outcome.reaction not in ("rejected", "corrected") and outcome.effectiveness not in (
            "ineffective", "side_effect",
        ):
            return
        cutoff = now - timedelta(days=30)
        bad = [
            item for item in self.store.list_outcomes(limit=200)
            if item.action_kind == outcome.action_kind
            and item.status == "evaluated"
            and (
                item.reaction in ("rejected", "corrected")
                or item.effectiveness in ("ineffective", "side_effect")
            )
            and (when := parse_iso(item.created_at)) is not None
            and when >= cutoff
        ]
        if len(bad) < 2:
            return
        statement = (
            f"Interventions of kind '{outcome.action_kind}' were rejected or "
            f"ineffective {len(bad)} times in the last 30 days. Lower this kind "
            "of suggestion and change the approach instead of repeating it."
        )
        existing = {
            note.statement for note in self.store.list_self_model_notes(active_only=True)
        }
        if statement in existing:
            return
        self.store.add_self_model_note(SelfModelNote(
            id=make_id("smn", statement, now),
            created_at=iso(now),
            kind="overreminding",
            statement=statement,
            evidence_ids=tuple(item.id for item in bad[:5]),
            confidence="medium",
        ))

    # -- reads --------------------------------------------------------------

    def last_outcome(self) -> OutcomeRecord | None:
        return self.store.last_outcome()

    def recent_outcomes(self, limit: int = 10) -> list[OutcomeRecord]:
        return self.store.list_outcomes(limit=limit)
