"""Perception layer: local collection, privacy filtering and turn observation.

The rule from the upgrade plan is: **local code perceives reality, the LLM
understands it**. Nothing here calls a model. Raw observations are bounded,
privacy-filtered and stored under ``navigator/observations.jsonl``; the state
aggregator later turns them into facts.

Android sources (notifications, app usage, health, coarse location) enter
through :meth:`PerceptionManager.ingest_record`, either from a JSON file dropped
in ``navigator/inbox/`` or from a future Kotlin bridge. The filter below is the
single privacy gate: a disabled source is dropped, a sensitive app keeps only
non-identifying metadata, and health data is whitelisted field by field.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from jenny.navigator.models import (
    Observation,
    content_id,
    iso,
    local_date,
    local_hour,
    make_id,
    parse_iso,
    utc_now,
)
from jenny.navigator.store import NavigatorStore

# Apps whose notification / usage content is content-sensitive by default.
# Matching is by package prefix so a family of packages can be covered.
DEFAULT_SENSITIVE_PACKAGES: tuple[str, ...] = (
    "com.tencent.mm",            # WeChat
    "com.tencent.mobileqq",      # QQ
    "com.tencent.tim",
    "com.eg.android.AlipayGphone",
    "com.unionpay",
    "com.icbc",
    "com.chinamworld.main",      # CCB
    "com.bankcomm",
    "com.cmbchina",
    "com.tencent.mtt",           # QQ browser (often logged-in)
)

# Health values we are willing to keep when health capture is on. "Can be
# collected" is not a reason to collect it; these are the fields the state
# model actually uses.
_HEALTH_FIELDS = (
    "sleep_minutes", "sleep_hours", "steps", "resting_hr", "heart_rate",
    "workout_minutes", "active_minutes", "distance_km",
)

# Observation kinds the runtime understands from external sources. Unknown
# kinds are kept as ``other`` rather than rejected: a future Android source
# should not need a Python release to start reporting.
_EXTERNAL_KINDS = frozenset({
    "notification", "app_usage", "screen", "location", "weather", "calendar",
    "sleep", "steps", "heart_rate", "activity", "workout", "battery", "power",
    "health", "environment", "other",
})

_SCREEN_KINDS = frozenset({"screen"})
_HEALTH_KINDS = frozenset({"sleep", "steps", "heart_rate", "activity", "workout", "health"})
_NOTIFICATION_KINDS = frozenset({"notification"})
_APP_USAGE_KINDS = frozenset({"app_usage"})
_LOCATION_KINDS = frozenset({"location"})

_ACK_TOKENS = frozenset({
    "知道了", "知道", "好", "好的", "好吧", "行", "行的", "嗯", "嗯嗯", "哦",
    "收到", "ok", "okay", "k", "sure", "got it", "fine", "随便", "都行",
})

_DISMISSAL_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"我没事", r"没事(?:儿)?[，,。!.！ ]*只是", r"只是(?:有点)?累", r"不用管我",
    r"别管我", r"别(?:再)?提醒我", r"不用(?:再)?提醒", r"别(?:再)?说(?:了|这个)",
    r"我想(?:一个人)?静一静", r"let it go", r"i'?m fine",
    r"just (?:tired|tiring)", r"stop reminding", r"leave me alone",
))

_CORRECTION_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"你(?:说|搞|弄|想)错", r"不是这样", r"我没(?:说|这么|那个意思)",
    r"你误解", r"不是这个意思", r"别瞎(?:猜|说)", r"搞错了", r"胡说",
    r"you(?:'re| are) wrong", r"that'?s not (?:what|right)", r"i didn'?t say",
    r"you misunderstood", r"misunderstood me", r"don'?t make things up",
))


def _normalize(text: str) -> str:
    return re.sub(r"[\s，,。.!！?？~～、:：;；]+", "", text or "").lower()


def is_acknowledgement(text: str) -> bool:
    """True when the whole message is a bare acknowledgement.

    A fact, not a judgement: "知道了" repeated five times is what the plan calls
    evidence of possible resistance, and it is the aggregator's job to count the
    repetitions — not this function's job to label the user.
    """
    return _normalize(text) in _ACK_TOKENS


def is_dismissal(text: str) -> bool:
    """True when the user explicitly says they are fine / asks to back off."""
    return any(pattern.search(text or "") for pattern in _DISMISSAL_PATTERNS)


def is_correction(text: str) -> bool:
    """True when the user explicitly corrects the Navigator's judgement."""
    return any(pattern.search(text or "") for pattern in _CORRECTION_PATTERNS)


def is_short_reply(text: str) -> bool:
    """Short reply threshold used by the aggregator. Deliberately textual."""
    return len((text or "").strip()) <= 8


