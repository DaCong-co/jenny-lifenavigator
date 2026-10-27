"""Permission layer: judgement and execution are separate.

The Navigator Core has no tools — it can only *propose*. This layer classifies
the proposal and decides whether the runtime may turn it into speech now, into a
question that asks for confirmation, or not at all.

Default policy (from the upgrade plan):

* observe / analyse / talk / low-risk reversible state suggestions — allowed;
* important life direction, relationships, finance, outbound communication,
  data deletion, system settings, other high-risk acts — require confirmation.

The runtime implements only the *speech* side. A proposal of kind ``act`` is
never executed here: it becomes a discussion, and the user's answer is what
authorises anything later. That is the strongest possible guard for Phase 1 —
the model literally has no executor to bypass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from jenny.navigator.models import CONFIDENCE_LEVELS, NavigatorDecision, RecommendedAction


class ActionTier(IntEnum):
    """Ordered by how much the action can change in the user's life."""

    OBSERVE = 0
    DISCUSS = 1
    LOW_RISK_SUGGESTION = 2
    REQUIRES_CONFIRMATION = 3


# Safety net, not the primary classifier: the Core's own ``risk`` field and
# ``kind == 'act'`` come first. These words catch a model that under-reports.
_HIGH_RISK_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"辞职", r"退学", r"分手", r"离婚", r"结婚", r"搬家", r"移民", r"投资", r"借钱",
    r"转账", r"支付", r"汇款", r"删除", r"清空", r"注销", r"发送", r"发布", r"公开",
    r"修改设置", r"关闭.*(?:账户|服务)", r"体检", r"手术", r"吃药", r"停药",
    r"resign", r"quit (?:job|school)", r"break up", r"divorce", r"marry", r"move",
    r"invest", r"borrow", r"transfer", r"pay(?:ment)?", r"delete", r"erase",
    r"send", r"publish", r"post", r"make (?:it )?public", r"settings",
    r"surgery", r"medication", r"stop taking",
))

_MEDIUM_RISK_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"建议.*(?:每天|坚持|必须)", r"应该.*(?:立刻|马上)", r"schedule", r"remind",
    r"提醒", r"安排", r"计划",
))


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    """The answer of the permission layer, with the reason that produced it."""

    allowed: bool
    tier: ActionTier
    requires_confirmation: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "tier": self.tier.name.lower(),
            "requires_confirmation": self.requires_confirmation,
            "reason": self.reason,
        }


class PermissionLayer:
    """Classify and authorise a Navigator proposal."""

    def authorize(
        self,
        decision: NavigatorDecision,
        *,
        trigger_allow_speak: bool = True,
    ) -> PermissionDecision:
        if not decision.parse_ok:
            return PermissionDecision(False, ActionTier.OBSERVE, reason="unparsed decision")
        if decision.confidence not in CONFIDENCE_LEVELS:
            return PermissionDecision(False, ActionTier.OBSERVE, reason="invalid confidence")

        action = decision.recommended_action
        tier, reason = self.classify(action)

        if action.kind == "observe":
            return PermissionDecision(
                False, ActionTier.OBSERVE, reason="observation only",
            )

        if tier is ActionTier.REQUIRES_CONFIRMATION:
            # A high-risk proposal may be *discussed*, but only as a question.
            # The runtime has no executor; this flag is what a future action
            # layer must check before doing anything.
            return PermissionDecision(
                allowed=False,
                tier=tier,
                requires_confirmation=True,
                reason=reason,
            )

        if not decision.need_user_discussion:
            return PermissionDecision(
                False, tier, reason="no user discussion requested",
            )
        if decision.confidence == "low":
            # Case B: a short-term signal is not a conclusion. Stay silent.
            return PermissionDecision(
                False, tier, reason="low confidence cannot justify a proactive message",
            )
        if not trigger_allow_speak:
            return PermissionDecision(
                False, tier, reason="the trigger allowed analysis but not speech",
            )
        return PermissionDecision(True, tier, reason=reason)

    def classify(self, action: RecommendedAction) -> tuple[ActionTier, str]:
        text = f"{action.description}\n{action.kind}"
        # Discussion is the confirmation mechanism, not an action. A high-risk
        # *topic* must still be discussable; what needs confirmation is doing
        # something about it.
        if action.kind == "discuss":
            return ActionTier.DISCUSS, "discussion is always allowed"
        if action.kind == "observe":
            return ActionTier.OBSERVE, "observation only"
        if action.kind == "act" or action.risk == "high" or not action.reversible:
            if action.risk == "high" or any(p.search(text) for p in _HIGH_RISK_PATTERNS):
                return ActionTier.REQUIRES_CONFIRMATION, "high-risk or irreversible action"
            if action.kind == "act":
                return ActionTier.REQUIRES_CONFIRMATION, "act requires explicit confirmation"
        if any(p.search(text) for p in _HIGH_RISK_PATTERNS):
            return ActionTier.REQUIRES_CONFIRMATION, "high-risk topic detected in the proposal"
        if action.kind == "suggest":
            if action.risk == "medium" or any(p.search(text) for p in _MEDIUM_RISK_PATTERNS):
                return ActionTier.DISCUSS, "suggestion phrased as a plan is a discussion"
            return ActionTier.LOW_RISK_SUGGESTION, "low-risk reversible suggestion"
        return ActionTier.DISCUSS, "unknown action kind treated as discussion"
