"""End-to-end wire: real Core and Expression, fake provider, one delivered turn."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from jenny.agent.turn_types import TurnOutcome
from jenny.config.schema import NavigatorConfig
from jenny.navigator.models import TriggerDecision, TriggerPriority
from jenny.navigator.runtime import NavigatorRuntime
from jenny.navigator.store import NavigatorStore

NOW = datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc)


class FakeSession:
    def __init__(self, messages=None):
        self.messages = list(messages or [])

    def clear(self):
        self.messages.clear()


class FakeSessions:
    def __init__(self):
        self._by_key = {}

    def get_or_create(self, key):
        if key not in self._by_key:
            messages = (
                [{"role": "user", "content": "最近作业很多"}]
                if key == "unified:default" else []
            )
            self._by_key[key] = FakeSession(messages)
        return self._by_key[key]

    def save(self, session):
        return None


class FakeAgent:
    """First LLM call is the Core (JSON), second is the Expression (plain text)."""

    def __init__(self):
        self.workspace = None
        self.sessions = FakeSessions()
        self.prompts: list[str] = []
        self._calls = 0

    async def process_direct_outcome(self, prompt, **kwargs):
        self.prompts.append(prompt)
        self._calls += 1
        if self._calls == 1:
            payload = {
                "observed_facts": ["过去 7 天有 3 次只回复“知道了”"],
                "hypotheses": [],
                "confidence": "medium",
                "state_assessment": "学习在继续，恢复可能不足",
                "conflicts": ["想学习 vs 睡眠不足"],
                "recommended_action": {
                    "kind": "discuss",
                    "description": "问一句最近是不是太累",
                    "risk": "none",
                    "reversible": True,
                },
                "need_user_discussion": True,
                "reasoning_evidence_ids": ["obs_emotional_state"],
            }
            return TurnOutcome.silent(final_text=json.dumps(payload, ensure_ascii=False))
        return TurnOutcome.silent(final_text="最近睡得少，要不要今晚早点休息？")


class FakeTrigger:
    def evaluate(self, snapshot, context):
        return TriggerDecision(
            priority=TriggerPriority.IMPORTANT,
            reasons=("multi-domain change",),
            allow_speak=True,
        )


def test_real_core_and_expression_produce_and_deliver_one_message(tmp_path):
    async def run():
        delivered = []

        async def deliver(message):
            delivered.append(message)

        agent = FakeAgent()
        agent.workspace = tmp_path
        agent.sessions.get_or_create("internal:navigator_core").messages.append(
            {"role": "assistant", "content": "OLD CORE JSON"},
        )
        agent.sessions.get_or_create("internal:navigator_expression").messages.append(
            {"role": "assistant", "content": "OLD EXPRESSION"},
        )
        runtime = NavigatorRuntime(
            agent,
            NavigatorConfig(
                quiet_start_hour=0, quiet_end_hour=0,
                min_minutes_between_analyses=30, max_analyses_per_day=3,
            ),
            workspace=tmp_path,
            deliver=deliver,
            timezone_name="Asia/Shanghai",
            now_fn=lambda: NOW,
            store=NavigatorStore(tmp_path),
            trigger=FakeTrigger(),
        )
        result = await runtime.run_cycle(now=NOW)
        return result, delivered, agent

    result, delivered, agent = asyncio.run(run())

    assert result.status == "spoke"
    assert result.spoke is True
    assert len(delivered) == 1
    assert delivered[0].content == "最近睡得少，要不要今晚早点休息？"
    # The Core prompt carried the aggregated facts, the Expression prompt carried
    # the structured decision — not the other way around.
    assert "Aggregated state" in agent.prompts[0]
    assert "Structured decision" in agent.prompts[1]
    assert "Output contract" in agent.prompts[0]
    assert "Return ONLY the message" in agent.prompts[1]
    assert agent.sessions.get_or_create("internal:navigator_core").messages == []
    assert agent.sessions.get_or_create("internal:navigator_expression").messages == []
