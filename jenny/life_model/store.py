"""Life Model store: pure file I/O for the Life Navigator state-model layer.

Parallel to ``agent.memory.MemoryStore`` but semantically isolated: it never
imports MemoryStore / Consolidator / entry_archiver, holds no cursor, and owns
``life_model/`` under the workspace and nothing else. The only shared primitive
is ``utils.path.atomic_write`` (low-level file I/O, not memory-upgrade logic).

File model (see ``docs/lifenavigator-v1.5-spec.md`` §4.1):

    life_model/
    ├── life_goals.md          # user-owned, read-only here
    ├── current_state.md       # Dream-written snapshot (full overwrite)
    ├── events/<ts>-<hash>.md  # agent-written, append-only, one file per event
    ├── pattern_candidates.md  # Dream-written entry collection
    ├── patterns.md            # user-owned, read-only here
    └── expression.md          # user-owned, read-only here
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from jenny.utils.path import atomic_write

_ID_CHARS = 8
_SEPARATOR = "---"

# Growth governance: strategy first, exact values tuned later (spec §4.1).
# ``growth_notice()`` emits a signal only; consolidation is Dream's job.
LIFE_MODEL_PATTERN_CANDIDATE_LIMIT = 50
LIFE_MODEL_EVENT_WARNING_THRESHOLD = 300

_VALID_CANDIDATE_STATUSES = frozenset({"observing", "supported", "rejected"})
_VALID_EVENT_KINDS = frozenset({
    "goal_change", "setback", "milestone", "decision", "state_change", "other",
})

# Event filenames are "<YYYYMMDD>-<HHMMSS>-<8 hex>.md". read_event() rejects
# anything that is not exactly that shape, so a model/user-supplied id can never
# escape events/ via path traversal.
_EVENT_STEM = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$")


def _content_id(text: str) -> str:
    """Stable short id, a function of content only (same convention as memory_entries)."""
    normalized = "\n".join(line.rstrip() for line in text.strip().splitlines())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:_ID_CHARS]


@dataclass(frozen=True, slots=True)
class EventRef:
    id: str          # filename stem, e.g. "20260908-143000-ab12cd34"
    timestamp: str   # "20260908-143000"
    kind: str
    first_line: str
    size: int


@dataclass(frozen=True, slots=True)
class Candidate:
    id: str
    text: str
    evidence: tuple[str, ...]
    status: str
    confidence: str
    created: str = ""
    observed_period: str = ""


def _split_blocks(text: str) -> list[str]:
    """Split a ``---``-delimited entry file into per-entry blocks.

    Tolerant like ``memory_archive._parse_archived``: a trailing unterminated
    block is still returned rather than silently dropped.
    """
    blocks: list[str] = []
    current: list[str] = []
    in_block = False
    for line in text.splitlines():
        if line.strip() == _SEPARATOR:
            if in_block:
                blocks.append("\n".join(current).strip())
                current = []
                in_block = False
            else:
                in_block = True
        elif in_block:
            current.append(line)
    if in_block and current:
        blocks.append("\n".join(current).strip())
    return blocks


class LifeModelStore:
    """Pure file I/O for the Life Navigator state model under ``life_model/``."""

    def __init__(self, workspace: Path):
        workspace = Path(workspace)
        self.workspace = workspace
        self.life_model_dir = workspace / "life_model"
        self.life_model_dir.mkdir(parents=True, exist_ok=True)
        self.events_dir = self.life_model_dir / "events"
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.life_goals_file = self.life_model_dir / "life_goals.md"
        self.current_state_file = self.life_model_dir / "current_state.md"
        self.patterns_file = self.life_model_dir / "patterns.md"
        self.expression_file = self.life_model_dir / "expression.md"
        self.pattern_candidates_file = self.life_model_dir / "pattern_candidates.md"

    # -- read-only files (injected into context) ----------------------------

    @staticmethod
    def _read_file(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return ""

    def read_life_goals(self) -> str:
        return self._read_file(self.life_goals_file)

    def read_current_state(self) -> str:
        return self._read_file(self.current_state_file)

    def read_patterns(self) -> str:
        return self._read_file(self.patterns_file)

    def read_expression(self) -> str:
        return self._read_file(self.expression_file)

    def read_context_files(self) -> dict[str, str]:
        """The four files safe to inject; ContextBuilder decides how to render them."""
        return {
            "life_goals": self.read_life_goals(),
            "current_state": self.read_current_state(),
            "patterns": self.read_patterns(),
            "expression": self.read_expression(),
        }

    # -- events (append-only, one file per event) ---------------------------

    def write_event(self, content: str, *, kind: str, source_turn: str | None = None) -> str:
        """Write one event file and return its stable id (the filename stem).

        Events are occurrences, not deduplicated facts: a repeated event still
        becomes a new file. ``kind`` is a whitelist; ``source_turn`` is optional
        provenance.
        """
        body = (content or "").strip()
        if not body:
            raise ValueError("event content is empty")
        if kind not in _VALID_EVENT_KINDS:
            raise ValueError(f"unknown event kind {kind!r}")
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        event_id = _content_id(f"{timestamp}:{body}")
        filename = f"{timestamp}-{event_id}.md"
        fields = [
            ("id", event_id),
            ("timestamp", timestamp),
            ("kind", kind),
            ("source_turn", source_turn or ""),
        ]
        head = [f"{k}: {v}" for k, v in fields if v]
        rendered = "\n".join([_SEPARATOR, *head, _SEPARATOR, "", body, ""])
        atomic_write(self.events_dir / filename, rendered)
        return filename[:-3]

    def list_events(self) -> list[EventRef]:
        """All events, newest first. Reads metadata only, not full bodies."""
        try:
            paths = sorted(self.events_dir.glob("*.md"), reverse=True)
        except OSError:
            return []
        refs: list[EventRef] = []
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8")
                size = path.stat().st_size
            except OSError:
                continue
            fields, body = self._parse_event(text)
            stem = path.stem
            parts = stem.split("-")
            timestamp = "-".join(parts[:2]) if len(parts) >= 2 else fields.get("timestamp", stem)
            first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
            refs.append(EventRef(
                id=stem,
                timestamp=timestamp,
                kind=fields.get("kind", "other"),
                first_line=first,
                size=size,
            ))
        return refs

    def read_event(self, event_id: str) -> str:
        """Return the body of one event by its id (filename stem)."""
        if not _EVENT_STEM.match(event_id):
            raise ValueError(f"invalid event id {event_id!r}")
        try:
            text = (self.events_dir / f"{event_id}.md").read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return ""
        _, body = self._parse_event(text)
        return body

    @staticmethod
    def _parse_event(text: str) -> tuple[dict[str, str], str]:
        lines = text.splitlines()
        if not lines or lines[0].strip() != _SEPARATOR:
            return {}, text.strip()
        fields: dict[str, str] = {}
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == _SEPARATOR:
                return fields, "\n".join(lines[i + 1:]).strip()
            key, sep, value = line.partition(":")
            if sep:
                fields[key.strip()] = value.strip()
        return fields, ""

    # -- current_state (full snapshot) --------------------------------------

    def update_current_state(self, content: str) -> None:
        atomic_write(self.current_state_file, (content or "").strip() + "\n")

    # -- pattern candidates (entry collection) ------------------------------

    def list_candidates(self) -> list[Candidate]:
        text = self._read_file(self.pattern_candidates_file)
        if not text.strip():
            return []
        candidates: list[Candidate] = []
        for block in _split_blocks(text):
            candidate = self._parse_candidate_block(block)
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    def add_pattern_candidate(
        self,
        candidate: str,
        evidence: list[str] | tuple[str, ...],
        *,
        observed_period: str = "",
        confidence: str = "low",
    ) -> str:
        """Append a candidate entry and return its id. Idempotent by content."""
        text = (candidate or "").strip()
        if not text:
            raise ValueError("candidate text is empty")
        candidate_id = _content_id(text)
        entry = Candidate(
            id=candidate_id,
            text=text,
            evidence=tuple(e.strip() for e in evidence if (e or "").strip()),
            status="observing",
            confidence=confidence,
            created=datetime.now().strftime("%Y-%m-%d"),
            observed_period=observed_period.strip(),
        )
        existing = self.list_candidates()
        if any(c.id == candidate_id for c in existing):
            return candidate_id
        existing.append(entry)
        self._write_candidates(existing)
        return candidate_id

    def update_pattern_candidate_status(self, candidate_id: str, status: str) -> None:
        if status not in _VALID_CANDIDATE_STATUSES:
            raise ValueError(f"unknown candidate status {status!r}")
        candidates = self.list_candidates()
        for i, c in enumerate(candidates):
            if c.id == candidate_id:
                candidates[i] = replace(c, status=status)
                break
        else:
            raise KeyError(f"no candidate with id {candidate_id!r}")
        self._write_candidates(candidates)

    def remove_pattern_candidate(self, candidate_id: str) -> None:
        candidates = self.list_candidates()
        kept = [c for c in candidates if c.id != candidate_id]
        if len(kept) == len(candidates):
            raise KeyError(f"no candidate with id {candidate_id!r}")
        self._write_candidates(kept)

    @staticmethod
    def _parse_candidate_block(text: str) -> Candidate | None:
        fields: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            key, sep, value = line.partition(":")
            if sep:
                fields[key.strip()] = value.strip()
        candidate_id = fields.get("id", "")
        candidate = fields.get("candidate", "")
        if not candidate_id or not candidate:
            return None
        evidence = tuple(e.strip() for e in fields.get("evidence", "").split("|") if e.strip())
        return Candidate(
            id=candidate_id,
            text=candidate,
            evidence=evidence,
            status=fields.get("status", "observing"),
            confidence=fields.get("confidence", "medium"),
            created=fields.get("created", ""),
            observed_period=fields.get("observed_period", ""),
        )

    @staticmethod
    def _render_candidate(candidate: Candidate) -> str:
        fields = [
            ("id", candidate.id),
            ("status", candidate.status),
            ("created", candidate.created),
            ("observed_period", candidate.observed_period),
            ("confidence", candidate.confidence),
        ]
        head = [f"{k}: {v}" for k, v in fields if v]
        body = [f"candidate: {candidate.text}"]
        if candidate.evidence:
            body.append("evidence: " + " | ".join(candidate.evidence))
        return "\n".join([_SEPARATOR, *head, *body, _SEPARATOR, ""])

    def _write_candidates(self, candidates: list[Candidate]) -> None:
        if not candidates:
            atomic_write(self.pattern_candidates_file, "")
            return
        blocks = [self._render_candidate(c) for c in candidates]
        atomic_write(self.pattern_candidates_file, "\n".join(blocks))

    # -- growth governance --------------------------------------------------

    def growth_notice(self) -> str | None:
        """Emit a signal when the model approaches its growth limits.

        Never deletes anything itself: candidates trigger a consolidation signal,
        events trigger an archiving-policy review signal. Dream's prompt decides
        what to do with the signal.
        """
        notices: list[str] = []
        n_candidates = len(self.list_candidates())
        if n_candidates >= LIFE_MODEL_PATTERN_CANDIDATE_LIMIT:
            notices.append(
                f"pattern_candidates holds {n_candidates} candidates "
                f"(>= {LIFE_MODEL_PATTERN_CANDIDATE_LIMIT}); merge duplicates and drop "
                "low-confidence candidates"
            )
        try:
            n_events = sum(1 for _ in self.events_dir.glob("*.md"))
        except OSError:
            n_events = 0
        if n_events >= LIFE_MODEL_EVENT_WARNING_THRESHOLD:
            notices.append(
                f"events/ holds {n_events} events "
                f"(>= {LIFE_MODEL_EVENT_WARNING_THRESHOLD}); review archiving policy "
                "(events are never auto-deleted)"
            )
        return "; ".join(notices) if notices else None
