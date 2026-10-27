"""File store for the Life Navigator runtime.

Independent of :class:`jenny.agent.memory.MemoryStore` and of the Life Model
store. The runtime owns ``navigator/`` and nothing else; the only shared
primitive is ``utils.path.atomic_write`` (low-level file I/O, not memory logic).

Layout::

    navigator/
    ├── observations.jsonl    # raw local observations (rolling, bounded)
    ├── events.jsonl          # meaningful, factual changes
    ├── snapshots.jsonl       # state aggregator output
    ├── hypotheses.jsonl      # append-only status updates, deduped by id
    ├── outcomes.jsonl        # what the Navigator did and what followed
    ├── self_model.jsonl      # notes about the Navigator's own judgement
    ├── decisions.jsonl       # structured Navigator Core output
    ├── trigger_log.jsonl     # every trigger evaluation (bounded)
    ├── rules.jsonl           # controlled rule versions
    ├── trigger_state.json    # throttle bookkeeping
    ├── environment.md        # user-owned Environment Model (read-only here)
    └── inbox/                # external observations dropped by Android/other code

The runtime never imports the Life Model store here: :mod:`jenny.navigator.runtime`
reads it explicitly, so the two storage boundaries stay visible.
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from loguru import logger

from jenny.navigator.models import (
    Hypothesis,
    NavigatorDecision,
    NavigatorEvent,
    Observation,
    OutcomeRecord,
    RuleVersion,
    SelfModelNote,
    StateSnapshot,
    TriggerDecision,
    parse_iso,
    utc_now,
)
from jenny.utils.path import atomic_write

# Bounded growth: raw observations are cheap but not free, and a phone should
# never accumulate an unbounded JSONL. Compaction keeps the newest lines and
# happens only when the file crosses the cap.
_MAX_JSONL_BYTES = 1_500_000
_KEEP_LINES = 2_000
_TRIGGER_LOG_KEEP = 500
_DECISIONS_KEEP = 500

_WRITE_LOCK = threading.RLock()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file tolerantly: a corrupt line is skipped, not fatal."""
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return []
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    text = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows)
    atomic_write(path, text + ("\n" if text else ""))


def _append_jsonl(
    path: Path,
    row: dict[str, Any],
    *,
    max_bytes: int = _MAX_JSONL_BYTES,
    keep_lines: int = _KEEP_LINES,
) -> None:
    """Append one record; compact the file when it grows past ``max_bytes``.

    Append (not rewrite) is the normal path: a user turn writes one observation,
    and rewriting a megabyte for that would be both slower and a larger window
    for a crash to lose history. Compaction is the exception and still atomic.
    """
    line = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
    with _WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            logger.opt(exception=True).warning("Navigator: could not append {}", path.name)
            return
        try:
            too_big = path.stat().st_size > max_bytes
        except OSError:
            too_big = False
        if not too_big:
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        tail = lines[-keep_lines:]
        atomic_write(path, "\n".join(tail) + ("\n" if tail else ""))


