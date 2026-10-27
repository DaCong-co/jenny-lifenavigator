"""Le manopole dei lavoratori periodici e del Life Navigator, per le Impostazioni.

Dream, il giardiniere e il Navigator sono i job di sistema registrati all'avvio
(``runtime/container.py``). Le prime due sezioni vivono in ``config.json`` sotto
``agents.defaults.{dream,gardener}`` piu'
``agents.defaults.compact_projects_when_idle``; il Navigator sta in
``agents.defaults.navigator`` ed e' l'unica di queste manopole che deve anche
ri-armare il proprio job quando cambia ``enabled`` o ``interval_min``.

Un comando e' un verbo: fa qualcosa adesso, in questa conversazione. Una
manopola e' una preferenza che sopravvive al turno, e sta dove stanno le altre
diciotto. Restano comandi ``/dream`` e ``/gardener`` — cioe' i due verbi, che
sono la ragione per cui i lavoratori sono collaudabili senza aspettare i loro
orologi.

**Sta su ``/api/`` e non sull'RPC** perche' non porta contenuto: numeri e
booleani stanno in una query string (v. la docstring di ``webui/commands.py``
per il confine). E sta in un modulo suo e non in ``settings_api.py``, che e'
gia' oltre 1.200 righe fra provider, onboarding, update e diagnostica
energetica.

Due cose che questo modulo deve fare e che sono facili da perdere:

- **I range non si riscrivono qui.** Si leggono dallo schema
  (:func:`_bounds`): un range scritto due volte diventa due range appena uno
  dei due si muove, ed e' proprio il numero che si racconta all'utente nel
  rifiuto.
- **Le letture non sollevano mai.** Aprono i file dell'utente, e una schermata
  Impostazioni che non si apre perche' ``SOUL.md`` e' illeggibile
  sarebbe un guasto peggiore di quello che sta segnalando — oltre a essere la
  schermata da cui si spengono i lavoratori.
"""

from __future__ import annotations

import os
from typing import Any

from loguru import logger

from jenny.channels.http_utils import parse_flag
from jenny.config import store
from jenny.config.loader import load_config
from jenny.config.schema import Config, DreamConfig, GardenerConfig, NavigatorConfig
from jenny.webui.settings_api import (
    WebUISettingsError,
    _apply_bool,
    _apply_int,
    _bounds,
    _parse_int,
    settings_payload,
)

QueryParams = dict[str, list[str]]

# Il pavimento operativo della cadenza di review. **Non e' nello schema**
# (``review_every_runs`` resta ``ge=1``) perche' un ``config.json`` restaurato
# deve poter riportare qualunque valore storico senza far fallire il parse: il
# pavimento e' una regola di questa superficie, non della struttura del file.
#
# Dodici perche' sotto quella soglia le passate di review si incontrano: la
# seconda atterra su file che la prima ha gia' potato e continua a cercare cose
# da togliere. Misurato sul device: due passate consecutive hanno portato
# ``USER.md`` da 3.524 a 1.626 caratteri, il 31% sulla sola seconda, e una
# passata forzata ha rimosso cinque voci vere — due domande aperte, un piano, un
# dettaglio biografico e un'osservazione. Era il pavimento di ``/dream budget
# review``, con una frase di conferma da ribattere; qui la conferma e' il flag
# ``confirm_back_to_back``, che il client alza solo dopo un dialogo.
REVIEW_CADENCE_FLOOR = 12

# Le chiavi che, se presenti, chiedono un ri-armo del job Navigator. Presenza e
# non cambio: ``register_system_job`` e' idempotente a intervallo identico, mentre
# perdersi la transizione di ``enabled`` lascerebbe il file acceso e il job no.
NAVIGATOR_REARM_KEYS = (
    "navigator_enabled",
    "navigatorEnabled",
    "navigator_interval_min",
    "navigatorIntervalMin",
)




# ── Lettura ──────────────────────────────────────────────────────────────────




def _number(model: type, attr: str, value: Any) -> dict[str, Any]:
    """Un numero col suo range, cosi' l'``<input type=number>`` non inventa i propri."""
    low, high = _bounds(model, attr)
    return {"value": int(value), "min": low, "max": high}


