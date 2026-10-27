"""Perception hook: observes user turns, ignores internal ones, never speaks."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from jenny.agent.hook import AgentRunHookContext
from jenny.navigator.hook import NavigatorPerceptionHook
from jenny.navigator.runtime import NavigatorTurnObserver
from jenny.navigator.store import NavigatorStore

NOW = datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc)


class ConfigProvider:
    def __call__(self):
        return SimpleNamespace(
            capture_conversation=True, capture_conversation_text=True,
            capture_notifications=True, capture_app_usage=True,
            capture_health=False, capture_location=True, capture_screen=False,
            redact_sensitive_content=True, sensitive_packages=(),
        )


def _observer(tmp_path):
    return NavigatorTurnObserver(
        tmp_path, timezone_name="Asia/Shanghai", config_provider=ConfigProvider(),
        now_fn=lambda: NOW,
    )


def test_hook_records_one_user_turn(tmp_path):
    hook = NavigatorPerceptionHook(_observer(tmp_path))
    context = AgentRunHookContext(
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "最近作业很多"},
        ],
        final_content="那就先做一件小事。",
        session_key="unified:default",
    )
    asyncio.run(hook.after_run(context))

    store = NavigatorStore(tmp_path)
    observations = store.list_observations()
    assert [o.kind for o in observations] == ["user_message", "assistant_message"]
    assert observations[0].value["text"] == "最近作业很多"


def test_hook_ignores_internal_sessions(tmp_path):
    hook = NavigatorPerceptionHook(_observer(tmp_path))
    for key in ("dream:20261026", "internal:navigator_core", "subagent:x", "heartbeat"):
        asyncio.run(hook.after_run(AgentRunHookContext(
            messages=[{"role": "user", "content": "internal work"}],
            final_content="done",
            session_key=key,
        )))
    assert NavigatorStore(tmp_path).list_observations() == []


def test_hook_ignores_synthetic_user_rows(tmp_path):
    hook = NavigatorPerceptionHook(_observer(tmp_path))
    asyncio.run(hook.after_run(AgentRunHookContext(
        messages=[
            {"role": "user", "content": "a subagent result", "injected_event": "subagent_result"},
            {"role": "user", "content": "but this is the real user"},
        ],
        final_content="ok",
        session_key="unified:default",
    )))
    observations = NavigatorStore(tmp_path).list_observations()
    assert observations[0].value["text"] == "but this is the real user"


def test_observer_correction_invalidates_hypotheses_and_notes_itself(tmp_path):
    from jenny.navigator.models import Hypothesis, iso

    store = NavigatorStore(tmp_path)
    store.add_hypothesis(Hypothesis(
        id="hyp_1", statement="可能不想学习", evidence_ids=("obs_x",),
        confidence="medium", status="observing", created_at=iso(NOW),
    ))
    turn = _observer(tmp_path).observe_turn(
        session_key="unified:default",
        user_text="你搞错了，我没说过不想学习",
        now=NOW,
    )
    assert turn.correction is True
    assert store.list_hypotheses(statuses=("invalidated",))[0].id == "hyp_1"
    assert store.list_self_model_notes()[0].kind == "misjudgement"
