"""Outcome tracking and the Navigator self-model (including Case F)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jenny.navigator.models import (
    Hypothesis,
    NavigatorDecision,
    Observation,
    RecommendedAction,
    iso,
    make_id,
)
from jenny.navigator.outcome import OutcomeTracker
from jenny.navigator.store import NavigatorStore

NOW = datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc)


def _observation(store, text, when, *, correction=False, dismissal=False):
    store.add_observation(Observation(
        id=make_id("obs", f"{text}:{iso(when)}", when),
        timestamp=iso(when), source="conversation", kind="user_message",
        value={
            "text": text, "chars": len(text),
            "is_ack": text in ("知道了", "好"), "is_short": len(text) <= 8,
            "is_question": text.endswith("?"), "is_correction": correction,
            "is_dismissal": dismissal,
        },
        metadata={"session_key": "unified:default"},
    ))


def _decision():
    return NavigatorDecision(
        id="dec_1", timestamp=iso(NOW - timedelta(hours=1)),
        confidence="medium", need_user_discussion=True,
        recommended_action=RecommendedAction(kind="suggest", description="出去走走"),
    )


def test_no_response_is_unknown_effectiveness(tmp_path):
    store = NavigatorStore(tmp_path)
    tracker = OutcomeTracker(store, now_fn=lambda: NOW)
    tracker.record_intervention(
        _decision(), delivered_text="出去走走吧", action_kind="suggest",
        action_description="出去走走", window_hours=1, now=NOW - timedelta(hours=2),
    )
    evaluated = tracker.evaluate_pending(now=NOW, observations=[], events=[])

    assert evaluated[0].reaction == "no_response"
    assert evaluated[0].effectiveness == "unknown"
    assert evaluated[0].status == "evaluated"


def test_short_ack_is_not_effectiveness(tmp_path):
    store = NavigatorStore(tmp_path)
    tracker = OutcomeTracker(store, now_fn=lambda: NOW)
    tracker.record_intervention(
        _decision(), delivered_text="出去走走吧", action_kind="suggest",
        action_description="出去走走", window_hours=1, now=NOW - timedelta(hours=2),
    )
    _observation(store, "知道了", NOW - timedelta(hours=1))
    evaluated = tracker.evaluate_pending(
        now=NOW, observations=store.list_observations(), events=[],
    )
    assert evaluated[0].reaction == "acknowledged"
    assert evaluated[0].effectiveness == "unknown"


def test_explicit_pushback_is_ineffective(tmp_path):
    store = NavigatorStore(tmp_path)
    tracker = OutcomeTracker(store, now_fn=lambda: NOW)
    tracker.record_intervention(
        _decision(), delivered_text="出去走走吧", action_kind="suggest",
        action_description="出去走走", window_hours=1, now=NOW - timedelta(hours=2),
    )
    _observation(store, "别提醒我了，烦", NOW - timedelta(hours=1))
    evaluated = tracker.evaluate_pending(
        now=NOW, observations=store.list_observations(), events=[],
    )
    assert evaluated[0].reaction == "rejected"
    assert evaluated[0].effectiveness == "ineffective"
    assert store.list_events(types=("intervention_rejected",))


def test_correction_invalidates_hypothesis_and_writes_self_model(tmp_path):
    store = NavigatorStore(tmp_path)
    store.add_hypothesis(Hypothesis(
        id="hyp_1", statement="可能对提醒有抗拒", evidence_ids=("obs_1",),
        confidence="medium", status="observing", created_at=iso(NOW),
    ))
    tracker = OutcomeTracker(store, now_fn=lambda: NOW)
    invalidated = tracker.handle_correction(
        user_text="你搞错了，我没说过这个", observation_id="obs_2", now=NOW,
    )

    assert invalidated == 1
    assert store.list_hypotheses(statuses=("invalidated",))[0].id == "hyp_1"
    note = store.list_self_model_notes()[0]
    assert note.kind == "misjudgement"
    assert "not a fact about the user" in note.statement
    assert "lazy" not in note.statement.casefold()


def test_repeated_rejection_becomes_a_self_model_note(tmp_path):
    store = NavigatorStore(tmp_path)
    tracker = OutcomeTracker(store, now_fn=lambda: NOW)
    for index in range(2):
        when = NOW - timedelta(days=10 - index)
        tracker.record_intervention(
            _decision(), delivered_text="该学习了", action_kind="suggest",
            action_description="学习提醒", window_hours=1, now=when,
        )
        _observation(store, "别说了，烦", when + timedelta(minutes=30))
        tracker.evaluate_pending(
            now=when + timedelta(hours=2),
            observations=store.list_observations(), events=[],
        )

    notes = store.list_self_model_notes()
    assert any(note.kind == "overreminding" for note in notes)
    assert "user" not in notes[0].statement.casefold()[:20]
    assert "lower this kind" in " ".join(note.statement for note in notes).casefold()


def test_dismissal_writes_a_boundary_note_without_diagnosis(tmp_path):
    store = NavigatorStore(tmp_path)
    tracker = OutcomeTracker(store, now_fn=lambda: NOW)
    tracker.handle_dismissal(
        user_text="我没事，只是累", observation_id="obs_9", now=NOW,
    )
    note = store.list_self_model_notes()[0]
    assert note.kind == "user_boundary"
    assert "stay quiet" in note.statement.casefold()
    assert "lazy" not in note.statement.casefold()