def _memory_files() -> list[dict[str, Any]] | None:
    """Le tre misure che ``/dream budget`` stampava, o ``None`` se non misurabili.

    ``None`` e non una lista vuota: "non ho potuto misurare" e "tre file da zero
    caratteri" sono due cose diverse, e la seconda va detta con dei numeri.

    L'ordine e' quello di ``budget_report`` (MEMORY, USER, SOUL) ed e' contratto.

    ``exists`` e ``readable`` stanno accanto a ``chars`` perche' ``count_chars``
    ritorna ``0`` in tre casi che non si somigliano: file vuoto, file mai
    scritto, file che non si riesce ad aprire. Il terzo, misurato sul telefono il
    31/08/2026 con un ``chmod 000``, leggeva «0 caratteri su 3.000» per un file
    da 2.407 byte: la schermata sopravviveva — che era il punto — ma quel numero
    era una bugia. Con ``readable`` la UI puo' dire "non misurabile" invece di
    inventare uno zero.
    """
    try:
        from jenny.agent.memory import MemoryStore
        from jenny.agent.memory_budget import budget_report

        config = load_config()
        dream = config.agents.defaults.dream
        memory = MemoryStore(config.workspace_path)
        report = budget_report(
            memory,
            memory_chars=dream.memory_budget_chars,
            user_chars=dream.user_budget_chars,
            soul_chars=dream.soul_budget_chars,
        )
    except Exception as exc:  # noqa: BLE001 — la schermata si apre comunque
        logger.warning("could not measure the memory files for settings: {}", exc)
        return None
    return [
        {
            "label": entry.label,
            "chars": entry.chars,
            "budget": entry.budget,
            "exists": entry.path.exists(),
            "readable": os.access(entry.path, os.R_OK),
        }
        for entry in report
    ]


def _review_state() -> dict[str, int] | None:
    """Stato del review pass: passate dall'ultima, e i due contatori di stallo."""
    try:
        from jenny.agent.memory import MemoryStore

        memory = MemoryStore(load_config().workspace_path)
        runs_since_review, stuck_runs = memory.get_review_state()
        return {
            "runs_since_review": runs_since_review,
            "stuck_runs": stuck_runs,
            "nothing_new_runs": memory.get_nothing_new_runs(),
        }
    except Exception as exc:  # noqa: BLE001 — v. _memory_files
        logger.warning("could not read the Dream review state: {}", exc)
        return None


def memory_settings_payload(config: Config | None = None) -> dict[str, Any]:
    """Dream e i tetti dei file di memoria lunga."""
    cfg = (config or load_config()).agents.defaults.dream
    return {
        "enabled": cfg.enabled,
        "interval_h": _number(DreamConfig, "interval_h", cfg.interval_h),
        "schedule": cfg.describe_schedule(),
        "memory_budget_chars": _number(
            DreamConfig, "memory_budget_chars", cfg.memory_budget_chars
        ),
        "user_budget_chars": _number(DreamConfig, "user_budget_chars", cfg.user_budget_chars),
        "soul_budget_chars": _number(DreamConfig, "soul_budget_chars", cfg.soul_budget_chars),
        "review_every_runs": _number(DreamConfig, "review_every_runs", cfg.review_every_runs),
        # Il pavimento viaggia col payload: il dialogo di conferma lo fa il
        # client, e senza il numero dovrebbe tenerne una copia sua.
        "review_floor": REVIEW_CADENCE_FLOOR,
        "files": _memory_files(),
        "review_state": _review_state(),
    }


def worker_settings_payload(config: Config | None = None) -> dict[str, Any]:
    """Il giardiniere e la compattazione delle chat di progetto."""
    defaults = (config or load_config()).agents.defaults
    gardener = defaults.gardener
    return {
        "gardener": {
            "enabled": gardener.enabled,
            "interval_min": _number(GardenerConfig, "interval_min", gardener.interval_min),
            "idle_min": _number(GardenerConfig, "idle_min", gardener.idle_min),
            "min_hours_between_passes": _number(
                GardenerConfig,
                "min_hours_between_passes",
                gardener.min_hours_between_passes,
            ),
            "schedule": gardener.describe_schedule(),
        },
        # Non e' del giardiniere (sta in ``agents.defaults``) ma e' la stessa
        # decisione vista dall'altro lato, e vale dal prossimo avvio: v.
        # ``update_worker_settings``.
        "compact_projects_when_idle": defaults.compact_projects_when_idle,
    }


# ── Life Navigator ──────────────────────────────────────────────────────────


