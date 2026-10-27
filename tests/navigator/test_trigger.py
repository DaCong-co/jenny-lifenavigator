"""Trigger engine: cheap local filter, scheduling priorities, no diagnosis."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jenny.navigator.models import (
    DIMENSION_KEYS,
    CHANGE_STABLE,
    CHANGE_CHANGED,
    Dimension,
    NavigatorEvent,
    OutcomeRecord,
    StateSnapshot,
    TriggerPriority,
    iso,
)
from jenny.navigator.rules import NavigatorRules
from jenny.navigator.trigger import TriggerContext, TriggerEngine

NOW = datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc)  # 22:00 Shanghai


def _snapshot(*, changed=(), when=NOW, facts_by_key=None):
    facts_by_key = facts_by_key or {}
    dimensions = {}
    for key in DIMENSION_KEYS:
        facts = tuple(facts_by_key.get(key, (f"{key} stable",)))
        dimensions[key] = Dimension(
            key, facts=facts,
            evidence_ids=(f"obs_{key}",),
            change=CHANGE_CHANGED if key in changed else CHANGE_STABLE,
        )
    return StateSnapshot(
        timestamp=iso(when), window_start=iso(when - timedelta(days=7)),
        window_end=iso(when), dimensions=dimensions,
        metrics={"user_messages": 4, "goal_like_events": 0},
        evidence_ids=tuple(f"obs_{key}" for key in DIMENSION_KEYS),
    )


def _context(**kwargs):
    return TriggerContext(
        now=kwargs.pop("now", NOW),
        rules=kwargs.pop("rules", NavigatorRules(quiet_start_hour=0, quiet_end_hour=0)),
        timezone_name="Asia/Shanghai",
        **kwargs,
    )


def test_case_a_stable_day_is_no_trigger():
    decision = TriggerEngine().evaluate(_snapshot(), _context())
    assert decision.priority is TriggerPriority.NO_TRIGGER
    assert decision.allow_speak is False


def test_multi_domain_change_is_normal():
    decision = TriggerEngine().evaluate(
        _snapshot(changed=("emotional_state", "behavioral_state")), _context(),
    )
    assert decision.priority is TriggerPriority.NORMAL
    assert decision.allow_speak is True
    assert any("multi-domain" in reason for reason in decision.reasons)


def test_sustained_drift_is_important():
    earlier = _snapshot(changed=("emotional_state",), when=NOW - timedelta(days=2))
    yesterday = _snapshot(changed=("behavioral_state",), when=NOW - timedelta(days=1))
    current = _snapshot(changed=("emotional_state",))
    decision = TriggerEngine().evaluate(
        current, _context(recent_snapshots=(earlier, yesterday, current)),
    )
    assert decision.priority is TriggerPriority.IMPORTANT
    assert any("sustained drift" in reason for reason in decision.reasons)


def test_drift_counts_days_not_samples():
    """Hourly snapshots must not hide a drift that spans two days.

    This is the failure a "last N snapshots" slice produced: ten hourly samples
    are ten hours, so a two-day drift was invisible.
    """
    samples = []
    for hours_ago in range(48, -1, -1):
        when = NOW - timedelta(hours=hours_ago)
        changed = ("emotional_state",) if hours_ago in (24, 1) else ()
        samples.append(_snapshot(changed=changed, when=when))
    decision = TriggerEngine().evaluate(
        samples[-1], _context(recent_snapshots=tuple(samples)),
    )
    assert decision.priority is TriggerPriority.IMPORTANT
    assert any("sustained drift" in reason for reason in decision.reasons)


def test_case_d_ineffective_previous_intervention_is_important():
    last = OutcomeRecord(
        id="out_1", intervention_id="int_1", decision_id="dec_1",
        action_kind="suggest", action_description="study reminder",
        delivered_text="该学习了", created_at=iso(NOW - timedelta(hours=6)),
        window_end=iso(NOW), status="evaluated", reaction="rejected",
        effectiveness="ineffective",
    )
    decision = TriggerEngine().evaluate(
        _snapshot(), _context(last_outcome=last),
    )
    assert decision.priority is TriggerPriority.IMPORTANT
    assert any("did not help" in reason for reason in decision.reasons)
    assert decision.allow_speak is False


def test_case_g_dismissal_downgrades_to_low():
    event = NavigatorEvent(
        id="evt_1", timestamp=iso(NOW - timedelta(hours=2)),
        type="user_dismissal", summary="I am fine",
    )
    decision = TriggerEngine().evaluate(
        _snapshot(changed=("emotional_state", "behavioral_state")),
        _context(recent_events=(event,)),
    )
    assert decision.priority in (TriggerPriority.LOW_PRIORITY, TriggerPriority.NO_TRIGGER)
    assert decision.allow_speak is False


def test_quiet_hours_defer_speech_and_analysis():
    night = NOW.replace(hour=15)  # 23:00 Shanghai
    rules = NavigatorRules(quiet_start_hour=23, quiet_end_hour=7)
    decision = TriggerEngine().evaluate(
        _snapshot(changed=("emotional_state", "behavioral_state"), when=night),
        _context(now=night, rules=rules),
    )
    assert decision.priority is TriggerPriority.LOW_PRIORITY
    assert decision.throttled is True
    assert decision.next_eligible_at


def test_cooldown_blocks_a_second_analysis():
    state = {"last_llm_at": iso(NOW - timedelta(minutes=30))}
    decision = TriggerEngine().evaluate(
        _snapshot(changed=("emotional_state", "behavioral_state")),
        _context(state=state),
    )
    assert decision.priority is TriggerPriority.LOW_PRIORITY
    assert decision.throttled is True
    assert decision.next_eligible_at


def test_daily_cap_blocks_unless_urgent():
    state = {
        "last_llm_at": iso(NOW - timedelta(hours=8)),
        "runs_by_day": {"2026-10-26": 3},
    }
    decision = TriggerEngine().evaluate(
        _snapshot(changed=("emotional_state", "behavioral_state")),
        _context(state=state),
    )
    assert decision.priority is TriggerPriority.LOW_PRIORITY
    assert decision.throttled is True


def test_dismissal_suppresses_even_a_watched_topic_without_new_evidence():
    dismissal = NavigatorEvent(
        id="evt_1", timestamp=iso(NOW - timedelta(hours=2)),
        type="user_dismissal", summary="I am fine",
    )
    decision = TriggerEngine().evaluate(
        _snapshot(),
        _context(
            recent_events=(dismissal,),
            recent_user_texts=("专升本的事以后再说",),
            watch_topics=("专升本",),
        ),
    )
    assert decision.priority is TriggerPriority.LOW_PRIORITY
    assert decision.allow_speak is False


def test_watched_topic_with_event_is_urgent():
    event = NavigatorEvent(
        id="evt_1", timestamp=iso(NOW - timedelta(hours=1)),
        type="milestone", summary="A goal moved",
    )
    decision = TriggerEngine().evaluate(
        _snapshot(),
        _context(
            recent_events=(event,),
            recent_user_texts=("专升本的事怎么样了",),
            watch_topics=("专升本",),
        ),
    )
    assert decision.priority is TriggerPriority.URGENT
    assert decision.allow_speak is True
