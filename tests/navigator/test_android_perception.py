"""Android perception collector: JSON bridge -> observations, privacy applied."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from jenny.navigator import android_perception as ap
from jenny.navigator.perception import PerceptionManager, PrivacyPolicy
from jenny.navigator.store import NavigatorStore

NOW = datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc)


class FakeBridge:
    def __init__(self):
        self.cleared = 0
        self.capture = None

    def setNotificationCapture(self, enabled):  # noqa: N802
        self.capture = bool(enabled)

    def drainNotifications(self, since_ms, limit):  # noqa: N802
        return json.dumps([{
            "ts": int((NOW - timedelta(minutes=5)).timestamp() * 1000),
            "package": "com.tencent.mm",
            "app": "WeChat",
            "title": "转账",
            "text": "请确认收款 5000 元",
            "category": "msg",
            "ongoing": False,
        }])

    def clearNotifications(self):  # noqa: N802
        self.cleared += 1

    def getUsageEvents(self, since_ms, until_ms):  # noqa: N802
        return json.dumps([
            {"package": "com.example.study", "app": "Study", "minutes": 42.5, "count": 3},
            {"package": "com.tencent.mm", "app": "WeChat", "minutes": 12.0, "count": 7},
        ])

    def getSystemStatus(self):  # noqa: N802
        return json.dumps({
            "battery_percent": 63, "charging": False, "screen_on": True,
            "power_save": False, "network": "wifi", "uptime_s": 12345,
        })

    def hasNotificationAccess(self):  # noqa: N802
        return True

    def hasUsageAccess(self):  # noqa: N802
        return False


def _manager(tmp_path):
    store = NavigatorStore(tmp_path)
    policy = PrivacyPolicy()
    return store, PerceptionManager(
        store, timezone_name="Asia/Shanghai", privacy=policy, now_fn=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_collects_notifications_usage_and_system(tmp_path, monkeypatch):
    store, perception = _manager(tmp_path)
    bridge = FakeBridge()
    monkeypatch.setattr(ap, "get_android_context", lambda: object())

    async def _get(_context):
        return bridge

    monkeypatch.setattr(ap, "_get_bridge", _get)
    config = SimpleNamespace(
        capture_notifications=True, capture_app_usage=True, capture_system=True,
    )

    result = await ap.collect_android_observations(
        perception, config, now=NOW, since=NOW - timedelta(hours=1),
    )

    assert result.notifications == 1
    assert result.app_usage == 2
    assert result.system == 1
    assert not result.errors
    assert bridge.capture is True
    by_kind = {}
    for observation in store.list_observations():
        by_kind.setdefault(observation.kind, []).append(observation)
    assert "notification" in by_kind
    notification = by_kind["notification"][0]
    # Sensitive package: package/app kept, title/text dropped before storage.
    assert notification.value["package"] == "com.tencent.mm"
    assert "title" not in notification.value and "text" not in notification.value
    assert notification.metadata["redacted"] is True
    assert any(
        observation.value.get("minutes") == 42.5
        for observation in by_kind["app_usage"]
    )
    assert by_kind["power"][0].value["battery_percent"] == 63


@pytest.mark.asyncio
async def test_disabled_notification_capture_discards_the_queue(tmp_path, monkeypatch):
    store, perception = _manager(tmp_path)
    bridge = FakeBridge()
    monkeypatch.setattr(ap, "get_android_context", lambda: object())

    async def _get(_context):
        return bridge

    monkeypatch.setattr(ap, "_get_bridge", _get)
    config = SimpleNamespace(
        capture_notifications=False, capture_app_usage=False, capture_system=False,
    )

    result = await ap.collect_android_observations(
        perception, config, now=NOW, since=NOW - timedelta(hours=1),
    )

    assert result.total == 0
    assert bridge.cleared == 1
    assert bridge.capture is False
    assert store.list_observations() == []


@pytest.mark.asyncio
async def test_no_android_context_is_a_clean_skip(tmp_path, monkeypatch):
    store, perception = _manager(tmp_path)
    monkeypatch.setattr(ap, "get_android_context", lambda: None)
    result = await ap.collect_android_observations(
        perception, SimpleNamespace(), now=NOW,
    )
    assert result.skipped is True
    assert result.total == 0
    assert store.list_observations() == []


@pytest.mark.asyncio
async def test_location_reuses_the_existing_bridge_and_is_coarsened(tmp_path, monkeypatch):
    from jenny.runtime import location as location_runtime

    store, perception = _manager(tmp_path)
    monkeypatch.setattr(ap, "get_android_context", lambda: object())

    async def _refresh(_cfg):
        return None

    async def _get(_cfg, *, precise):
        assert precise is False
        return location_runtime.LocationFix(
            latitude=31.230416, longitude=121.473701, accuracy_m=5.0,
            time_ms=int(NOW.timestamp() * 1000), place="Shanghai",
        )

    monkeypatch.setattr(location_runtime, "refresh_current_location", _refresh)
    monkeypatch.setattr(location_runtime, "get_location", _get)

    recorded = await ap.collect_location_observation(
        perception, SimpleNamespace(enable=True), now=NOW,
    )

    assert recorded is True
    observation = store.list_observations()[0]
    assert observation.kind == "location"
    assert observation.value["latitude"] == 31.23
    assert observation.value["longitude"] == 121.47
    # Street-level reverse geocoding is deliberately not stored; rounded
    # coordinates are the coarse signal the Navigator needs.
    assert "place" not in observation.value


@pytest.mark.asyncio
async def test_configure_at_startup_disables_the_listener(tmp_path, monkeypatch):
    bridge = FakeBridge()
    monkeypatch.setattr(ap, "get_android_context", lambda: object())

    async def _get(_context):
        return bridge

    monkeypatch.setattr(ap, "_get_bridge", _get)

    await ap.configure_android_perception(SimpleNamespace(
        enabled=False, capture_notifications=True,
    ))

    assert bridge.capture is False
    assert bridge.cleared == 1


@pytest.mark.asyncio
async def test_access_status_never_opens_settings(tmp_path, monkeypatch):
    bridge = FakeBridge()
    monkeypatch.setattr(ap, "get_android_context", lambda: object())

    async def _get(_context):
        return bridge

    monkeypatch.setattr(ap, "_get_bridge", _get)

    status = await ap.android_access_status()

    assert status == {"notifications": True, "usage": False}