def _navigator_status(config: Config | None = None) -> dict[str, Any] | None:
    """Read-only snapshot for the settings panel; never raises into the page."""
    try:
        from jenny.navigator.models import DIMENSION_KEYS
        from jenny.navigator.store import NavigatorStore

        workspace = (config or load_config()).workspace_path
        store = NavigatorStore(workspace)
        snapshot = store.latest_snapshot()
        state = store.read_trigger_state()
        notes = store.list_self_model_notes(limit=5)
        pending = store.pending_outcomes()
        dimensions: list[dict[str, Any]] = []
        if snapshot is not None:
            for key in DIMENSION_KEYS:
                dimension = snapshot.dimension(key)
                dimensions.append({
                    "key": key,
                    "change": dimension.change,
                    "facts": [fact[:220] for fact in dimension.facts[:3]],
                })
        return {
            "last_analysis": state.get("last_llm_at"),
            "last_priority": state.get("last_priority"),
            "snapshot_at": snapshot.timestamp if snapshot else None,
            "window": (
                f"{snapshot.window_start} .. {snapshot.window_end}"
                if snapshot else ""
            ),
            "dimensions": dimensions,
            "pending": len(pending),
            "self_model": [note.statement[:260] for note in notes],
        }
    except Exception:
        logger.exception("could not read the Navigator status")
        return None


def navigator_settings_payload(config: Config | None = None) -> dict[str, Any] | None:
    """The Navigator's knobs, plus a read-only state summary for the panel."""
    try:
        cfg = (config or load_config()).agents.defaults.navigator
        return {
            "enabled": cfg.enabled,
            "schedule": cfg.describe_schedule(),
            "interval_min": _number(NavigatorConfig, "interval_min", cfg.interval_min),
            "min_minutes_between_analyses": _number(
                NavigatorConfig,
                "min_minutes_between_analyses",
                cfg.min_minutes_between_analyses,
            ),
            "max_analyses_per_day": _number(
                NavigatorConfig, "max_analyses_per_day", cfg.max_analyses_per_day,
            ),
            "quiet_start_hour": _number(
                NavigatorConfig, "quiet_start_hour", cfg.quiet_start_hour,
            ),
            "quiet_end_hour": _number(
                NavigatorConfig, "quiet_end_hour", cfg.quiet_end_hour,
            ),
            "observation_window_days": _number(
                NavigatorConfig, "observation_window_days", cfg.observation_window_days,
            ),
            "outcome_window_hours": _number(
                NavigatorConfig, "outcome_window_hours", cfg.outcome_window_hours,
            ),
            "drift_min_days": _number(
                NavigatorConfig, "drift_min_days", cfg.drift_min_days,
            ),
            "dismissal_cooldown_hours": _number(
                NavigatorConfig, "dismissal_cooldown_hours", cfg.dismissal_cooldown_hours,
            ),
            "watch_topics": list(cfg.watch_topics),
            "capture_conversation": bool(cfg.capture_conversation),
            "capture_conversation_text": bool(cfg.capture_conversation_text),
            "capture_notifications": bool(cfg.capture_notifications),
            "capture_app_usage": bool(cfg.capture_app_usage),
            "capture_health": bool(cfg.capture_health),
            "capture_location": bool(cfg.capture_location),
            "capture_screen": bool(cfg.capture_screen),
            "capture_system": bool(cfg.capture_system),
            "redact_sensitive_content": bool(cfg.redact_sensitive_content),
            "sensitive_packages": list(cfg.sensitive_packages),
            "blocked_packages": list(cfg.blocked_packages),
            "status": _navigator_status(config),
        }
    except Exception:
        logger.exception("could not build the Navigator settings section")
        return None


def _parse_list(raw: str, *, limit: int = 100) -> list[str]:
    import re

    items: list[str] = []
    for part in re.split(r"[\n,;|，；]+", raw or ""):
        value = part.strip()
        if value and value not in items:
            items.append(value[:80])
        if len(items) >= limit:
            break
    return items


def _apply_list(
    query: QueryParams,
    target: Any,
    attr: str,
    *names: str,
) -> bool:
    """Patch a string list; an explicit ``*_clear`` empties it.

    An empty textarea cannot travel as a query value (``_postWithQuery`` drops
    empty strings), so clearing needs a flag of its own rather than an empty
    ``watch_topics=`` that the client would never send.
    """
    clear = any(
        parse_flag(_first(query, f"{name}_clear", f"{name}Clear") or "")
        for name in names
    )
    raw = _first(query, *names)
    if raw is None and not clear:
        return False
    value = [] if clear else _parse_list(raw or "")
    if list(getattr(target, attr)) == value:
        return False
    setattr(target, attr, value)
    return True


