"""Runtime orchestration: no trigger means no LLM; permission before speech."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from jenny.config.schema import NavigatorConfig
from jenny.navigator.models import (
    NavigatorDecision,
    RecommendedAction,
    TriggerDecision,
    TriggerPriority,
    iso,
)
from jenny.navigator.runtime import NavigatorRuntime
from jenny.navigator.store import NavigatorStore

NOW = datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc)


class FakeSessions:
    def __init__(self):
        self._by_key = {}

    def get_or_create(self, key):
        if key not in self._by_key:
            messages = (
                [
                    {"role": "user", "content": "最近作业很多"},
                    {"role": "assistant", "content": "要不要先休息一下？"},
                ]
                if key == "unified:default" else []
            )
            self._by_key[key] = SimpleNamespace(messages=messages)
        return self._by_key[key]

    def save(self, session):
        return None


class FakeAgent:
    def __init__(self, workspace):
        self.workspace = workspace
        self.sessions = FakeSessions()


class FakeTrigger:
    def __init__(self, decision):
        self.decision = decision
        self.calls = 0

    def evaluate(self, snapshot, context):
        self.calls += 1
        return self.decision


class FakeCore:
    def __init__(self, decision):
        self.decision = decision
        self.calls = 0

    async def analyze(self, agent, inputs):
        self.calls += 1
        return self.decision


class FakeExpression:
    def __init__(self, text="最近睡得少，要不要今晚早点休息？"):
        self.text = text
        self.calls = 0

    async def compose(self, agent, decision, **kwargs):
        self.calls += 1
        return self.text


def _decision(*, confidence="medium", kind="discuss", need=True):
    return NavigatorDecision(
        id="dec_1", timestamp=iso(NOW), confidence=confidence,
        observed_facts=("过去 7 天睡眠 6.1h",),
        state_assessment="恢复可能不足",
        recommended_action=RecommendedAction(kind=kind, description="问一句"),
        need_user_discussion=need,
        parse_ok=True,
    )


def _runtime(tmp_path, *, trigger_decision, decision=None, deliver=None,
             config=None, expression=None):
    store = NavigatorStore(tmp_path)
    agent = FakeAgent(tmp_path)
    core = FakeCore(decision or _decision())
    trigger = FakeTrigger(trigger_decision)
    expr = expression or FakeExpression()
    runtime = NavigatorRuntime(
        agent,
        config or NavigatorConfig(
            quiet_start_hour=0, quiet_end_hour=0,
            min_minutes_between_analyses=30, max_analyses_per_day=3,
        ),
        workspace=tmp_path,
        deliver=deliver,
        timezone_name="Asia/Shanghai",
        now_fn=lambda: NOW,
        store=store,
        core=core,
        trigger=trigger,
        expression=expr,
    )
    return runtime, store, core, trigger, expr


def _important(**kwargs):
    return TriggerDecision(
        priority=TriggerPriority.IMPORTANT,
        reasons=("multi-domain change",),
        allow_speak=kwargs.pop("allow_speak", True),
    )


async def test_a_second_cycle_is_busy_while_one_is_running(tmp_path):
    from jenny.navigator import runtime as runtime_module

    runtime, _store, core, _trigger, _expr = _runtime(
        tmp_path, trigger_decision=_important(),
    )
    runtime_module._CYCLE_MUTEX.acquire()
    try:
        result = await runtime.run_cycle(now=NOW)
    finally:
        runtime_module._CYCLE_MUTEX.release()

    assert result.status == "busy"
    assert core.calls == 0


async def test_no_trigger_costs_no_llm_call(tmp_path):
    runtime, store, core, _trigger, expr = _runtime(
        tmp_path, trigger_decision=TriggerDecision(priority=TriggerPriority.NO_TRIGGER),
    )
    result = await runtime.run_cycle(now=NOW)

    assert result.status == "no_trigger"
    assert core.calls == 0 and expr.calls == 0
    assert store.latest_snapshot() is not None


async def test_disabled_runtime_does_nothing(tmp_path):
    runtime, _store, _core, trigger, _expr = _runtime(
        tmp_path, trigger_decision=_important(),
        config=NavigatorConfig(enabled=False),
    )
    result = await runtime.run_cycle(now=NOW)
    assert result.status == "disabled"
    assert trigger.calls == 0


async def test_important_trigger_speaks_and_records_outcome(tmp_path):
    delivered = []

    async def deliver(message):
        delivered.append(message)

    runtime, store, core, _trigger, expr = _runtime(
        tmp_path, trigger_decision=_important(), deliver=deliver,
    )
    result = await runtime.run_cycle(now=NOW)

    assert result.status == "spoke"
    assert result.spoke is True
    assert core.calls == 1 and expr.calls == 1
    assert len(delivered) == 1
    assert delivered[0].content == expr.text
    outcomes = store.list_outcomes()
    assert len(outcomes) == 1 and outcomes[0].status == "pending"
    assert store.read_trigger_state()["runs_by_day"]["2026-10-26"] == 1


async def test_low_confidence_is_analysis_only(tmp_path):
    delivered = []

    async def deliver(message):
        delivered.append(message)

    runtime, store, _core, _trigger, _expr = _runtime(
        tmp_path, trigger_decision=_important(),
        decision=_decision(confidence="low"), deliver=deliver,
    )
    result = await runtime.run_cycle(now=NOW)

    assert result.status == "analysis_only"
    assert delivered == []
    assert store.list_outcomes() == []


async def test_high_risk_action_is_not_executed(tmp_path):
    delivered = []

    async def deliver(message):
        delivered.append(message)

    runtime, store, _core, _trigger, _expr = _runtime(
        tmp_path, trigger_decision=_important(),
        decision=_decision(kind="act"), deliver=deliver,
    )
    result = await runtime.run_cycle(now=NOW)

    assert result.status == "analysis_only"
    assert result.permissions["requires_confirmation"] is True
    assert delivered == []
    assert store.list_events(types=("permission_confirmation_required",))


async def test_speech_not_allowed_by_trigger_is_analysis_only(tmp_path):
    delivered = []

    async def deliver(message):
        delivered.append(message)

    runtime, _store, core, _trigger, expr = _runtime(
        tmp_path, trigger_decision=_important(allow_speak=False), deliver=deliver,
    )
    result = await runtime.run_cycle(now=NOW)
    assert result.status == "analysis_only"
    assert core.calls == 1 and expr.calls == 0
    assert delivered == []


async def test_delivery_failure_does_not_create_an_outcome(tmp_path):
    async def deliver(message):
        raise RuntimeError("no channel")

    runtime, store, _core, _trigger, _expr = _runtime(
        tmp_path, trigger_decision=_important(), deliver=deliver,
    )
    result = await runtime.run_cycle(now=NOW)

    assert result.status == "delivery_failed"
    assert store.list_outcomes() == []