@dataclass(frozen=True, slots=True)
class PrivacyPolicy:
    """Which sources are collected, and how content is reduced."""

    capture_conversation: bool = True
    capture_conversation_text: bool = True
    capture_notifications: bool = True
    capture_app_usage: bool = True
    capture_health: bool = False
    capture_location: bool = True
    capture_screen: bool = False
    redact_sensitive_content: bool = True
    sensitive_packages: tuple[str, ...] = DEFAULT_SENSITIVE_PACKAGES
    blocked_packages: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, config: Any) -> "PrivacyPolicy":
        """Build from ``NavigatorConfig`` without importing it (leaf module)."""

        def _bool(name: str, default: bool) -> bool:
            value = getattr(config, name, default)
            return bool(default if value is None else value)

        packages = tuple(
            str(item).strip()
            for item in (getattr(config, "sensitive_packages", None) or DEFAULT_SENSITIVE_PACKAGES)
            if str(item).strip()
        )
        blocked = tuple(
            str(item).strip()
            for item in (getattr(config, "blocked_packages", None) or ())
            if str(item).strip()
        )
        return cls(
            capture_conversation=_bool("capture_conversation", True),
            capture_conversation_text=_bool("capture_conversation_text", True),
            capture_notifications=_bool("capture_notifications", True),
            capture_app_usage=_bool("capture_app_usage", True),
            capture_health=_bool("capture_health", False),
            capture_location=_bool("capture_location", True),
            capture_screen=_bool("capture_screen", False),
            redact_sensitive_content=_bool("redact_sensitive_content", True),
            sensitive_packages=packages or DEFAULT_SENSITIVE_PACKAGES,
            blocked_packages=blocked,
        )


@dataclass(frozen=True, slots=True)
class TurnObservation:
    """What one captured turn means for the local (non-LLM) layers."""

    user_observation_id: str
    assistant_observation_id: str = ""
    correction: bool = False
    dismissal: bool = False
    user_text: str = ""


@dataclass(slots=True)
class InboxResult:
    files: int = 0
    accepted: int = 0
    rejected: int = 0

    def summary(self) -> str:
        return f"{self.files} file(s), {self.accepted} accepted, {self.rejected} rejected"


@dataclass(frozen=True, slots=True)
class _Filtered:
    value: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)


def _clip_text(value: Any, limit: int = 800) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    """Bound external payloads: short strings, shallow nesting, small lists."""
    if depth > 3:
        return None
    if isinstance(value, str):
        return _clip_text(value, 500)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in list(value.items())[:30]:
            clean = _sanitize(item, depth=depth + 1)
            if clean is not None:
                out[str(key)[:80]] = clean
        return out
    if isinstance(value, (list, tuple)):
        return [
            clean for item in list(value)[:20]
            if (clean := _sanitize(item, depth=depth + 1)) is not None
        ]
    return _clip_text(value, 200)