async def update_navigator_settings(query: QueryParams) -> dict[str, Any]:
    """Write the Navigator's knobs through the config funnel."""

    def _apply(config: Config) -> bool:
        nav = config.agents.defaults.navigator
        changed = _apply_bool(
            query, nav, "enabled", "navigator_enabled", "navigatorEnabled",
        )
        for attr, snake, camel in (
            ("interval_min", "navigator_interval_min", "navigatorIntervalMin"),
            (
                "min_minutes_between_analyses",
                "navigator_min_minutes_between_analyses",
                "navigatorMinMinutesBetweenAnalyses",
            ),
            (
                "max_analyses_per_day",
                "navigator_max_analyses_per_day",
                "navigatorMaxAnalysesPerDay",
            ),
            ("quiet_start_hour", "navigator_quiet_start_hour", "navigatorQuietStartHour"),
            ("quiet_end_hour", "navigator_quiet_end_hour", "navigatorQuietEndHour"),
            (
                "observation_window_days",
                "navigator_observation_window_days",
                "navigatorObservationWindowDays",
            ),
            (
                "outcome_window_hours",
                "navigator_outcome_window_hours",
                "navigatorOutcomeWindowHours",
            ),
            ("drift_min_days", "navigator_drift_min_days", "navigatorDriftMinDays"),
            (
                "dismissal_cooldown_hours",
                "navigator_dismissal_cooldown_hours",
                "navigatorDismissalCooldownHours",
            ),
        ):
            changed |= _apply_int(query, nav, attr, NavigatorConfig, snake, camel)

        for attr, snake, camel in (
            (
                "capture_conversation",
                "navigator_capture_conversation",
                "navigatorCaptureConversation",
            ),
            (
                "capture_conversation_text",
                "navigator_capture_conversation_text",
                "navigatorCaptureConversationText",
            ),
            (
                "capture_notifications",
                "navigator_capture_notifications",
                "navigatorCaptureNotifications",
            ),
            (
                "capture_app_usage",
                "navigator_capture_app_usage",
                "navigatorCaptureAppUsage",
            ),
            ("capture_health", "navigator_capture_health", "navigatorCaptureHealth"),
            (
                "capture_location",
                "navigator_capture_location",
                "navigatorCaptureLocation",
            ),
            ("capture_screen", "navigator_capture_screen", "navigatorCaptureScreen"),
            ("capture_system", "navigator_capture_system", "navigatorCaptureSystem"),
            (
                "redact_sensitive_content",
                "navigator_redact_sensitive_content",
                "navigatorRedactSensitiveContent",
            ),
        ):
            changed |= _apply_bool(query, nav, attr, snake, camel)

        changed |= _apply_list(
            query, nav, "watch_topics",
            "navigator_watch_topics", "navigatorWatchTopics",
        )
        changed |= _apply_list(
            query, nav, "sensitive_packages",
            "navigator_sensitive_packages", "navigatorSensitivePackages",
        )
        changed |= _apply_list(
            query, nav, "blocked_packages",
            "navigator_blocked_packages", "navigatorBlockedPackages",
        )
        return changed

    await store.mutate(_apply)
    return settings_payload()


async def navigator_access_payload() -> dict[str, Any]:
    """Notification/usage access state for the panel; read-only."""
    try:
        from jenny.navigator.android_perception import android_access_status

        return {"access": await android_access_status()}
    except Exception:
        logger.exception("could not read Android perception access")
        return {"access": None}


async def open_navigator_access(query: QueryParams) -> dict[str, Any]:
    """Open one Android system access screen, then return the fresh state."""
    from jenny.navigator.android_perception import (
        android_access_status,
        open_android_access_settings,
    )

    kind = (_first(query, "kind") or "usage").strip().lower()
    if kind not in ("notifications", "usage"):
        raise WebUISettingsError("kind must be 'notifications' or 'usage'")
    opened = await open_android_access_settings(kind)
    return {
        "opened": bool(opened),
        "access": await android_access_status(),
    }



# ── Scrittura ────────────────────────────────────────────────────────────────


def _first(query: QueryParams, *names: str) -> str | None:
    """Il primo valore fra piu' nomi accettati (snake_case e camelCase)."""
    for name in names:
        values = query.get(name)
        if values:
            return values[0]
    return None


