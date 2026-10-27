"""NavigatorRuntime: the loop that ties perception, trigger, core and outcomes.

One cycle is deliberately boring:

    capture time + inbox
    -> aggregate a snapshot
    -> evaluate elapsed outcomes
    -> ask the trigger whether a thought is worth an LLM call
    -> Navigator Core (structured)
    -> permission layer
    -> expression layer + delivery
    -> outcome record + self-model

The cycle never raises into cron: a broken cycle is logged and reported as an
error result, because the next hour must still work. A user turn is observed by
:class:`NavigatorTurnObserver` without any LLM call; the cron job does the
thinking, which keeps "continuous perception, not continuous thought" true.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from jenny.life_model.store import LifeModelStore
from jenny.navigator.aggregator import StateAggregator
from jenny.navigator.core import CoreInputs, NavigatorCore
from jenny.navigator.expression import ExpressionLayer
from jenny.navigator.models import (
    Hypothesis,
    StateSnapshot,
    TriggerDecision,
    TriggerPriority,
    content_id,
    iso,
    local_date,
    make_id,
    utc_now,
)
from jenny.navigator.outcome import OutcomeTracker
from jenny.navigator.perception import (
    PerceptionManager,
    PrivacyPolicy,
    TurnObservation,
)
from jenny.navigator.permissions import PermissionLayer
from jenny.navigator.rules import NavigatorRules, RuleBook
from jenny.navigator.store import NavigatorStore
from jenny.navigator.trigger import TriggerContext, TriggerEngine

Deliver = Callable[[Any], Awaitable[None]]

# One cycle at a time per process. A cycle can contain two LLM turns; if the
# cron tick fires again before it finishes, a second cycle would duplicate the
# snapshot, the outcome and possibly the message. ``acquire(blocking=False)`` is
# the point: a contender must be told "busy" now, not queued to run the same
# analysis a moment later. The lock is never contended by another thread, so
# holding it across awaits does not block the event loop.
_CYCLE_MUTEX = threading.Lock()


def _config_value(config: Any, name: str, default: Any) -> Any:
    value = getattr(config, name, default)
    return default if value is None else value


def _config_bool(config: Any, name: str, default: bool) -> bool:
    return bool(_config_value(config, name, default))


def _watch_topics(config: Any) -> tuple[str, ...]:
    raw = _config_value(config, "watch_topics", ()) or ()
    if isinstance(raw, str):
        raw = [raw]
    return tuple(str(item).strip() for item in raw if str(item).strip())


class NavigatorTurnObserver:
    """Cheap, synchronous observation of one user turn. No LLM, no delivery."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        timezone_name: str = "",
        config_provider: Callable[[], Any] | None = None,
        now_fn: Any = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.timezone_name = timezone_name
        self._config_provider = config_provider
        self._now_fn = now_fn or utc_now
        self._cached_config: Any = None
        self._cached_config_at = 0.0

    def _config(self) -> Any:
        now = time.monotonic()
        if self._cached_config is not None and (now - self._cached_config_at) < 60:
            return self._cached_config
        try:
            if self._config_provider is not None:
                config = self._config_provider()
            else:
                from jenny.config.loader import load_config
                config = load_config()
        except Exception:
            if self._cached_config is None:
                raise
            # A transient config read failure must not stop perception: the
            # previous privacy policy is still better than no observation.
            return self._cached_config
        self._cached_config = config
        self._cached_config_at = now
        return config

    def observe_turn(
        self,
        *,
        session_key: str,
        user_text: str,
        assistant_text: str = "",
        channel: str = "",
        now: datetime | None = None,
    ) -> TurnObservation:
        config = self._config()
        if not bool(getattr(config, "enabled", True)):
            return TurnObservation(user_observation_id="")
        store = NavigatorStore(self.workspace)
        perception = PerceptionManager(
            store,
            timezone_name=self.timezone_name,
            privacy=PrivacyPolicy.from_config(config),
            now_fn=self._now_fn,
        )
        turn = perception.record_turn(
            session_key=session_key,
            user_text=user_text,
            assistant_text=assistant_text,
            channel=channel,
            now=now,
        )
        if not turn.user_observation_id:
            return turn
        tracker = OutcomeTracker(store, now_fn=self._now_fn)
        event = self._latest_event(store, turn)
        if turn.correction:
            tracker.handle_correction(
                user_text=user_text,
                observation_id=turn.user_observation_id,
                event=event,
                now=now,
            )
        elif turn.dismissal:
            tracker.handle_dismissal(
                user_text=user_text,
                observation_id=turn.user_observation_id,
                event=event,
                now=now,
            )
        return turn

    @staticmethod
    def _latest_event(store: NavigatorStore, turn: TurnObservation):
        kind = "user_correction" if turn.correction else (
            "user_dismissal" if turn.dismissal else ""
        )
        if not kind:
            return None
        events = store.list_events(types=(kind,), limit=1)
        return events[-1] if events else None


