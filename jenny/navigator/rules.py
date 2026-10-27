"""Navigator rules and their version history.

Thresholds are *rules*, not observations: cooldowns, quiet hours, how many bad
outcomes before the Navigator changes approach. The plan requires every
important rule change to keep the current version and the last six months of
versions, so this module stores versions in ``navigator/rules.jsonl`` and never
promotes one automatically. Nothing in the runtime calls
:meth:`RuleBook.record_change` on its own; a rule change is a deliberate,
user-discussed act.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timedelta
from typing import Any

from jenny.navigator.models import RuleVersion, iso, utc_now
from jenny.navigator.store import NavigatorStore


@dataclass(frozen=True, slots=True)
class NavigatorRules:
    """Behavioural thresholds of the runtime.

    These are scheduling rules, never user severity scores. ``urgent`` in the
    trigger engine means "the user asked to be alerted about this", not "the
    user is in a bad state".
    """

    # At most one Navigator analysis per this gap, and at most N per local day.
    min_minutes_between_analyses: int = 180
    max_analyses_per_day: int = 3

    # Quiet hours: no proactive analysis or message. An ``urgent`` trigger is
    # the only bypass, and only when a watched topic actually matched.
    quiet_start_hour: int = 23
    quiet_end_hour: int = 7

    # Sustained drift needs this many distinct days with a moved dimension.
    drift_min_days: int = 2
    drift_window_days: int = 3

    # Window for evaluating an intervention's outcome.
    outcome_window_hours: int = 48

    # After the user says "I'm fine / back off", stay quiet this long unless a
    # genuinely high-value event appears.
    dismissal_cooldown_hours: int = 24

    # How much raw history the aggregator looks at.
    observation_window_days: int = 7

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_config(cls, config: Any) -> "NavigatorRules":
        """Read the same fields from ``NavigatorConfig`` when they exist."""
        values: dict[str, Any] = {}
        for field in fields(cls):
            raw = getattr(config, field.name, None)
            if raw is None:
                continue
            try:
                values[field.name] = int(raw)
            except (TypeError, ValueError):
                continue
        return cls(**values)

    def overlay(self, values: dict[str, Any]) -> "NavigatorRules":
        """Apply a stored rule version, ignoring unknown or invalid fields."""
        known = {field.name: field for field in fields(self)}
        updates: dict[str, Any] = {}
        for key, value in (values or {}).items():
            if key not in known:
                continue
            try:
                updates[key] = int(value)
            except (TypeError, ValueError):
                continue
        return replace(self, **updates) if updates else self


@dataclass(frozen=True, slots=True)
class RuleWindow:
    """The current rule version plus a compact evolution summary."""

    rules: NavigatorRules
    current_version: RuleVersion | None = None
    recent_versions: tuple[RuleVersion, ...] = ()

    def evolution_summary(self, *, limit: int = 6) -> str:
        if not self.recent_versions:
            return ""
        lines: list[str] = []
        for version in self.recent_versions[-limit:]:
            changed = sorted(
                key for key in version.new_rule
                if version.old_rule.get(key) != version.new_rule.get(key)
            )
            lines.append(
                f"v{version.version} ({version.effective_time[:10]}): "
                f"changed {', '.join(changed) or 'nothing'}; reason: {version.reason[:160]}"
            )
        return "\n".join(lines)


class RuleBook:
    """Read and write Navigator rule versions."""

    def __init__(self, store: NavigatorStore, *, now_fn: Any = None) -> None:
        self.store = store
        self._now_fn = now_fn or utc_now

    def effective(self, defaults: NavigatorRules) -> NavigatorRules:
        latest = self.store.latest_rule_version()
        if latest is None:
            return defaults
        return defaults.overlay(latest.new_rule)

    def window(self, defaults: NavigatorRules, *, months: int = 6) -> RuleWindow:
        versions = self.store.list_rule_versions(limit=200)
        cutoff = self._now_fn() - timedelta(days=max(1, months) * 30)
        recent: list[RuleVersion] = []
        for version in versions:
            when = self._parse(version.effective_time)
            if when is None or when >= cutoff:
                recent.append(version)
        return RuleWindow(
            rules=self.effective(defaults),
            current_version=recent[-1] if recent else None,
            recent_versions=tuple(recent),
        )

    def record_change(
        self,
        *,
        defaults: NavigatorRules,
        new_rules: NavigatorRules,
        reason: str,
        evidence: tuple[str, ...] = (),
        discussion_result: str = "",
        now: datetime | None = None,
    ) -> RuleVersion:
        """Append one controlled rule change and return it.

        This does **not** apply the rule to a running cycle: the caller decides
        when a new version takes effect. Recording before applying keeps the
        experiment auditable — old rule, reason, evidence, discussion, effect.
        """
        before = self.effective(defaults)
        before_dict = before.to_dict()
        after_dict = new_rules.to_dict()
        if before_dict == after_dict:
            raise ValueError("new rules are identical to the active rules")
        latest = self.store.latest_rule_version()
        version = RuleVersion(
            version=(latest.version if latest else 0) + 1,
            effective_time=iso(now),
            old_rule=before_dict,
            new_rule=after_dict,
            reason=str(reason or "").strip(),
            evidence=tuple(str(item) for item in evidence if str(item).strip()),
            discussion_result=str(discussion_result or "").strip(),
        )
        self.store.add_rule_version(version)
        return version

    @staticmethod
    def _parse(value: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.astimezone()
