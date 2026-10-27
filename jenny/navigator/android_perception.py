"""Android perception collectors for the Life Navigator (P0 + basic system).

The native side lives in ``PerceptionBridge.kt``; this module decides *what* to
ask for, converts the JSON into observations and lets
:class:`~jenny.navigator.perception.PerceptionManager` apply the single privacy
gate. Off Android (desktop, tests) every entry point degrades to a no-op.

Data collected here:

* notifications — the user must grant "notification access"; buffered by the
  listener, drained on each Navigator cycle, never interpreted locally;
* app usage — the user must grant "usage access"; foreground minutes are
  computed from raw usage events for the cycle window, not from cumulative
  daily buckets;
* system status — battery, charging, screen on, power save, network, uptime.

Sleep / heart rate / workouts are **not** collected here: they need Health
Connect (or a device-specific sensor path) and a runtime permission flow. The
``navigator/inbox/`` JSON interface already accepts them when such a collector
exists, and the privacy filter whitelists the fields.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from jenny.navigator.models import iso
from jenny.navigator.perception import PerceptionManager
from jenny.runtime.chaquopy_bridge import BridgeCache
from jenny.runtime.context import get_android_context

_BRIDGE = BridgeCache("com.flagdizero.jenny.PerceptionBridge")

_BRIDGE_TIMEOUT_S = 15.0
_ACCESS_TIMEOUT_S = 3.0
_MAX_NOTIFICATIONS = 300


def reset_android_perception_state() -> None:
    """Drop the cached PerceptionBridge at a gateway restart (loop-bound lock)."""
    _BRIDGE.reset()


def _resolve_bridge_class() -> Any:
    """Seam used by tests to replace the Kotlin class."""
    return _BRIDGE.resolve_class()


async def _get_bridge(context: Any) -> Any:
    return await _BRIDGE.get(context, resolve=_resolve_bridge_class)


@dataclass(slots=True)
class AndroidPerceptionResult:
    notifications: int = 0
    app_usage: int = 0
    system: int = 0
    errors: list[str] = field(default_factory=list)
    skipped: bool = False

    @property
    def total(self) -> int:
        return self.notifications + self.app_usage + self.system

    def summary(self) -> str:
        if self.skipped:
            return "skipped (no Android context)"
        parts = [
            f"notifications={self.notifications}",
            f"app_usage={self.app_usage}",
            f"system={self.system}",
        ]
        if self.errors:
            parts.append(f"errors={len(self.errors)}")
        return ", ".join(parts)


async def _call(bridge: Any, method: str, *args: Any) -> Any:
    """Run one blocking bridge method off the event loop, with a ceiling."""
    return await asyncio.wait_for(
        asyncio.to_thread(getattr(bridge, method), *args),
        timeout=_BRIDGE_TIMEOUT_S,
    )


def _json_list(raw: Any) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _json_dict(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _ms_to_iso(milliseconds: Any) -> str | None:
    try:
        value = int(milliseconds)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    try:
        return iso(datetime.fromtimestamp(value / 1000.0, tz=timezone.utc))
    except (OverflowError, OSError, ValueError):
        return None


def _record(perception: PerceptionManager, payload: dict[str, Any]) -> bool:
    observation = perception.ingest_record(payload)
    if observation is None:
        return False
    perception.store.add_observation(observation)
    return True


async def collect_android_observations(
    perception: PerceptionManager,
    config: Any,
    *,
    now: datetime,
    since: datetime | None = None,
) -> AndroidPerceptionResult:
    """Collect one cycle of Android signals into the Navigator store."""
    context = get_android_context()
    if context is None:
        return AndroidPerceptionResult(skipped=True)

    result = AndroidPerceptionResult()
    try:
        bridge = await _get_bridge(context)
    except Exception as exc:  # noqa: BLE001 - native bridge may not exist off-device
        result.errors.append(f"bridge: {type(exc).__name__}: {exc}")
        logger.opt(exception=True).debug("PerceptionBridge unavailable")
        return result

    since = since or now
    since_ms = int(since.timestamp() * 1000)
    now_ms = int(now.timestamp() * 1000)

    if bool(getattr(config, "capture_notifications", True)):
        try:
            # The listener defaults to off; the first enabled cycle turns it on.
            await _call(bridge, "setNotificationCapture", True)
            # The queue is drained every cycle, so there is no need to filter by
            # a time window here; using 0 keeps a notification captured just
            # before an offline period instead of dropping it silently.
            raw = await _call(
                bridge, "drainNotifications", 0, _MAX_NOTIFICATIONS,
            )
            for item in _json_list(raw):
                timestamp = _ms_to_iso(item.get("ts"))
                if timestamp is None:
                    continue
                payload = {
                    "source": "android",
                    "kind": "notification",
                    "timestamp": timestamp,
                    "value": {
                        "package": item.get("package", ""),
                        "app": item.get("app", ""),
                        "title": item.get("title", ""),
                        "text": item.get("text", ""),
                        "category": item.get("category", ""),
                        "ongoing": bool(item.get("ongoing", False)),
                    },
                }
                if _record(perception, payload):
                    result.notifications += 1
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"notifications: {type(exc).__name__}")
            logger.opt(exception=True).debug("Navigator: notification collection failed")
    else:
        # Stop the listener too, not just this cycle's queue: otherwise it would
        # keep writing while capture is off and only be cleared on the next run.
        try:
            await _call(bridge, "setNotificationCapture", False)
            await _call(bridge, "clearNotifications")
        except Exception:
            pass

    if bool(getattr(config, "capture_app_usage", True)):
        try:
            raw = await _call(bridge, "getUsageEvents", since_ms, now_ms)
            for item in _json_list(raw):
                package = str(item.get("package") or "")
                if not package:
                    continue
                payload = {
                    "source": "android",
                    "kind": "app_usage",
                    "timestamp": iso(now),
                    "value": {
                        "package": package,
                        "app": item.get("app", ""),
                        "minutes": item.get("minutes", 0),
                        "count": item.get("count", 0),
                    },
                }
                if _record(perception, payload):
                    result.app_usage += 1
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"app_usage: {type(exc).__name__}")
            logger.opt(exception=True).debug("Navigator: app usage collection failed")

    if bool(getattr(config, "capture_system", True)):
        try:
            raw = await _call(bridge, "getSystemStatus")
            status = _json_dict(raw)
            if status:
                payload = {
                    "source": "android",
                    "kind": "power",
                    "timestamp": iso(now),
                    "value": status,
                }
                if _record(perception, payload):
                    result.system += 1
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"system: {type(exc).__name__}")
            logger.opt(exception=True).debug("Navigator: system status collection failed")

    return result


async def configure_android_perception(config: Any) -> None:
    """Apply the notification-capture switch at gateway start.

    Without this, a phone whose Navigator is disabled would keep the listener's
    previous state until a cycle runs — and no cycle runs when it is disabled.
    Best-effort: a missing bridge or revoked access is not an error.
    """
    context = get_android_context()
    if context is None:
        return
    enabled = bool(getattr(config, "enabled", True)) and bool(
        getattr(config, "capture_notifications", True)
    )
    try:
        bridge = await _get_bridge(context)
        await _call(bridge, "setNotificationCapture", enabled)
        if not enabled:
            await _call(bridge, "clearNotifications")
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).debug("Navigator: could not configure perception")


async def open_android_access_settings(kind: str) -> bool:
    """Open the system screen for notification or usage access.

    Returns False off Android or when the OEM has no such screen. This is the
    only side-effecting function in the module; ``/navigator`` uses it only when
    the user explicitly types ``/navigator notifications`` or ``/navigator usage``.
    """
    context = get_android_context()
    if context is None:
        return False
    try:
        bridge = await _get_bridge(context)
        method = (
            "openNotificationAccessSettings"
            if kind == "notifications"
            else "openUsageAccessSettings"
        )
        return bool(await _call(bridge, method))
    except Exception:  # noqa: BLE001
        return False


async def android_access_status() -> dict[str, bool] | None:
    """Notification/usage access state, or ``None`` when not on Android.

    Read-only: it never opens a settings screen. ``/navigator`` uses it to tell
    the user why no app/notification observations exist yet.
    """
    context = get_android_context()
    if context is None:
        return None
    try:
        bridge = await _get_bridge(context)
        notifications = await asyncio.wait_for(
            asyncio.to_thread(bridge.hasNotificationAccess),
            timeout=_ACCESS_TIMEOUT_S,
        )
        usage = await asyncio.wait_for(
            asyncio.to_thread(bridge.hasUsageAccess),
            timeout=_ACCESS_TIMEOUT_S,
        )
        return {"notifications": bool(notifications), "usage": bool(usage)}
    except Exception:  # noqa: BLE001
        return None


async def collect_location_observation(
    perception: PerceptionManager,
    location_config: Any,
    *,
    now: datetime,
) -> bool:
    """Coarse location through the existing LocationBridge, if enabled.

    Reuses ``jenny.runtime.location.get_location`` so there is one location
    policy (permission, last-known cache, reverse geocoding) rather than a second
    copy for the Navigator. Privacy rounding still happens in
    ``PerceptionManager``.
    """
    if location_config is None or not bool(getattr(location_config, "enable", True)):
        return False
    if get_android_context() is None:
        return False
    try:
        from jenny.runtime.location import get_location, refresh_current_location

        # Refresh the shared last-known cache first: get_location() returns the
        # cached fix, and an hour-old Navigator cycle should not record a stale
        # position just because no conversation turn refreshed it.
        await refresh_current_location(location_config)
        fix = await get_location(location_config, precise=False)
        if fix is None:
            return False
        payload = {
            "source": "android",
            "kind": "location",
            "timestamp": iso(now),
            "value": {
                "latitude": fix.latitude,
                "longitude": fix.longitude,
                "accuracy": fix.accuracy_m,
            },
        }
        return _record(perception, payload)
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).debug("Navigator: location collection failed")
        return False