def _dedupe_newest(rows: Iterable[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Append-only updates: the last row with a given id is the current state."""
    latest: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        value = str(row.get(key, ""))
        if not value:
            continue
        if value not in latest:
            order.append(value)
        latest[value] = row
    return [latest[value] for value in order]


def _tail_jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    """Read only the last ``limit`` JSONL rows, without parsing the whole file.

    The perception hook calls this on every user turn to suppress duplicates;
    reading a megabyte to look at twenty lines would put avoidable work on the
    phone.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            window = min(size, 128 * 1024)
            handle.seek(size - window)
            data = handle.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if window < size and lines:
        lines = lines[1:]  # the first line may be partial
    rows: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


class NavigatorStore:
    """Pure file I/O for the Navigator runtime under ``navigator/``."""

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace)
        self.root = self.workspace / "navigator"
        self.inbox_dir = self.root / "inbox"
        self.processed_dir = self.inbox_dir / "processed"
        self.failed_dir = self.inbox_dir / "failed"
        self.observations_file = self.root / "observations.jsonl"
        self.events_file = self.root / "events.jsonl"
        self.snapshots_file = self.root / "snapshots.jsonl"
        self.hypotheses_file = self.root / "hypotheses.jsonl"
        self.outcomes_file = self.root / "outcomes.jsonl"
        self.self_model_file = self.root / "self_model.jsonl"
        self.decisions_file = self.root / "decisions.jsonl"
        self.trigger_log_file = self.root / "trigger_log.jsonl"
        self.rules_file = self.root / "rules.jsonl"
        self.trigger_state_file = self.root / "trigger_state.json"
        self.environment_file = self.root / "environment.md"
        self.ensure_layout()

    def ensure_layout(self) -> None:
        for path in (self.root, self.inbox_dir, self.processed_dir, self.failed_dir):
            path.mkdir(parents=True, exist_ok=True)

    # -- observations -------------------------------------------------------

    def add_observation(self, observation: Observation) -> None:
        _append_jsonl(self.observations_file, observation.to_dict())

    def add_observations(self, observations: Iterable[Observation]) -> None:
        for observation in observations:
            self.add_observation(observation)

    def list_observations(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        kinds: Iterable[str] | None = None,
        limit: int | None = None,
    ) -> list[Observation]:
        wanted = set(kinds) if kinds is not None else None
        rows: list[Observation] = []
        for raw in _read_jsonl(self.observations_file):
            observation = Observation.from_dict(raw)
            if not observation.id or not observation.timestamp:
                continue
            when = parse_iso(observation.timestamp)
            if when is None:
                continue
            if since is not None and when < since:
                continue
            if until is not None and when > until:
                continue
            if wanted is not None and observation.kind not in wanted:
                continue
            rows.append(observation)
        rows.sort(key=lambda item: item.timestamp)
        if limit is not None and limit >= 0:
            rows = rows[-limit:] if limit else []
        return rows

    def last_observation_matching(
        self, *, kind: str, within_seconds: int = 180,
    ) -> Observation | None:
        now = utc_now()
        for raw in reversed(_tail_jsonl(self.observations_file, 20)):
            observation = Observation.from_dict(raw)
            if observation.kind != kind:
                continue
            when = parse_iso(observation.timestamp)
            if when is not None and (now - when).total_seconds() <= within_seconds:
                return observation
        return None

    def tail_observations(self, limit: int = 20) -> list[Observation]:
        """The newest observations, without a full-file parse."""
        observations = [Observation.from_dict(row) for row in _tail_jsonl(
            self.observations_file, max(1, limit),
        )]
        observations = [item for item in observations if item.id and item.timestamp]
        return observations[-limit:]

    # -- events -------------------------------------------------------------

    def add_event(self, event: NavigatorEvent) -> None:
        _append_jsonl(self.events_file, event.to_dict())

    def list_events(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        types: Iterable[str] | None = None,
        limit: int | None = None,
    ) -> list[NavigatorEvent]:
        wanted = set(types) if types is not None else None
        events: list[NavigatorEvent] = []
        for raw in _read_jsonl(self.events_file):
            event = NavigatorEvent.from_dict(raw)
            when = parse_iso(event.timestamp)
            if not event.id or when is None:
                continue
            if since is not None and when < since:
                continue
            if until is not None and when > until:
                continue
            if wanted is not None and event.type not in wanted:
                continue
            events.append(event)
        events.sort(key=lambda item: item.timestamp)
        if limit is not None and limit >= 0:
            events = events[-limit:] if limit else []
        return events

    # -- snapshots ----------------------------------------------------------

    def add_snapshot(self, snapshot: StateSnapshot) -> None:
        _append_jsonl(self.snapshots_file, snapshot.to_dict())

    def list_snapshots(self, *, limit: int = 20) -> list[StateSnapshot]:
        rows = _read_jsonl(self.snapshots_file)
        snapshots = [StateSnapshot.from_dict(row) for row in rows]
        snapshots = [s for s in snapshots if s.timestamp]
        snapshots.sort(key=lambda item: item.timestamp)
        return snapshots[-limit:] if limit else snapshots

    def latest_snapshot(self) -> StateSnapshot | None:
        snapshots = self.list_snapshots(limit=1)
        return snapshots[-1] if snapshots else None

    # -- decisions / trigger log -------------------------------------------

    def add_decision(self, decision: NavigatorDecision) -> None:
        _append_jsonl(
            self.decisions_file, decision.to_dict(), keep_lines=_DECISIONS_KEEP,
        )

    def list_decisions(self, *, limit: int = 20) -> list[NavigatorDecision]:
        rows = _read_jsonl(self.decisions_file)
        decisions = [NavigatorDecision.from_dict(row) for row in rows]
        decisions = [d for d in decisions if d.id]
        return decisions[-limit:] if limit else decisions

    def log_trigger(self, decision: TriggerDecision, *, snapshot_timestamp: str = "") -> None:
        row = decision.to_dict()
        row["snapshot_timestamp"] = snapshot_timestamp
        row["logged_at"] = utc_now().isoformat(timespec="seconds")
        _append_jsonl(self.trigger_log_file, row, keep_lines=_TRIGGER_LOG_KEEP)

    # -- hypotheses (append-only status updates) ----------------------------

    def add_hypothesis(self, hypothesis: Hypothesis) -> None:
        _append_jsonl(self.hypotheses_file, hypothesis.to_dict())

    def list_hypotheses(
        self,
        *,
        statuses: Iterable[str] | None = None,
        limit: int | None = None,
    ) -> list[Hypothesis]:
        rows = _dedupe_newest(_read_jsonl(self.hypotheses_file), "id")
        hypotheses = [Hypothesis.from_dict(row) for row in rows]
        if statuses is not None:
            wanted = set(statuses)
            hypotheses = [h for h in hypotheses if h.status in wanted]
        hypotheses.sort(key=lambda item: item.created_at)
        if limit is not None and limit >= 0:
            hypotheses = hypotheses[-limit:] if limit else []
        return hypotheses

    def update_hypothesis_status(self, hypothesis_id: str, status: str) -> bool:
        with _WRITE_LOCK:
            rows = _read_jsonl(self.hypotheses_file)
            current = next(
                (Hypothesis.from_dict(row) for row in reversed(rows)
                 if str(row.get("id", "")) == hypothesis_id),
                None,
            )
            if current is None:
                return False
            updated = replace(
                current,
                status=status,
                updated_at=utc_now().isoformat(timespec="seconds"),
            )
            _append_jsonl(self.hypotheses_file, updated.to_dict())
            return True

    def invalidate_hypotheses(self, *, reason: str = "") -> int:
        """Invalidate every still-active hypothesis. Used after a user correction.

        Deliberately broad: a correction means the *current judgement* is void,
        and the runtime cannot know which hypothesis the user was answering.
        Hypotheses are the Navigator's own work-in-progress, so throwing them
        away is cheap and safe; writing any of it into the user model is not.
        """
        changed = 0
        for hypothesis in self.list_hypotheses(statuses=("observing", "supported")):
            updated = replace(
                hypothesis,
                status="invalidated",
                updated_at=utc_now().isoformat(timespec="seconds"),
            )
            metadata_note = f"invalidated: {reason}" if reason else "invalidated"
            row = updated.to_dict()
            row["note"] = metadata_note
            with _WRITE_LOCK:
                _append_jsonl(self.hypotheses_file, row)
            changed += 1
        return changed

    # -- outcomes (append-only status updates) -----------------------------

    def add_outcome(self, outcome: OutcomeRecord) -> None:
        _append_jsonl(self.outcomes_file, outcome.to_dict())

    def update_outcome(self, outcome: OutcomeRecord) -> None:
        self.add_outcome(outcome)

    def list_outcomes(
        self,
        *,
        statuses: Iterable[str] | None = None,
        limit: int | None = None,
    ) -> list[OutcomeRecord]:
        rows = _dedupe_newest(_read_jsonl(self.outcomes_file), "id")
        outcomes = [OutcomeRecord.from_dict(row) for row in rows]
        if statuses is not None:
            wanted = set(statuses)
            outcomes = [o for o in outcomes if o.status in wanted]
        outcomes.sort(key=lambda item: item.created_at)
        if limit is not None and limit >= 0:
            outcomes = outcomes[-limit:] if limit else []
        return outcomes

    def pending_outcomes(self) -> list[OutcomeRecord]:
        return self.list_outcomes(statuses=("pending",))

    def last_outcome(self) -> OutcomeRecord | None:
        outcomes = self.list_outcomes(limit=1)
        return outcomes[-1] if outcomes else None

    # -- self-model ---------------------------------------------------------

    def add_self_model_note(self, note: SelfModelNote) -> None:
        _append_jsonl(self.self_model_file, note.to_dict())

    def list_self_model_notes(
        self, *, active_only: bool = True, limit: int | None = 20,
    ) -> list[SelfModelNote]:
        rows = _dedupe_newest(_read_jsonl(self.self_model_file), "id")
        notes = [SelfModelNote.from_dict(row) for row in rows]
        if active_only:
            notes = [note for note in notes if note.active]
        notes.sort(key=lambda item: item.created_at)
        if limit is not None and limit >= 0:
            notes = notes[-limit:] if limit else []
        return notes

    def deactivate_self_model_note(self, note_id: str) -> bool:
        with _WRITE_LOCK:
            note = next(
                (n for n in self.list_self_model_notes(active_only=False)
                 if n.id == note_id),
                None,
            )
            if note is None:
                return False
            self.add_self_model_note(replace(note, active=False))
            return True

    # -- rule versions ------------------------------------------------------

    def add_rule_version(self, version: RuleVersion) -> None:
        _append_jsonl(self.rules_file, version.to_dict())

    def list_rule_versions(self, *, limit: int = 50) -> list[RuleVersion]:
        rows = _read_jsonl(self.rules_file)
        versions = [RuleVersion.from_dict(row) for row in rows]
        versions = [v for v in versions if v.version > 0]
        versions.sort(key=lambda item: item.version)
        return versions[-limit:] if limit else versions

    def latest_rule_version(self) -> RuleVersion | None:
        versions = self.list_rule_versions(limit=1)
        return versions[-1] if versions else None

    # -- trigger state ------------------------------------------------------

    def read_trigger_state(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.trigger_state_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def write_trigger_state(self, state: dict[str, Any]) -> None:
        with _WRITE_LOCK:
            atomic_write(
                self.trigger_state_file,
                json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )

    # -- environment model (user-owned) ------------------------------------

    def read_environment_model(self) -> str:
        try:
            return self.environment_file.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            return ""

    # -- external observation inbox ----------------------------------------

    def inbox_files(self) -> list[Path]:
        files: list[Path] = []
        for pattern in ("*.jsonl", "*.json"):
            try:
                files.extend(self.inbox_dir.glob(pattern))
            except OSError:
                continue
        return sorted(files)

    def move_inbox_file(self, path: Path, *, failed: bool = False) -> Path:
        target_dir = self.failed_dir if failed else self.processed_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name
        if target.exists():
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            target = target_dir / f"{path.stem}-{stamp}{path.suffix}"
        try:
            path.replace(target)
        except OSError:
            logger.opt(exception=True).warning("Navigator: could not archive {}", path)
        return target

    def read_inbox_file(self, path: Path) -> list[dict[str, Any]]:
        """Read one inbox file: JSONL or a JSON array/object."""
        try:
            text = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return []
        if path.suffix == ".jsonl":
            rows: list[dict[str, Any]] = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(value, dict):
                    rows.append(value)
            return rows
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return []
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        return [value] if isinstance(value, dict) else []
