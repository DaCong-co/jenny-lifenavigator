"""Permission layer: judgement and execution stay separate."""

from __future__ import annotations

from jenny.navigator.models import (
    NavigatorDecision,
    RecommendedAction,
)
from jenny.navigator.permissions import ActionTier, PermissionLayer


def _decision(
    *,
    kind="discuss",
    risk="none",
    reversible=True,
    confidence="medium",
    need=True,
    parse_ok=True,
    description="聊一聊最近的睡眠",
):
    return NavigatorDecision(
        id="dec_1", timestamp="2026-10-26T12:00:00Z",
        confidence=confidence,
        recommended_action=RecommendedAction(
            kind=kind, description=description, risk=risk, reversible=reversible,
        ),
        need_user_discussion=need,
        parse_ok=parse_ok,
    )


def test_low_risk_discussion_is_allowed():
    result = PermissionLayer().authorize(_decision())
    assert result.allowed is True
    assert result.tier is ActionTier.DISCUSS


def test_low_confidence_cannot_speak():
    result = PermissionLayer().authorize(_decision(confidence="low"))
    assert result.allowed is False
    assert "low confidence" in result.reason


def test_unparsed_decision_is_denied():
    result = PermissionLayer().authorize(_decision(parse_ok=False))
    assert result.allowed is False


def test_high_risk_topic_can_still_be_discussed():
    result = PermissionLayer().authorize(_decision(
        kind="discuss", description="你最近在考虑辞职吗？想聊聊吗？",
    ))
    assert result.allowed is True
    assert result.tier is ActionTier.DISCUSS


def test_immediate_act_requires_confirmation():
    result = PermissionLayer().authorize(_decision(kind="act", risk="low"))
    assert result.allowed is False
    assert result.requires_confirmation is True
    assert result.tier is ActionTier.REQUIRES_CONFIRMATION


def test_high_risk_topic_requires_confirmation_even_when_phrased_as_suggestion():
    result = PermissionLayer().authorize(_decision(
        kind="suggest", description="建议你转账到新账户",
    ))
    assert result.allowed is False
    assert result.requires_confirmation is True


def test_medium_risk_suggestion_is_downgraded_to_discussion():
    result = PermissionLayer().authorize(_decision(
        kind="suggest", description="建议你每天坚持跑步",
    ))
    assert result.allowed is True
    assert result.tier is ActionTier.DISCUSS


def test_observe_never_speaks_even_if_discussion_is_requested():
    result = PermissionLayer().authorize(_decision(kind="observe"))
    assert result.allowed is False
    assert "observation only" in result.reason


def test_trigger_may_allow_analysis_but_not_speech():
    result = PermissionLayer().authorize(_decision(), trigger_allow_speak=False)
    assert result.allowed is False
    assert "not speech" in result.reason
