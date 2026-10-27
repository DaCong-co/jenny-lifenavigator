"""The Life Navigator knobs exposed in WebUI Settings."""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from websockets.http11 import Headers
from websockets.http11 import Request as WsRequest

from jenny.channels.http_utils import (
    check_api_secret,
    http_error,
    http_json_response,
    parse_query,
)
from jenny.config.loader import load_config, save_config
from jenny.config.schema import Config, NavigatorConfig
from jenny.navigator.models import Dimension, SelfModelNote, StateSnapshot
from jenny.navigator.store import NavigatorStore
from jenny.runtime.context import get_runtime_context
from jenny.webui import worker_settings
from jenny.webui.settings_api import WebUISettingsError, settings_payload
from jenny.webui.settings_routes import WebUISettingsRouter
from jenny.webui.worker_settings import (
    NAVIGATOR_REARM_KEYS,
    navigator_access_payload,
    open_navigator_access,
    update_navigator_settings,
)

_SECRET = "s3cr3t-navigator"
_ASSETS = Path(__file__).resolve().parents[2] / "jenny" / "templates" / "ui" / "assets"


def test_the_settings_screen_wires_the_navigator_section() -> None:
    source = (_ASSETS / "mobile-settings.js").read_text(encoding="utf-8")

    for marker in (
        "this._section('navigator', 'ti-radar'",
        "_renderNavigator(d)",
        "navigator-enabled-toggle",
        "navigator-capture-notifications",
        "data-navigator-list",
        "api.updateNavigatorSettings",
        "api.getNavigatorAccess",
        "api.openNavigatorAccess",
        "this._wireNavigatorSettings();",
        "this._loadNavigatorAccess();",
    ):
        assert marker in source, marker


def test_both_locales_have_the_navigator_block() -> None:
    for locale in ("en", "it", "zh"):
        data = json.loads(
            (_ASSETS / "i18n" / f"{locale}.json").read_text(encoding="utf-8")
        )
        block = data["settings"]["navigator"]
        for key in (
            "title", "enable", "interval", "watchTopics", "captureNotifications",
            "android", "notificationsAccess", "usageAccess", "status",
        ):
            assert key in block, (locale, key)



@pytest.fixture
def config_path(tmp_path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "config.json"
    save_config(Config(), path)
    monkeypatch.setattr(get_runtime_context(), "config_path", path)
    monkeypatch.setattr(get_runtime_context(), "workspace_dir", tmp_path / "workspace")
    return path


def test_the_payload_carries_the_navigator_section(config_path) -> None:
    payload = settings_payload()["navigator"]

    assert payload["enabled"] is True
    assert payload["status"]["snapshot_at"] is None
    assert payload["watch_topics"] == []
    assert payload["capture_notifications"] is True


@pytest.mark.parametrize(
    ("field", "attr"),
    [
        ("interval_min", "interval_min"),
        ("min_minutes_between_analyses", "min_minutes_between_analyses"),
        ("max_analyses_per_day", "max_analyses_per_day"),
        ("quiet_start_hour", "quiet_start_hour"),
        ("quiet_end_hour", "quiet_end_hour"),
        ("observation_window_days", "observation_window_days"),
        ("outcome_window_hours", "outcome_window_hours"),
        ("drift_min_days", "drift_min_days"),
        ("dismissal_cooldown_hours", "dismissal_cooldown_hours"),
    ],
)
def test_numbers_carry_the_schema_bounds(config_path, field: str, attr: str) -> None:
    info = NavigatorConfig.model_fields[attr]
    payload = settings_payload()["navigator"][field]

    assert payload["value"] == int(getattr(
        load_config(config_path).agents.defaults.navigator, attr
    ))
    assert payload["min"] == (int(info.ge) if info.ge is not None else None)
    assert payload["max"] == (int(info.le) if info.le is not None else None)


@pytest.mark.asyncio
async def test_navigator_toggles_and_numbers_are_persisted(config_path) -> None:
    await update_navigator_settings({
        "navigator_enabled": ["0"],
        "navigator_interval_min": ["20"],
        "navigator_capture_notifications": ["0"],
        "navigator_quiet_start_hour": ["23"],
        "navigator_quiet_end_hour": ["7"],
    })

    nav = load_config(config_path).agents.defaults.navigator
    assert nav.enabled is False
    assert nav.interval_min == 20
    assert nav.capture_notifications is False
    assert nav.quiet_start_hour == 23
    assert nav.quiet_end_hour == 7


@pytest.mark.asyncio
async def test_watch_topics_are_split_deduplicated_and_can_be_cleared(config_path) -> None:
    await update_navigator_settings({
        "navigator_watch_topics": ["health, work\nsleep, health"],
    })
    assert load_config(config_path).agents.defaults.navigator.watch_topics == [
        "health", "work", "sleep",
    ]

    await update_navigator_settings({"navigator_watch_topics_clear": ["1"]})
    assert load_config(config_path).agents.defaults.navigator.watch_topics == []


@pytest.mark.asyncio
async def test_sensitive_and_blocked_packages_are_lists_too(config_path) -> None:
    await update_navigator_settings({
        "navigator_sensitive_packages": ["com.tencent.mm, com.example.bank"],
        "navigator_blocked_packages": ["com.example.game"],
    })

    nav = load_config(config_path).agents.defaults.navigator
    assert nav.sensitive_packages == ["com.tencent.mm", "com.example.bank"]
    assert nav.blocked_packages == ["com.example.game"]


@pytest.mark.asyncio
async def test_a_bad_boolean_is_refused(config_path) -> None:
    with pytest.raises(WebUISettingsError):
        await update_navigator_settings({"navigator_enabled": ["maybe"]})


def test_the_rearm_keys_name_the_job(config_path) -> None:
    assert "navigatorEnabled" in NAVIGATOR_REARM_KEYS
    assert "navigator_interval_min" in NAVIGATOR_REARM_KEYS


@pytest.mark.asyncio
async def test_access_payload_is_fail_soft(config_path, monkeypatch) -> None:
    async def _status():
        return {"notifications": True, "usage": False}

    monkeypatch.setattr(
        "jenny.navigator.android_perception.android_access_status", _status,
    )

    assert await navigator_access_payload() == {
        "access": {"notifications": True, "usage": False},
    }


@pytest.mark.asyncio
async def test_open_access_rejects_an_unknown_screen(config_path) -> None:
    with pytest.raises(WebUISettingsError):
        await open_navigator_access({"kind": ["calendar"]})


@pytest.mark.asyncio
async def test_open_access_returns_the_fresh_status(config_path, monkeypatch) -> None:
    async def _open(kind):
        assert kind == "notifications"
        return True

    async def _status():
        return {"notifications": True, "usage": False}

    monkeypatch.setattr(
        "jenny.navigator.android_perception.open_android_access_settings", _open,
    )
    monkeypatch.setattr(
        "jenny.navigator.android_perception.android_access_status", _status,
    )

    assert await open_navigator_access({"kind": ["notifications"]}) == {
        "opened": True,
        "access": {"notifications": True, "usage": False},
    }


def test_worker_module_exports_the_payload(config_path) -> None:
    payload = worker_settings.navigator_settings_payload()

    assert payload is not None
    assert "schedule" in payload


def test_status_reads_the_latest_snapshot(config_path) -> None:
    workspace = get_runtime_context().workspace_dir
    store = NavigatorStore(workspace)
    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    store.add_snapshot(StateSnapshot(
        timestamp=stamp,
        window_start=stamp,
        window_end=stamp,
        dimensions={
            "body_state": Dimension(
                key="body_state", facts=("sleep: 6h",), change="changed",
            ),
        },
    ))
    store.add_self_model_note(SelfModelNote(
        id="note-1",
        created_at=stamp,
        kind="overreminding",
        statement="Morning reminders tend to be dismissed on workdays.",
    ))

    status = settings_payload()["navigator"]["status"]

    assert status["snapshot_at"] == stamp
    assert status["last_analysis"] is None
    assert status["dimensions"][0]["key"] == "body_state"
    assert status["dimensions"][0]["facts"] == ["sleep: 6h"]
    assert status["self_model"] == [
        "Morning reminders tend to be dismissed on workdays.",
    ]


def _request(path: str, token: str | None = _SECRET) -> WsRequest:
    if token is not None:
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}token={urllib.parse.quote(token)}"
    return WsRequest(path=path, headers=Headers())


