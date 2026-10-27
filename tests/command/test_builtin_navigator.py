"""`/navigator`: a read-only window on the runtime state."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jenny.bus.events import InboundMessage
from jenny.command.builtin import cmd_navigator
from jenny.command.router import CommandContext
from jenny.command.specs import BUILTIN_COMMAND_SPECS
from jenny.navigator.models import (
    DIMENSION_KEYS,
    Dimension,
    SelfModelNote,
    StateSnapshot,
    iso,
)
from jenny.navigator.store import NavigatorStore


def _ctx(workspace):
    return CommandContext(
        msg=InboundMessage(
            channel="websocket", sender_id="user", chat_id="default",
            content="/navigator",
        ),
        session=None,
        key="unified:default",
        raw="/navigator",
        args="",
        loop=SimpleNamespace(workspace=workspace),
    )


def test_the_command_is_listed_with_a_tabler_icon():
    spec = {item.command: item for item in BUILTIN_COMMAND_SPECS}["/navigator"]
    assert spec.icon == "radar"
    assert spec.scope == "any"


@pytest.mark.asyncio
async def test_no_snapshot_explains_the_hourly_cycle(tmp_path):
    out = await cmd_navigator(_ctx(tmp_path))
    assert "No local cycle has completed yet" in out.content
    assert out.metadata["render_as"] == "text"


@pytest.mark.asyncio
async def test_it_renders_the_seven_dimensions_and_self_model(tmp_path):
    store = NavigatorStore(tmp_path)
    store.add_snapshot(StateSnapshot(
        timestamp=iso(),
        window_start="2026-10-19T00:00:00Z",
        window_end="2026-10-26T00:00:00Z",
        dimensions={
            key: Dimension(
                key, facts=(f"{key} fact",), evidence_ids=(f"obs_{key}",),
                change="stable",
            )
            for key in DIMENSION_KEYS
        },
    ))
    store.add_self_model_note(SelfModelNote(
        id="smn_1", created_at=iso(), kind="overreminding",
        statement="Study reminders were rejected twice",
    ))

    out = await cmd_navigator(_ctx(tmp_path))

    for key in DIMENSION_KEYS:
        assert f"- {key} [" in out.content
    assert "Navigator self-model" in out.content
    assert "Study reminders were rejected twice" in out.content
