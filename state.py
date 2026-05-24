"""Process-local routing state, the global router lock, and disk persistence.

``BROWSER_CDP_URL`` is process-global, so all mutations and all wrapped
browser calls must serialize through ``ROUTER_LOCK``.

There are three layers of state, looked up in priority order by the
wrappers:

1. **Per-session state** (``SESSION_STATE[session_key]``) — set by model
   tools (``browser_policy_set``, ``browser_policy_route``) and by
   slash commands invoked with an explicit ``<session-name>`` argument.
2. **Global default** (``GLOBAL_DEFAULT``) — set by slash commands
   invoked without an argument (``/browser-local``, ``/browser-cloud``,
   ``/browser-auto``).
3. **Auto routing** — URL classification when neither layer pins.

State is persisted to ``~/.hermes/state/browser-policy-router/state.json``
so per-session pins and the global default survive a gateway restart
**and** ``hermes plugins install --force`` (which rebuilds the plugin
checkout). A legacy ``.state.json`` next to the plugin from v1.0 installs
is auto-migrated on first read (see ``_state_path``). Persistence is
best-effort: a failed write is logged but does not abort the operation.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROUTER_LOCK: threading.RLock = threading.RLock()

SESSION_STATE: dict[str, dict[str, Any]] = {}


def _empty_session() -> dict[str, Any]:
    return {
        # "auto" or "pinned".
        "mode": "auto",
        # Engine when mode == "pinned".
        "pinned_engine": None,
        # One-shot engine forced by ``browser_policy_route(url, hint!=auto)``.
        # Consumed (and cleared) by the next ``browser_navigate`` on the
        # same session.
        "explicit_engine": None,
        # When True, ``decide_route`` ignores the GLOBAL_DEFAULT pin for
        # this session even though ``mode == "auto"``. Set by an explicit
        # ``browser_policy_set(engine="auto")`` or ``/browser-auto
        # <session-name>``; cleared as soon as the session is pinned
        # again. Fresh sessions default to False so they still inherit
        # the global default.
        "ignore_global": False,
        "last_engine": None,
        "last_url": None,
        "last_reason": None,
        "last_changed_at": None,
        "cdp_url": None,
    }


# ---------------------------------------------------------------------------
# Per-session state
# ---------------------------------------------------------------------------


def get_session(session_key: str) -> dict[str, Any]:
    state = SESSION_STATE.get(session_key)
    if state is None:
        state = _empty_session()
        SESSION_STATE[session_key] = state
    return state


def set_session(session_key: str, **fields: Any) -> dict[str, Any]:
    state = get_session(session_key)
    state.update(fields)
    return state


def reset_session(session_key: str) -> None:
    """Reset a session to defaults (mode=auto, follows global default)."""
    SESSION_STATE[session_key] = _empty_session()


# ---------------------------------------------------------------------------
# Global default (cross-session, slash-command driven)
# ---------------------------------------------------------------------------


GLOBAL_DEFAULT: dict[str, Any] = {
    "mode": "auto",
    "pinned_engine": None,
    "set_by": None,
    "changed_at": None,
}


def set_global_default(mode: str, pinned_engine: Any, set_by: str, when: str) -> None:
    GLOBAL_DEFAULT["mode"] = mode
    GLOBAL_DEFAULT["pinned_engine"] = pinned_engine
    GLOBAL_DEFAULT["set_by"] = set_by
    GLOBAL_DEFAULT["changed_at"] = when


def reset_global_default() -> None:
    GLOBAL_DEFAULT["mode"] = "auto"
    GLOBAL_DEFAULT["pinned_engine"] = None
    GLOBAL_DEFAULT["set_by"] = None
    GLOBAL_DEFAULT["changed_at"] = None


# ---------------------------------------------------------------------------
# Disk persistence
# ---------------------------------------------------------------------------


_STATE_FILENAME = "state.json"
# Default lives under ~/.hermes/state/browser-policy-router/ rather than
# inside the plugin checkout itself. Two reasons:
#   - `hermes plugins install --force` reinstalls the plugin directory
#     and could clobber the file, taking pinned-engine state with it.
#   - the file holds runtime state, not plugin source, so it doesn't
#     belong in a code path. Tests still override via set_persistence_path.
_LEGACY_STATE_FILENAME = ".state.json"
_PERSIST_PATH: Path | None = None


def _state_dir() -> Path:
    """Return ``~/.hermes/state/browser-policy-router/`` (created on demand)."""
    base = Path.home() / ".hermes" / "state" / "browser-policy-router"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _state_path() -> Path:
    """Default persistence path — under ``~/.hermes/state/...``.

    If a legacy ``.state.json`` is still sitting next to the plugin
    module (Phase 1 / v1.0 path), migrate it on first call and remove
    the legacy file so we don't end up reading two conflicting copies.
    """
    new_path = _state_dir() / _STATE_FILENAME
    legacy = Path(__file__).parent / _LEGACY_STATE_FILENAME
    if legacy.exists() and not new_path.exists():
        try:
            new_path.write_bytes(legacy.read_bytes())
            legacy.unlink()
            logger.info(
                "browser-policy-router: migrated legacy state %s -> %s",
                legacy,
                new_path,
            )
        except OSError as exc:  # pragma: no cover - filesystem issue
            logger.warning(
                "browser-policy-router: failed to migrate legacy state %s: %s",
                legacy,
                exc,
            )
    return new_path


def set_persistence_path(path: Path | None) -> None:
    """Override the persistence path (used by tests)."""
    global _PERSIST_PATH  # noqa: PLW0603 - module-scoped path override for tests
    _PERSIST_PATH = path


def _resolved_path() -> Path:
    return _PERSIST_PATH if _PERSIST_PATH is not None else _state_path()


def save_state() -> None:
    """Persist SESSION_STATE + GLOBAL_DEFAULT to disk.

    Best-effort: errors are logged, not raised. Atomic via tmp+rename so
    a crash mid-write does not corrupt the file. Caller is expected to
    hold ``ROUTER_LOCK`` to ensure a consistent snapshot.
    """
    path = _resolved_path()
    payload = {
        "version": 1,
        "global_default": dict(GLOBAL_DEFAULT),
        "sessions": {k: dict(v) for k, v in SESSION_STATE.items()},
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
        tmp.replace(path)
    except OSError as exc:  # pragma: no cover - disk error
        logger.warning("browser-policy-router: failed to persist state: %s", exc)


def load_state() -> None:
    """Restore SESSION_STATE + GLOBAL_DEFAULT from disk if available.

    Best-effort: a malformed file is logged and ignored (start fresh).
    Called once at plugin register() time.
    """
    path = _resolved_path()
    if not path.exists():
        return

    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("browser-policy-router: ignoring corrupt state file %s: %s", path, exc)
        return

    if not isinstance(data, dict):
        logger.warning("browser-policy-router: state file has unexpected shape; ignored")
        return

    gd = data.get("global_default")
    if isinstance(gd, dict):
        for key in ("mode", "pinned_engine", "set_by", "changed_at"):
            if key in gd:
                GLOBAL_DEFAULT[key] = gd[key]

    sessions = data.get("sessions")
    if isinstance(sessions, dict):
        SESSION_STATE.clear()
        for sk, sv in sessions.items():
            if not isinstance(sv, dict):
                continue
            merged = _empty_session()
            merged.update({k: v for k, v in sv.items() if k in merged})
            SESSION_STATE[sk] = merged
