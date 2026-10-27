"""Aggregator: seven dimensions, facts with time ranges, no score."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jenny.life_model.store import LifeModelStore
from jenny.navigator.aggregator import StateAggregator
from jenny.navigator.models import DIMENSION_KEYS, Observation, iso, make_id
from jenny.navigator.store import NavigatorStore

NOW = datetime(2026, 10, 26, 21, 0, tzinfo=timezone.utc)


def _add(store: NavigatorStore, kind: str, value: dict, when: datetime, **meta):
    store.add_observation(Observation(
        id=make_id("obs", f"{kind}:{value}:{iso(when)}", when),
        timestamp=iso(when), source="test", kind=kind, value=value, metadata=meta,
    ))


def _message(store, text, when, *, ack=False, short=False, correction=False):
    _add(store, "user_message", {
        "text": text, "chars": len(text), "is_ack": ack, "is_short": short,
        "is_question": text.endswith("?"), "is_correction": correction,
        "is_dismissal": False, "local_hour": 21, "local_date": when.date().isoformat(),
    }, when)


def _aggregator(store, workspace):
    return StateAggregator(
        store, workspace=workspace, life_model=LifeModelStore(workspace),
        timezone_name="Asia/Shanghai", now_fn=lambda: NOW,
    )


def test_empty_workspace_still_has_all_seven_dimensions(tmp_path):
    store = NavigatorStore(tmp_path)
    snapshot = _aggregator(store, tmp_path).build(now=NOW)

    assert set(snapshot.dimensions) == set(DIMENSION_KEYS)
    assert snapshot.dimension("body_state").change == "unknown"
    assert "no body data" in snapshot.dimension("body_state").facts[0]
    assert "score" not in snapshot.metrics
    assert snapshot.metrics["user_messages"] == 0


def test_emotional_state_counts_short_acknowledgements_as_facts(tmp_path):
    store = NavigatorStore(tmp_path)
    for day in range(4):
        _message(store, "知道了", NOW - timedelta(days=day), ack=True, short=True)
    _message(store, "最近作业很多?", NOW - timedelta(hours=1))

    snapshot = _aggregator(store, tmp_path).build(now=NOW)
    dimension = snapshot.dimension("emotional_state")

    assert dimension.change in ("changed", "stable", "unknown")
    joined = " ".join(dimension.facts)
    assert "acknowledgement-only" in joined
    assert "4" in joined
    assert "resistant" not in joined.casefold()
    assert dimension.evidence_ids


def test_body_state_reads_sleep_records(tmp_path):
    store = NavigatorStore(tmp_path)
    _add(store, "sleep", {"sleep_minutes": 360}, NOW - timedelta(days=1))
    _add(store, "sleep", {"sleep_minutes": 300}, NOW - timedelta(days=2))

    snapshot = _aggregator(store, tmp_path).build(now=NOW)
    dimension = snapshot.dimension("body_state")

    assert "2 sleep record(s)" in " ".join(dimension.facts)
    assert "5.5h" in " ".join(dimension.facts)
    assert dimension.change in ("stable", "changed")


def test_repeated_message_is_reported_as_a_fact(tmp_path):
    store = NavigatorStore(tmp_path)
    for day in range(3):
        _message(store, "我要不要退学", NOW - timedelta(days=day))

    snapshot = _aggregator(store, tmp_path).build(now=NOW)
    joined = " ".join(snapshot.dimension("cognitive_state").facts)

    assert "same message was sent 3 times" in joined
    assert "wants to drop out" not in joined.casefold()


def test_development_state_reads_the_life_model_without_quoting_it(tmp_path):
    life = LifeModelStore(tmp_path)
    life.update_current_state("学习状态: 备考中\n压力: 偏高")
    # Write the event with a fixed timestamp instead of ``write_event`` (which
    # uses the wall clock): the test must stay inside its frozen NOW window.
    (life.events_dir / "20261026-140000-abcd1234.md").write_text(
        "---\n"
        "id: abcd1234\n"
        "timestamp: 20261026-140000\n"
        "kind: milestone\n"
        "source_turn: test\n"
        "---\n\n"
        "用户完成了一次阶段性目标\n",
        encoding="utf-8",
    )

    store = NavigatorStore(tmp_path)
    snapshot = _aggregator(store, tmp_path).build(now=NOW)
    dimension = snapshot.dimension("development_state")
    joined = " ".join(dimension.facts)

    assert "life_goals.md is still empty" in joined
    assert "decision/goal" not in joined  # that belongs to cognitive_state
    assert "压力" not in joined
    assert dimension.evidence_ids


def test_navigator_effectiveness_marks_rejection(tmp_path):
    from jenny.navigator.models import OutcomeRecord

    store = NavigatorStore(tmp_path)
    store.add_outcome(OutcomeRecord(
        id="out_1", intervention_id="int_1", decision_id="dec_1",
        action_kind="suggest", action_description="go for a walk",
        delivered_text="出去走走吧", created_at=iso(NOW - timedelta(days=1)),
        window_end=iso(NOW), status="evaluated", reaction="rejected",
        effectiveness="ineffective",
    ))
    snapshot = _aggregator(store, tmp_path).build(now=NOW)
    dimension = snapshot.dimension("navigator_effectiveness")

    assert dimension.change == "changed"
    assert "rejected 1" in " ".join(dimension.facts)
