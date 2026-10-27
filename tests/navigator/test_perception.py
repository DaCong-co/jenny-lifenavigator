"""Perception: local facts, privacy filtering, corrections, inbox."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from jenny.navigator.perception import (
    PerceptionManager,
    PrivacyPolicy,
    is_acknowledgement,
    is_correction,
    is_dismissal,
)
from jenny.navigator.store import NavigatorStore

NOW = datetime(2026, 10, 26, 21, 0, tzinfo=timezone.utc)


def _manager(tmp_path, **privacy):
    store = NavigatorStore(tmp_path)
    policy = PrivacyPolicy(**privacy)
    return store, PerceptionManager(
        store, timezone_name="Asia/Shanghai", privacy=policy,
        now_fn=lambda: NOW,
    )


def test_record_turn_writes_factual_metrics_not_labels(tmp_path):
    store, perception = _manager(tmp_path)
    turn = perception.record_turn(
        session_key="unified:default", user_text="知道了", assistant_text="好的",
        now=NOW,
    )
    assert turn.user_observation_id
    observations = store.list_observations()
    assert [o.kind for o in observations] == ["user_message", "assistant_message"]
    value = observations[0].value
    assert value["chars"] == 3
    assert value["is_short"] is True
    assert value["is_ack"] is True
    assert "resistant" not in json.dumps(value, ensure_ascii=False)


def test_duplicate_turn_within_three_minutes_is_ignored(tmp_path):
    store, perception = _manager(tmp_path)
    first = perception.record_turn(
        session_key="unified:default", user_text="在吗", now=NOW,
    )
    second = perception.record_turn(
        session_key="unified:default", user_text="在吗", now=NOW + timedelta(seconds=30),
    )
    assert first.user_observation_id
    assert second.user_observation_id == ""
    assert len(store.list_observations()) == 1


def test_correction_creates_a_factual_event(tmp_path):
    store, perception = _manager(tmp_path)
    turn = perception.record_turn(
        session_key="unified:default", user_text="你搞错了，我没说过这个", now=NOW,
    )
    assert turn.correction is True
    events = store.list_events(types=("user_correction",))
    assert len(events) == 1
    assert "corrected" in events[0].summary
    assert "hates" not in events[0].summary.casefold()


def test_dismissal_creates_a_backoff_event(tmp_path):
    store, perception = _manager(tmp_path)
    turn = perception.record_turn(
        session_key="unified:default", user_text="我没事，只是累", now=NOW,
    )
    assert turn.dismissal is True
    assert store.list_events(types=("user_dismissal",))[0].summary


def test_sensitive_notification_content_is_redacted(tmp_path):
    store, perception = _manager(tmp_path, redact_sensitive_content=True)
    observation = perception.ingest_record({
        "source": "android", "kind": "notification",
        "timestamp": NOW.isoformat(),
        "value": {
            "package": "com.tencent.mm", "app": "WeChat",
            "title": "转账", "text": "请确认收款 5000 元",
        },
    })
    assert observation is not None
    assert "title" not in observation.value and "text" not in observation.value
    assert observation.value["package"] == "com.tencent.mm"
    assert observation.metadata["redacted"] is True


def test_blocked_package_is_dropped_entirely(tmp_path):
    store, perception = _manager(tmp_path, blocked_packages=("com.example.secret",))
    assert perception.ingest_record({
        "source": "android", "kind": "notification",
        "value": {"package": "com.example.secret", "title": "private"},
    }) is None
    assert store.list_observations() == []


def test_disabled_sources_are_dropped(tmp_path):
    store, perception = _manager(tmp_path, capture_health=False, capture_screen=False)
    assert perception.ingest_record({
        "source": "android", "kind": "sleep",
        "value": {"sleep_minutes": 360},
    }) is None
    assert perception.ingest_record({
        "source": "android", "kind": "screen",
        "value": {"app": "com.example"},
    }) is None
    assert store.list_observations() == []


def test_health_whitelist_keeps_only_used_fields(tmp_path):
    store, perception = _manager(tmp_path, capture_health=True)
    observation = perception.ingest_record({
        "source": "health", "kind": "sleep",
        "value": {"sleep_minutes": 390, "raw_sensor_blob": "x" * 4000},
    })
    assert observation is not None
    assert observation.value == {"sleep_minutes": 390}


def test_location_is_coarsened(tmp_path):
    store, perception = _manager(tmp_path)
    observation = perception.ingest_record({
        "source": "android", "kind": "location",
        "value": {"latitude": 31.230416, "longitude": 121.473701, "accuracy": 5},
    })
    assert observation is not None
    assert observation.value["latitude"] == 31.23
    assert observation.value["longitude"] == 121.47
    assert "accuracy" not in observation.value


def test_inbox_drain_archives_files_and_counts_rejects(tmp_path):
    store, perception = _manager(tmp_path, capture_health=True)
    inbox = store.inbox_dir
    (inbox / "batch.jsonl").write_text(
        json.dumps({
            "source": "android", "kind": "steps",
            "timestamp": NOW.isoformat(), "value": {"steps": 4200},
        }) + "\n"
        + json.dumps({"source": "android", "kind": "weather",
                      "value": {"condition": "sunny"}}) + "\n",
        encoding="utf-8",
    )
    (inbox / "broken.json").write_text("{not json", encoding="utf-8")

    result = perception.drain_inbox()

    assert result.files == 2
    assert result.accepted == 2
    assert result.rejected == 1
    assert not list(inbox.glob("*.json*"))
    assert (store.processed_dir / "batch.jsonl").exists()
    assert (store.failed_dir / "broken.json").exists()
    assert {o.kind for o in store.list_observations()} == {"steps", "weather"}


def test_detectors_are_narrow():
    assert is_acknowledgement("知道了。") is True
    assert is_acknowledgement("我知道这件事很重要") is False
    assert is_correction("你搞错了，我没说过") is True
    assert is_correction("今天有点累") is False
    assert is_dismissal("我没事，只是累") is True
    assert is_dismissal("这个方案没问题") is False