def _flag(query: QueryParams, *names: str) -> bool:
    """Un flag di conferma: vero solo se dichiarato esplicitamente vero."""
    return parse_flag(_first(query, *names))










# Le chiavi che, se presenti, chiedono un ri-armo del job di quel lavoratore.
# **Presenza e non cambio**: ``register_system_job`` e' idempotente e riparte da
# zero solo se la pianificazione e' cambiata, quindi ri-armare a intervallo
# identico non sposta la prossima scadenza; mentre perdersi la transizione di
# ``enabled`` lascerebbe scritto nel file un valore che nessun job va a leggere
# (su un gateway partito col lavoratore spento il job non e' registrato).
MEMORY_REARM_KEYS = ("dream_enabled", "dreamEnabled", "dream_interval_h", "dreamIntervalH")
GARDENER_REARM_KEYS = (
    "gardener_enabled",
    "gardenerEnabled",
    "gardener_interval_min",
    "gardenerIntervalMin",
)


async def update_memory_settings(query: QueryParams) -> dict[str, Any]:
    """Scrive le manopole di Dream e i tre tetti della memoria lunga.

    Patch e non PUT: un campo assente dalla query non si tocca. Un valore
    identico lascia ``config.json`` intatto e non fa ruotare il ``.bak`` — e'
    quel che il comando faceva, ed e' pinnato dai suoi test.
    """
    review_raw = _first(query, "review_every_runs", "reviewEveryRuns")
    if review_raw is not None:
        # Validato **prima** di ``mutate``: un valore rifiutato non deve toccare
        # il file ne' far ruotare il backup.
        review = _parse_int(review_raw, "review_every_runs", DreamConfig, "review_every_runs")
        if review < REVIEW_CADENCE_FLOOR and not _flag(
            query, "confirm_back_to_back", "confirmBackToBack"
        ):
            raise WebUISettingsError(
                f"a review cadence below {REVIEW_CADENCE_FLOOR} runs needs an explicit "
                "confirmation: below it the review passes land on files a previous pass has "
                "already pruned, and keep looking for things to remove"
            )

    def _apply(config: Config) -> bool:
        dream = config.agents.defaults.dream
        changed = _apply_bool(query, dream, "enabled", "dream_enabled", "dreamEnabled")
        changed |= _apply_int(
            query, dream, "interval_h", DreamConfig, "dream_interval_h", "dreamIntervalH"
        )
        for attr, snake, camel in (
            ("memory_budget_chars", "memory_budget_chars", "memoryBudgetChars"),
            ("user_budget_chars", "user_budget_chars", "userBudgetChars"),
            ("soul_budget_chars", "soul_budget_chars", "soulBudgetChars"),
            ("review_every_runs", "review_every_runs", "reviewEveryRuns"),
        ):
            changed |= _apply_int(query, dream, attr, DreamConfig, snake, camel)
        return changed

    await store.mutate(_apply)
    return settings_payload()


async def update_worker_settings(query: QueryParams) -> dict[str, Any]:
    """Scrive le manopole del giardiniere e la compattazione progetti.

    ``compact_projects_when_idle`` e' la sola che non vale subito: la legge
    l'agente quando parte, quindi la risposta alza ``requires_restart`` e la UI
    lo dice sul posto invece che in un toast che scorre via.
    """
    seen: dict[str, bool] = {}

    def _apply(config: Config) -> bool:
        defaults = config.agents.defaults
        gardener = defaults.gardener
        changed = _apply_bool(
            query, gardener, "enabled", "gardener_enabled", "gardenerEnabled"
        )
        for attr, snake, camel in (
            ("interval_min", "gardener_interval_min", "gardenerIntervalMin"),
            ("idle_min", "gardener_idle_min", "gardenerIdleMin"),
            (
                "min_hours_between_passes",
                "gardener_min_hours_between_passes",
                "gardenerMinHoursBetweenPasses",
            ),
        ):
            changed |= _apply_int(query, gardener, attr, GardenerConfig, snake, camel)
        compact = _apply_bool(
            query,
            defaults,
            "compact_projects_when_idle",
            "compact_projects_when_idle",
            "compactProjectsWhenIdle",
        )
        seen["restart"] = compact
        return changed or compact

    await store.mutate(_apply)
    return settings_payload(requires_restart=seen.get("restart", False))