@dataclass(frozen=True, slots=True)
class NavigatorCycleResult:
    status: str
    priority: str = TriggerPriority.NO_TRIGGER.value
    analysis_ran: bool = False
    spoke: bool = False
    delivered_text: str = ""
    decision_id: str = ""
    outcome_id: str = ""
    permissions: dict[str, Any] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    error: str = ""

    @property
    def summary(self) -> str:
        parts = [self.status, self.priority]
        if self.analysis_ran:
            parts.append("analysed")
        if self.spoke:
            parts.append("spoke")
        if self.error:
            parts.append(f"error={self.error}")
        return ": ".join(parts[:2]) + (" (" + ", ".join(parts[2:]) + ")" if parts[2:] else "")


class NavigatorRuntime:
    """One hourly cycle of the cognitive runtime."""

    def __init__(
        self,
        agent: Any,
        config: Any,
        *,
        workspace: str | Path | None = None,
        deliver: Deliver | None = None,
        timezone_name: str = "",
        now_fn: Any = None,
        store: NavigatorStore | None = None,
        core: NavigatorCore | None = None,
        trigger: TriggerEngine | None = None,
        permissions: PermissionLayer | None = None,
        expression: ExpressionLayer | None = None,
    ) -> None:
        self.agent = agent
        self.config = config
        default_workspace = getattr(agent, "workspace", None) or Path.cwd()
        self.workspace = Path(workspace) if workspace else Path(
            _config_value(config, "workspace_path", default_workspace)
        )
        self.deliver = deliver
        self.timezone_name = timezone_name or str(
            _config_value(config, "timezone", "") or ""
        )
        self._now_fn = now_fn or utc_now
        self.store = store or NavigatorStore(self.workspace)
        self.core = core or NavigatorCore()
        self.trigger = trigger or TriggerEngine()
        self.permissions = permissions or PermissionLayer()
        self.expression = expression or ExpressionLayer()

    async def run_cycle(self, *, now: datetime | None = None) -> NavigatorCycleResult:
        if not _config_bool(self.config, "enabled", True):
            return NavigatorCycleResult(status="disabled")
        if not _CYCLE_MUTEX.acquire(blocking=False):
            return NavigatorCycleResult(status="busy")
        try:
            return await self._run_cycle(now=now)
        except Exception as exc:  # noqa: BLE001 - a cycle must never crash cron
            logger.opt(exception=True).error("Navigator: cycle failed")
            return NavigatorCycleResult(
                status="error", error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            _CYCLE_MUTEX.release()

    async def _run_cycle(self, *, now: datetime | None) -> NavigatorCycleResult:
        moment = now or self._now_fn()
        if moment.tzinfo is None:
            moment = moment.astimezone()

        perception = PerceptionManager(
            self.store,
            timezone_name=self.timezone_name,
            privacy=PrivacyPolicy.from_config(self.config),
            now_fn=self._now_fn,
        )
        perception.capture_time(moment)
        inbox = perception.drain_inbox()
        if inbox.files:
            logger.info("Navigator inbox: {}", inbox.summary())

        rules = RuleBook(self.store, now_fn=self._now_fn).effective(
            NavigatorRules.from_config(self.config)
        )
        # Evaluate elapsed outcomes *before* aggregating, so the snapshot the
        # trigger and the Core see already reflects what the Navigator learned.
        tracker = OutcomeTracker(self.store, now_fn=self._now_fn)
        evaluated = tracker.evaluate_pending(now=moment)
        if evaluated:
            logger.info("Navigator: evaluated {} outcome(s)", len(evaluated))

        aggregator = StateAggregator(
            self.store,
            workspace=self.workspace,
            timezone_name=self.timezone_name,
            now_fn=self._now_fn,
        )
        snapshot = aggregator.build(
            window_days=rules.observation_window_days, now=moment,
        )
        self.store.add_snapshot(snapshot)

        trigger_decision = self._trigger(snapshot, moment, rules)
        self.store.log_trigger(
            trigger_decision, snapshot_timestamp=snapshot.timestamp,
        )
        if not trigger_decision.should_analyze:
            return NavigatorCycleResult(
                status="low_priority" if trigger_decision.priority is not TriggerPriority.NO_TRIGGER else "no_trigger",
                priority=trigger_decision.priority.value,
                reasons=trigger_decision.reasons,
            )

        decision = await self.core.analyze(
            self.agent, self._core_inputs(snapshot, trigger_decision),
        )
        self.store.add_decision(decision)
        self._persist_hypotheses(decision)
        self._record_run(moment, trigger_decision.priority)

        permission = self.permissions.authorize(
            decision, trigger_allow_speak=trigger_decision.allow_speak,
        )
        if permission.requires_confirmation:
            self._record_confirmation_required(decision, permission)
            return NavigatorCycleResult(
                status="analysis_only",
                priority=trigger_decision.priority.value,
                analysis_ran=True,
                decision_id=decision.id,
                permissions=permission.to_dict(),
                reasons=trigger_decision.reasons,
            )

        if not permission.allowed:
            return NavigatorCycleResult(
                status="analysis_only",
                priority=trigger_decision.priority.value,
                analysis_ran=True,
                decision_id=decision.id,
                permissions=permission.to_dict(),
                reasons=trigger_decision.reasons,
            )

        text = await self.expression.compose(
            self.agent,
            decision,
            expression_preferences=str(
                self._life_model().get("expression", "") or ""
            ),
            recent_conversation=self.recent_conversation(),
            previous_outcomes=tracker.recent_outcomes(limit=5),
            now=moment,
        )
        if not text:
            return NavigatorCycleResult(
                status="analysis_only",
                priority=trigger_decision.priority.value,
                analysis_ran=True,
                decision_id=decision.id,
                permissions=permission.to_dict(),
                reasons=trigger_decision.reasons,
            )

        delivered = await self._deliver(text)
        if not delivered:
            return NavigatorCycleResult(
                status="delivery_failed",
                priority=trigger_decision.priority.value,
                analysis_ran=True,
                decision_id=decision.id,
                permissions=permission.to_dict(),
                reasons=trigger_decision.reasons,
            )
        outcome = tracker.record_intervention(
            decision,
            delivered_text=text,
            action_kind=decision.recommended_action.kind,
            action_description=decision.recommended_action.description,
            window_hours=rules.outcome_window_hours,
            now=moment,
        )
        return NavigatorCycleResult(
            status="spoke",
            priority=trigger_decision.priority.value,
            analysis_ran=True,
            spoke=True,
            delivered_text=text,
            decision_id=decision.id,
            outcome_id=outcome.id,
            permissions=permission.to_dict(),
            reasons=trigger_decision.reasons,
        )

    # -- inputs -------------------------------------------------------------

    def _trigger(
        self, snapshot: StateSnapshot, moment: datetime, rules: NavigatorRules,
    ) -> TriggerDecision:
        # "Recent mention" for a watched topic is a three-day question, not a
        # seven-day one: a passing reference last week is not a reason to ping.
        observations = self.store.list_observations(
            since=moment - timedelta(days=3),
        )
        recent_texts = tuple(
            str(observation.value.get("text", ""))
            for observation in observations
            if observation.kind == "user_message" and observation.value.get("text")
        )[-10:]
        return self.trigger.evaluate(snapshot, TriggerContext(
            now=moment,
            rules=rules,
            timezone_name=self.timezone_name,
            recent_snapshots=tuple(self.store.list_snapshots(limit=200)),
            recent_events=tuple(self.store.list_events(limit=50)),
            pending_outcomes=tuple(self.store.pending_outcomes()),
            last_outcome=self.store.last_outcome(),
            recent_user_texts=recent_texts,
            watch_topics=_watch_topics(self.config),
            state=self.store.read_trigger_state(),
        ))

    def _life_model(self) -> dict[str, str]:
        return LifeModelStore(self.workspace).read_context_files()

    def _core_inputs(
        self, snapshot: StateSnapshot, trigger: TriggerDecision,
    ) -> CoreInputs:
        return CoreInputs(
            snapshot=snapshot,
            recent_events=tuple(self.store.list_events(limit=15)),
            outcomes=tuple(self.store.list_outcomes(limit=10)),
            hypotheses=tuple(self.store.list_hypotheses(
                statuses=("observing", "supported"), limit=10,
            )),
            self_notes=tuple(self.store.list_self_model_notes(limit=8)),
            recent_conversation=tuple(self.recent_conversation()),
            life_model=self._life_model(),
            environment_model=self.store.read_environment_model(),
            trigger_reasons=trigger.reasons,
        )

    def recent_conversation(self, limit: int = 12) -> list[dict[str, str]]:
        """Recent personal conversation, text only, newest last.

        Project sessions are deliberately excluded: they are work context, and
        the Navigator's subject is the person's life, not a codebase.
        """
        try:
            from jenny.session.keys import UNIFIED_SESSION_KEY
            session = self.agent.sessions.get_or_create(UNIFIED_SESSION_KEY)
        except Exception:
            return []
        messages: list[dict[str, str]] = []
        for message in session.messages:
            role = str(message.get("role", ""))
            if role not in ("user", "assistant"):
                continue
            try:
                from jenny.session.history_meta import is_synthetic_history_row
                if is_synthetic_history_row(message):
                    continue
            except Exception:
                pass
            content = _text_content(message.get("content"))
            if content:
                messages.append({"role": role, "content": content[:500]})
        return messages[-limit:]

    # -- persistence --------------------------------------------------------

    def _persist_hypotheses(self, decision) -> None:
        """Store hypotheses as hypotheses; never promote them to facts."""
        existing = {
            hypothesis.statement: hypothesis
            for hypothesis in self.store.list_hypotheses(
                statuses=("observing", "supported"), limit=100,
            )
        }
        now = utc_now()
        for draft in decision.hypotheses:
            previous = existing.get(draft.statement)
            evidence = tuple(dict.fromkeys(
                [*(previous.evidence_ids if previous else ()), *draft.evidence_ids]
            ))[:20]
            pattern = draft.statement
            hypothesis = Hypothesis(
                id=previous.id if previous else f"hyp_{content_id(pattern)}",
                statement=pattern,
                evidence_ids=evidence,
                confidence=draft.confidence,
                status=previous.status if previous else "observing",
                created_at=previous.created_at if previous else iso(now),
                updated_at=iso(now),
                origin="navigator",
                decision_id=decision.id,
            )
            self.store.add_hypothesis(hypothesis)

    def _record_run(self, moment: datetime, priority: TriggerPriority) -> None:
        state = self.store.read_trigger_state()
        today = local_date(moment, self.timezone_name)
        runs = state.get("runs_by_day")
        runs = dict(runs) if isinstance(runs, dict) else {}
        runs[today] = int(runs.get(today, 0) or 0) + 1
        cutoff = (moment - timedelta(days=14)).date().isoformat()
        runs = {day: count for day, count in runs.items() if day >= cutoff}
        state.update({
            "last_llm_at": iso(moment),
            "runs_by_day": runs,
            "last_priority": priority.value,
        })
        self.store.write_trigger_state(state)

    def _record_confirmation_required(self, decision, permission) -> None:
        from jenny.navigator.models import NavigatorEvent

        try:
            self.store.add_event(NavigatorEvent(
                id=make_id("evt", f"perm:{decision.id}", utc_now()),
                timestamp=iso(utc_now()),
                type="permission_confirmation_required",
                summary=(
                    f"Navigator proposed a high-risk action and did not execute it: "
                    f"{decision.recommended_action.description[:200]}"
                ),
                source_ids=(decision.id,),
                metadata={"permission": permission.to_dict()},
            ))
        except Exception:
            logger.opt(exception=True).warning("Navigator: could not record permission event")

    async def _deliver(self, text: str) -> bool:
        if self.deliver is None:
            return False
        try:
            from jenny.bus.events import OutboundMessage
            from jenny.webui.metadata import WEBUI_MESSAGE_SOURCE_METADATA_KEY

            message = OutboundMessage(
                channel=str(_config_value(self.config, "delivery_channel", "websocket")),
                chat_id=str(_config_value(self.config, "delivery_chat_id", "default")),
                content=text,
                metadata={WEBUI_MESSAGE_SOURCE_METADATA_KEY: {"kind": "navigator"}},
            )
            await self.deliver(message)
            return True
        except Exception:
            logger.opt(exception=True).error("Navigator: delivery failed")
            return False


def _text_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts).strip()
    return ""