class PerceptionManager:
    """Collects local observations; never interprets them."""

    def __init__(
        self,
        store: NavigatorStore,
        *,
        timezone_name: str | None = None,
        privacy: PrivacyPolicy | None = None,
        now_fn: Any = None,
    ) -> None:
        self.store = store
        self.timezone_name = timezone_name or ""
        self.privacy = privacy or PrivacyPolicy()
        self._now_fn = now_fn or utc_now

    def _now(self) -> datetime:
        value = self._now_fn()
        return value if value.tzinfo else value.astimezone()

    # -- conversation -------------------------------------------------------

    def record_turn(
        self,
        *,
        session_key: str,
        user_text: str,
        assistant_text: str = "",
        channel: str = "",
        now: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TurnObservation:
        """Record one user turn and one assistant reply.

        Idempotent within a short window: a goal continuation can run the same
        turn twice, and a duplicate observation would double-count the user's
        message length and the ack streak.
        """
        if not self.privacy.capture_conversation:
            return TurnObservation(user_observation_id="")
        when = now or self._now()
        text = (user_text or "").strip()
        if not text or text.startswith("/"):
            return TurnObservation(user_observation_id="")
        if self._is_recent_duplicate(text, session_key, when):
            return TurnObservation(user_observation_id="")

        correction = is_correction(text)
        dismissal = is_dismissal(text)
        value = {
            "text": _clip_text(text, 800) if self.privacy.capture_conversation_text else "",
            "text_hash": content_id(text),
            "chars": len(text),
            "lines": text.count("\n") + 1,
            "is_short": is_short_reply(text),
            "is_ack": is_acknowledgement(text),
            "is_question": text.rstrip().endswith(("?", "？")),
            "is_correction": correction,
            "is_dismissal": dismissal,
            "local_hour": local_hour(when, self.timezone_name),
            "local_date": local_date(when, self.timezone_name),
        }
        user_meta: dict[str, Any] = {
            "session_key": session_key,
            "channel": channel or "",
            **(metadata or {}),
        }
        user_obs = self._observation("conversation", "user_message", value, user_meta, when)
        self.store.add_observation(user_obs)

        assistant_id = ""
        reply = (assistant_text or "").strip()
        if reply:
            reply_value = {
                "text": _clip_text(reply, 500) if self.privacy.capture_conversation_text else "",
                "chars": len(reply),
                "is_question": reply.rstrip().endswith(("?", "？")),
                "local_hour": local_hour(when, self.timezone_name),
            }
            assistant_obs = self._observation(
                "conversation", "assistant_message", reply_value,
                {"session_key": session_key, "channel": channel or ""}, when,
            )
            self.store.add_observation(assistant_obs)
            assistant_id = assistant_obs.id

        if correction:
            self.store.add_event(self._event(
                "user_correction",
                f"The user corrected the Navigator: {_clip_text(text, 160)}",
                (user_obs.id,), when, {"session_key": session_key},
            ))
        elif dismissal:
            self.store.add_event(self._event(
                "user_dismissal",
                f"The user asked the Navigator to back off / said they are fine: "
                f"{_clip_text(text, 160)}",
                (user_obs.id,), when, {"session_key": session_key},
            ))

        return TurnObservation(
            user_observation_id=user_obs.id,
            assistant_observation_id=assistant_id,
            correction=correction,
            dismissal=dismissal,
            user_text=text,
        )

    def _is_recent_duplicate(self, text: str, session_key: str, now: datetime) -> bool:
        cutoff = 180
        for observation in reversed(self.store.tail_observations(20)):
            if observation.kind != "user_message":
                continue
            when = parse_iso(observation.timestamp)
            if when is None or (now - when).total_seconds() > cutoff:
                continue
            if str(observation.metadata.get("session_key", "")) != session_key:
                continue
            stored = str(observation.value.get("text", ""))
            if stored:
                duplicate = stored == _clip_text(text, 800)
            else:
                duplicate = str(observation.value.get("text_hash", "")) == content_id(text)
            if duplicate:
                return True
        return False

    # -- time ---------------------------------------------------------------

    def capture_time(self, now: datetime | None = None) -> Observation | None:
        """Record a daily time context (weekday, hour range, weekend flag)."""
        when = now or self._now()
        today = local_date(when, self.timezone_name)
        for observation in reversed(self.store.list_observations(kinds=("time_context",), limit=3)):
            if str(observation.value.get("local_date", "")) == today:
                return None
        hour = local_hour(when, self.timezone_name)
        weekday = when.astimezone().strftime("%A") if not self.timezone_name else ""
        if self.timezone_name:
            try:
                from zoneinfo import ZoneInfo
                weekday = when.astimezone(ZoneInfo(self.timezone_name)).strftime("%A")
            except Exception:
                weekday = when.astimezone().strftime("%A")
        value = {
            "local_date": today,
            "local_hour": hour,
            "weekday": weekday,
            "is_weekend": weekday in ("Saturday", "Sunday"),
            "part_of_day": (
                "night" if hour < 6 else "morning" if hour < 12
                else "afternoon" if hour < 18 else "evening"
            ),
        }
        observation = self._observation("system", "time_context", value, {}, when)
        self.store.add_observation(observation)
        return observation

    # -- external inbox -----------------------------------------------------

    def drain_inbox(self) -> InboxResult:
        """Ingest every file under ``navigator/inbox/`` and archive it.

        Valid files move to ``inbox/processed/``; files with no valid record
        move to ``inbox/failed/`` so a malformed bridge payload cannot loop
        forever. Individual invalid records inside an otherwise valid file are
        counted and skipped.
        """
        result = InboxResult()
        for path in self.store.inbox_files():
            result.files += 1
            try:
                too_big = path.stat().st_size > 2_000_000
            except OSError:
                too_big = False
            if too_big:
                self.store.move_inbox_file(path, failed=True)
                result.rejected += 1
                continue
            rows = self.store.read_inbox_file(path)
            if not rows:
                self.store.move_inbox_file(path, failed=True)
                result.rejected += 1
                continue
            accepted_here = 0
            for row in rows:
                observation = self.ingest_record(row)
                if observation is None:
                    result.rejected += 1
                else:
                    self.store.add_observation(observation)
                    result.accepted += 1
                    accepted_here += 1
            self.store.move_inbox_file(path, failed=accepted_here == 0)
        return result

    def ingest_record(self, record: dict[str, Any]) -> Observation | None:
        """Validate, privacy-filter and build one observation from external data."""
        if not isinstance(record, dict):
            return None
        source = str(record.get("source") or "external").strip()[:40] or "external"
        raw_kind = str(record.get("kind") or "").strip().lower()
        if not raw_kind:
            return None
        kind = raw_kind if raw_kind in _EXTERNAL_KINDS else "other"
        raw_value = record.get("value")
        value = dict(raw_value) if isinstance(raw_value, dict) else {}
        metadata = dict(record.get("metadata") or {})
        filtered = self._privacy_filter(kind, value, metadata)
        if filtered is None:
            return None
        when = parse_iso(str(record.get("timestamp") or "")) or self._now()
        return self._observation(source, kind, filtered.value, filtered.metadata, when)

    def _privacy_filter(
        self, kind: str, value: dict[str, Any], metadata: dict[str, Any],
    ) -> _Filtered | None:
        if kind in _SCREEN_KINDS and not self.privacy.capture_screen:
            return None
        if kind in _HEALTH_KINDS and not self.privacy.capture_health:
            return None
        if kind in _NOTIFICATION_KINDS and not self.privacy.capture_notifications:
            return None
        if kind in _APP_USAGE_KINDS and not self.privacy.capture_app_usage:
            return None
        if kind in _LOCATION_KINDS and not self.privacy.capture_location:
            return None

        cleaned = _sanitize(value)
        value = cleaned if isinstance(cleaned, dict) else {}
        meta = _sanitize(metadata)
        metadata = meta if isinstance(meta, dict) else {}

        package = str(value.get("package") or metadata.get("package") or "")
        if package and self._is_blocked(package):
            return None
        if kind in _HEALTH_KINDS:
            value = {key: value[key] for key in _HEALTH_FIELDS if key in value}
            if not value:
                return None

        if kind in _LOCATION_KINDS:
            # Coarse only: the Navigator needs "at home / out / another city",
            # not a GPS trace. Two decimals is roughly a kilometre.
            for key in ("latitude", "lat", "longitude", "lon", "lng"):
                if isinstance(value.get(key), (int, float)):
                    value[key] = round(float(value[key]), 2)
            for key in ("accuracy", "speed", "altitude", "bearing"):
                value.pop(key, None)

        sensitive = bool(package) and self._is_sensitive(package)
        if sensitive and self.privacy.redact_sensitive_content:
            if kind in _NOTIFICATION_KINDS:
                value = {"package": package, "app": value.get("app", "")}
            elif kind in _APP_USAGE_KINDS:
                allowed = {"package": package, "app": value.get("app", ""),
                           "minutes": value.get("minutes"), "count": value.get("count")}
                value = {key: item for key, item in allowed.items() if item not in (None, "")}
            elif kind in _SCREEN_KINDS:
                value = {"package": package, "app": value.get("app", "")}
            metadata = {**metadata, "redacted": True, "sensitive_package": _package_label(package)}

        if not value:
            return None
        return _Filtered(value=value, metadata=metadata)

    def _is_sensitive(self, package: str) -> bool:
        return any(
            package == prefix or package.startswith(prefix + ".")
            for prefix in self.privacy.sensitive_packages
        )

    def _is_blocked(self, package: str) -> bool:
        return any(
            package == prefix or package.startswith(prefix + ".")
            for prefix in self.privacy.blocked_packages
        )

    # -- helpers ------------------------------------------------------------

    def _observation(
        self,
        source: str,
        kind: str,
        value: dict[str, Any],
        metadata: dict[str, Any],
        when: datetime,
    ) -> Observation:
        timestamp = iso(when)
        payload = json.dumps(
            {"source": source, "kind": kind, "value": value, "ts": timestamp},
            ensure_ascii=False, sort_keys=True,
        )
        return Observation(
            id=make_id("obs", payload, when),
            timestamp=timestamp,
            source=source,
            kind=kind,
            value=value,
            metadata=metadata,
        )

    def _event(
        self,
        event_type: str,
        summary: str,
        source_ids: tuple[str, ...],
        when: datetime,
        metadata: dict[str, Any],
    ):
        from jenny.navigator.models import NavigatorEvent

        timestamp = iso(when)
        return NavigatorEvent(
            id=make_id("evt", f"{event_type}:{timestamp}:{summary}", when),
            timestamp=timestamp,
            type=event_type,
            summary=_clip_text(summary, 500),
            source_ids=source_ids,
            metadata=metadata,
        )


def _package_label(package: str) -> str:
    """A short, non-identifying label for a sensitive package."""
    return package.rsplit(".", 1)[-1] if package else ""
