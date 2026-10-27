"""Store and model round-trips: JSONL stays a plain, append-only record."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jenny.navigator.models import (
    DIMENSION_KEYS,
    Dimension,
    Hypothesis,
    NavigatorDecision,
    NavigatorEvent,
    Observation,
    OutcomeRecord,
    RecommendedAction,
    SelfModelNote,
    StateSnapshot,
    TriggerDecision,
    TriggerPriority,
    iso,
    make_id,
)
from jenny.navigator.rules import NavigatorRules, RuleBook
from jenny.navigator.store import NavigatorStore


def _observation(kind: str, text: str, when: datetime) -> Observation:
    return Observation(
        id=make_id("obs", f"{kind}:{text}", when),
        timestamp=iso(when),
        source="test",
        kind=kind,
        value={"text": text},
        metadata={"session_key": "unified:default"},
    )


def test_observations_and_events_round_trip(tmp_path):
    store = NavigatorStore(tmp_path)
    now = datetime.now(timezone.utc)
    observation = _observation("user_message", "今天有点累", now)
    store.add_observation(observation)
    event = NavigatorEvent(
        id=make_id("evt", "dismissal", now),
        timestamp=iso(now),
        type="user_dismissal",
        summary="The user said they are fine",
        source_ids=(observation.id,),
    )
    store.add_event(event)

    assert [o.id for o in store.list_observations()] == [observation.id]
    assert store.list_events()[0].source_ids == (observation.id,)
    assert store.last_observation_matching(kind="user_message").id == observation.id


def test_snapshot_has_seven_dimensions_and_survives_json(tmp_path):
    store = NavigatorStore(tmp_path)
    now = datetime.now(timezone.utc)
    snapshot = StateSnapshot(
        timestamp=iso(now),
        window_start=iso(now - timedelta(days=7)),
        window_end=iso(now),
        dimensions={
            key: Dimension(key, facts=(f"{key} fact",), change="stable")
            for key in DIMENSION_KEYS
        },
        metrics={"user_messages": 1},
    )
    store.add_snapshot(snapshot)

    loaded = store.latest_snapshot()
    assert loaded is not None
    assert set(loaded.dimensions) == set(DIMENSION_KEYS)
    assert loaded.metrics["user_messages"] == 1
    assert loaded.dimension("body_state").facts == ("body_state fact",)
    assert "score" not in loaded.to_dict()


def test_hypothesis_status_updates_are_append_only_and_deduped(tmp_path):
    store = NavigatorStore(tmp_path)
    now = datetime.now(timezone.utc)
    hypothesis = Hypothesis(
        id="hyp_1", statement="可能正在积累恢复不足",
        evidence_ids=("obs_1", "obs_2"), confidence="medium",
        created_at=iso(now), updated_at=iso(now),
    )
    store.add_hypothesis(hypothesis)
    assert store.update_hypothesis_status("hyp_1", "supported") is True

    loaded = store.list_hypotheses()
    assert len(loaded) == 1
    assert loaded[0].status == "supported"
    # The history is not rewritten; only the last row wins.
    assert len(store.hypotheses_file.read_text("utf-8").splitlines()) == 2


def test_outcome_updates_are_deduped(tmp_path):
    store = NavigatorStore(tmp_path)
    now = datetime.now(timezone.utc)
    outcome = OutcomeRecord(
        id="out_1", intervention_id="int_1", decision_id="dec_1",
        action_kind="suggest", action_description="go for a walk",
        delivered_text="要不要出去走走？", created_at=iso(now),
        window_end=iso(now + timedelta(hours=48)),
    )
    store.add_outcome(outcome)
    store.update_outcome(OutcomeRecord.from_dict({
        **outcome.to_dict(),
        "status": "evaluated",
        "reaction": "acknowledged",
    }))

    loaded = store.list_outcomes()
    assert len(loaded) == 1
    assert loaded[0].status == "evaluated"
    assert store.pending_outcomes() == []


def test_self_model_notes_and_rule_versions(tmp_path):
    store = NavigatorStore(tmp_path)
    now = datetime.now(timezone.utc)
    note = SelfModelNote(
        id="smn_1", created_at=iso(now), kind="overreminding",
        statement="Reminders about study were rejected twice",
    )
    store.add_self_model_note(note)
    assert store.list_self_model_notes()[0].statement.startswith("Reminders")
    assert store.deactivate_self_model_note("smn_1") is True
    assert store.list_self_model_notes() == []

    book = RuleBook(store)
    defaults = NavigatorRules()
    changed = NavigatorRules(min_minutes_between_analyses=240)
    version = book.record_change(
        defaults=defaults, new_rules=changed, reason="too many reminders",
        evidence=("out_1", "out_2"), discussion_result="user agreed",
    )
    assert version.version == 1
    assert book.effective(defaults).min_minutes_between_analyses == 240
    assert book.window(defaults).current_version.version == 1


def test_trigger_state_and_decisions_are_persisted(tmp_path):
    store = NavigatorStore(tmp_path)
    store.write_trigger_state({"last_llm_at": "2026-10-26T12:00:00Z"})
    assert store.read_trigger_state()["last_llm_at"] == "2026-10-26T12:00:00Z"

    decision = NavigatorDecision(
        id="dec_1", timestamp="2026-10-26T12:00:00Z",
        recommended_action=RecommendedAction(kind="observe"),
    )
    store.add_decision(decision)
    assert store.list_decisions()[0].id == "dec_1"

    store.log_trigger(TriggerDecision(
        priority=TriggerPriority.LOW_PRIORITY, reasons=("user_active",),
    ))
    assert store.trigger_log_file.read_text("utf-8").strip()


def test_environment_model_is_read_only_for_the_store(tmp_path):
    store = NavigatorStore(tmp_path)
    store.environment_file.write_text("# Environment\n- Official fact\n", encoding="utf-8")
    assert store.read_environment_model().startswith("# Environment")
