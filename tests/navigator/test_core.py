"""Navigator Core: structured output, evidence traceability, silence on garbage."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from jenny.agent.turn_types import TurnOutcome
from jenny.navigator.core import CoreInputs, NavigatorCore
from jenny.navigator.models import (
    DIMENSION_KEYS,
    Dimension,
    StateSnapshot,
    iso,
)

NOW = datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc)


class FakeAgent:
    def __init__(self, response: str):
        self.response = response
        self.prompts: list[str] = []
        self.kwargs: list[dict] = []

    async def process_direct_outcome(self, prompt, **kwargs):
        self.prompts.append(prompt)
        self.kwargs.append(kwargs)
        return TurnOutcome.silent(final_text=self.response)


def _inputs() -> CoreInputs:
    dimensions = {
        key: Dimension(
            key, facts=(f"{key}: a fact with a time range",),
            evidence_ids=(f"obs_{key}",), change="stable",
        )
        for key in DIMENSION_KEYS
    }
    snapshot = StateSnapshot(
        timestamp=iso(NOW), window_start="2026-10-19T14:00:00Z",
        window_end=iso(NOW), dimensions=dimensions,
        metrics={}, evidence_ids=tuple(f"obs_{key}" for key in DIMENSION_KEYS),
    )
    return CoreInputs(
        snapshot=snapshot,
        recent_conversation=(),
        life_model={"life_goals": "长期目标", "current_state": "观察中的状态"},
        environment_model="官方事实: 某政策",
    )


def _json_response(**overrides):
    payload = {
        "observed_facts": ["过去 7 天有 3 次只回复“知道了”"],
        "hypotheses": [{
            "statement": "可能对当前提醒方式有抗拒",
            "confidence": "medium",
            "evidence_ids": ["obs_emotional_state"],
        }],
        "confidence": "medium",
        "state_assessment": "学习在继续，恢复可能不足",
        "conflicts": ["想学习 vs 睡眠不足"],
        "recommended_action": {
            "kind": "discuss", "description": "问一句最近是不是太累",
            "risk": "none", "reversible": True,
        },
        "need_user_discussion": True,
        "reasoning_evidence_ids": ["obs_emotional_state", "obs_body_state"],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def test_valid_json_becomes_a_decision():
    agent = FakeAgent(_json_response())
    decision = asyncio.run(NavigatorCore().analyze(agent, _inputs()))

    assert decision.parse_ok is True
    assert decision.confidence == "medium"
    assert decision.need_user_discussion is True
    assert decision.recommended_action.kind == "discuss"
    assert decision.hypotheses[0].confidence == "medium"
    assert agent.prompts and "aggregated state" in agent.prompts[0].lower()


def test_fenced_json_is_accepted():
    agent = FakeAgent("```json\n" + _json_response() + "\n```")
    decision = asyncio.run(NavigatorCore().analyze(agent, _inputs()))
    assert decision.parse_ok is True


def test_unknown_evidence_is_dropped_not_trusted():
    response = _json_response(
        reasoning_evidence_ids=["obs_body_state", "invented_id"],
        hypotheses=[{
            "statement": "a hypothesis",
            "confidence": "high",
            "evidence_ids": ["obs_body_state", "invented_id"],
        }],
    )
    decision = asyncio.run(NavigatorCore().analyze(FakeAgent(response), _inputs()))

    assert decision.reasoning_evidence_ids == ("obs_body_state",)
    assert decision.invalid_evidence_ids == ("invented_id",)
    assert decision.hypotheses[0].evidence_ids == ("obs_body_state",)


def test_hypothesis_without_traceable_evidence_is_dropped():
    response = _json_response(hypotheses=[{
        "statement": "invented", "confidence": "high",
        "evidence_ids": ["invented_id"],
    }])
    decision = asyncio.run(NavigatorCore().analyze(FakeAgent(response), _inputs()))
    assert decision.hypotheses == ()


def test_garbage_keeps_silence():
    decision = asyncio.run(NavigatorCore().analyze(FakeAgent("I think the user is tired."), _inputs()))
    assert decision.parse_ok is False
    assert decision.need_user_discussion is False
    assert decision.recommended_action.kind == "observe"


def test_prompt_does_not_carry_raw_observation_payloads():
    agent = FakeAgent(_json_response())
    asyncio.run(NavigatorCore().analyze(agent, _inputs()))
    prompt = agent.prompts[0]
    assert "sleep_minutes" not in prompt
    assert "com.tencent.mm" not in prompt
    assert "notification" not in prompt.casefold()


def test_core_runs_with_no_tools():
    agent = FakeAgent(_json_response())
    asyncio.run(NavigatorCore().analyze(agent, _inputs()))
    assert agent.kwargs[0]["tools"] is not None
    assert len(agent.kwargs[0]["tools"]) == 0
    assert agent.kwargs[0]["ephemeral"] is True
    assert agent.kwargs[0]["persist_user_message"] is False