def _router(on_jobs_changed=None) -> WebUISettingsRouter:
    return WebUISettingsRouter(
        bus=MagicMock(),
        logger=MagicMock(),
        check_api_token=lambda request: check_api_secret(
            request.headers, request.path, _SECRET,
        ),
        parse_query=parse_query,
        json_response=http_json_response,
        error_response=http_error,
        on_jobs_changed=on_jobs_changed,
    )


@pytest.mark.asyncio
async def test_navigator_routes_require_auth(config_path) -> None:
    for path in (
        "/api/settings/navigator/update",
        "/api/settings/navigator/access",
    ):
        response = await _router().dispatch(_request(path, token=None), path)
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_update_route_persists_and_rearms(config_path) -> None:
    seen: list[str] = []
    path = "/api/settings/navigator/update"
    response = await _router(on_jobs_changed=seen.append).dispatch(
        _request(f"{path}?navigator_enabled=0&navigator_interval_min=20"), path,
    )

    assert response.status_code == 200
    assert json.loads(response.body)["navigator"]["enabled"] is False
    assert load_config(config_path).agents.defaults.navigator.enabled is False
    assert seen == ["navigator"]


@pytest.mark.asyncio
async def test_a_tuning_number_does_not_rearm_the_job(config_path) -> None:
    seen: list[str] = []
    path = "/api/settings/navigator/update"
    response = await _router(on_jobs_changed=seen.append).dispatch(
        _request(f"{path}?navigator_max_analyses_per_day=2"), path,
    )

    assert response.status_code == 200
    assert seen == []


@pytest.mark.asyncio
async def test_access_route_is_read_only(config_path, monkeypatch) -> None:
    async def _payload():
        return {"access": {"notifications": True, "usage": False}}

    monkeypatch.setattr(
        "jenny.webui.settings_routes.navigator_access_payload", _payload,
    )

    path = "/api/settings/navigator/access"
    response = await _router().dispatch(_request(path), path)

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "access": {"notifications": True, "usage": False},
    }


@pytest.mark.asyncio
async def test_open_access_route_rejects_an_unknown_kind(config_path) -> None:
    path = "/api/settings/navigator/open-access"
    response = await _router().dispatch(_request(f"{path}?kind=calendar"), path)

    assert response.status_code == 400
