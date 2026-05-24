"""Slash command handlers.

The gateway does not pass a session id to slash command handlers, so these
commands operate on the cross-session **global default** by default. If the
caller supplies a session name as the slash argument, the command resolves
it via :class:`hermes_state.SessionDB` and writes the pin to that session
specifically — the gateway invokes browser tools with ``task_id=session_id``
(``gateway/run.py``: ``run_conversation(..., task_id=session_id)``), so the
wrapper consults ``SESSION_STATE[session_id]`` and the pin takes effect on
the next browser tool call from that session.

Note: per-session pins do **not** mutate ``BROWSER_CDP_URL`` / ``CAMOFOX_URL``
eagerly — those envs are process-global and changing them could interfere
with another session that happens to be running a browser tool right now.
The wrapper will apply the engine lazily under ``ROUTER_LOCK`` the next time
the target session dispatches a browser tool.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from . import routing, state

logger = logging.getLogger(__name__)

_ctx: Any = None


def set_plugin_context(ctx: Any) -> None:
    # ``_ctx`` is module-scoped so handlers can access it without
    # re-plumbing through every command signature.
    global _ctx  # noqa: PLW0603
    _ctx = ctx


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Session resolution
# ---------------------------------------------------------------------------


def _resolve_session_id(name_or_id: str) -> tuple[str | None, str]:  # noqa: PLR0911
    """Resolve a slash-command argument to a session id.

    Returns ``(session_id, diagnostic)``. ``session_id`` is ``None`` when the
    name cannot be resolved; ``diagnostic`` is a short human-readable note.

    Tries:
      1. Exact session-id match (32-hex or ``YYYYMMDD_...`` shapes).
      2. ``SessionDB.resolve_session_by_title`` for title-based lookups.

    We import ``hermes_state`` lazily because slash commands without a name
    arg never need it, and we want plugin import to remain side-effect free.
    """
    name = (name_or_id or "").strip()
    if not name:
        return None, "no session name given"

    try:
        from hermes_state import (  # type: ignore[import-not-found,reportMissingImports]
            SessionDB,
        )
    except ImportError as exc:
        return None, f"hermes_state not importable: {exc}"

    try:
        db = SessionDB()
    except Exception as exc:  # pragma: no cover - defensive
        return None, f"SessionDB unavailable: {exc}"

    # If it already looks like a raw session id, prefer that path.
    try:
        if hasattr(db, "resolve_session_id"):
            resolved = db.resolve_session_id(name)
            if resolved:
                return resolved, f"resolved id prefix {name!r}"
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("resolve_session_id failed for %r: %s", name, exc)

    try:
        resolved = db.resolve_session_by_title(name)
    except Exception as exc:  # pragma: no cover - defensive
        return None, f"resolve_session_by_title failed: {exc}"

    if not resolved:
        return None, f"no session matches title {name!r}"
    return resolved, f"resolved title {name!r}"


# ---------------------------------------------------------------------------
# /browser-status [session-name]
# ---------------------------------------------------------------------------


def cmd_browser_status(raw: str) -> str:  # noqa: PLR0915
    # The status dump intentionally lays out Chrome and Camofox readiness on
    # separate lines, plus optional session-specific fields, so the linear
    # statement count is high. Splitting would obscure the rendering order.
    config = routing.load_config(reload=True)

    # Optional session-name argument: show that session's pin (if any).
    target_id: str | None = None
    diag = ""
    arg = (raw or "").strip()
    if arg:
        target_id, diag = _resolve_session_id(arg)

    with state.ROUTER_LOCK:
        local_cfg = config.get("local_profile", {}) or {}
        local_cdp = local_cfg.get("cdp_url", "http://127.0.0.1:9222")
        cdp_ok = routing.cdp_ready(local_cdp)
        agent_state = routing.launchctl_state(local_cfg.get("launchctl_label", ""))
        cdp_env_set = bool(os.environ.get("BROWSER_CDP_URL"))

        camo_cfg = config.get("camofox", {}) or {}
        camo_health = routing._camofox_health_url(camo_cfg)
        camo_ok = routing.camofox_ready(camo_health) if camo_health else False
        colima_state = routing.launchctl_state(camo_cfg.get("colima_launchctl_label", ""))
        camo_env_set = bool(os.environ.get("CAMOFOX_URL"))

        gd = dict(state.GLOBAL_DEFAULT)
        global_mode = gd.get("mode", "auto")
        global_pin = gd.get("pinned_engine") or "none"
        global_set_by = gd.get("set_by") or "(unset)"
        global_changed_at = gd.get("changed_at") or "(never)"

        active_pins = [
            (sk, st.get("pinned_engine"))
            for sk, st in state.SESSION_STATE.items()
            if st.get("mode") == "pinned"
        ]

        target_state = None
        if target_id is not None and target_id in state.SESSION_STATE:
            target_state = dict(state.SESSION_STATE[target_id])

    lines = [
        "Browser policy router",
        f"global mode: {global_mode}",
        f"global pinned: {global_pin}",
        f"global set by: {global_set_by}",
        f"global changed at: {global_changed_at}",
        f"local CDP: {'OK' if cdp_ok else 'DOWN'} {local_cdp}",
        f"  Chrome LaunchAgent: {agent_state}",
        f"  BROWSER_CDP_URL: {'set' if cdp_env_set else 'unset'}",
        f"Camofox: {'OK' if camo_ok else 'DOWN'} {camo_cfg.get('url', '(unconfigured)')}",
    ]
    no_vnc = camo_cfg.get("no_vnc_url")
    if no_vnc:
        lines.append(f"  noVNC: {no_vnc}")
    if camo_cfg.get("colima_launchctl_label"):
        lines.append(f"  Colima LaunchDaemon: {colima_state}")
    if camo_cfg.get("docker_container"):
        lines.append(f"  Docker container: {camo_cfg.get('docker_container')}")
    lines.append(f"  CAMOFOX_URL: {'set' if camo_env_set else 'unset'}")
    lines.append(f"cloud expected: {routing.cloud_provider_expected(config)}")
    if active_pins:
        lines.append("session pins:")
        for sk, pin in active_pins:
            lines.append(f"  {sk}: {pin}")
    if arg:
        lines.append("")
        lines.append(f"queried session: {arg}")
        lines.append(f"  resolution: {diag}")
        if target_state:
            lines.append(f"  mode: {target_state.get('mode')}")
            lines.append(f"  pinned_engine: {target_state.get('pinned_engine')}")
            lines.append(f"  explicit_engine: {target_state.get('explicit_engine')}")
            lines.append(f"  ignore_global: {target_state.get('ignore_global')}")
            lines.append(f"  last_engine: {target_state.get('last_engine')}")
            lines.append(f"  last_url: {target_state.get('last_url')}")
            lines.append(f"  last_reason: {target_state.get('last_reason')}")
        elif target_id is not None:
            lines.append("  (no per-session pin set; session inherits global)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /browser-auto [session-name]
# ---------------------------------------------------------------------------


def cmd_browser_auto(raw: str) -> str:
    arg = (raw or "").strip()
    if not arg:
        with state.ROUTER_LOCK:
            state.reset_global_default()
            state.save_state()
        logger.info("browser-policy-router global mode=auto source=/browser-auto")
        return "Browser policy (global): auto (URL-driven)"

    session_id, diag = _resolve_session_id(arg)
    if session_id is None:
        return f"Could not resolve session {arg!r}: {diag}"

    with state.ROUTER_LOCK:
        session = state.get_session(session_id)
        session["mode"] = "auto"
        session["pinned_engine"] = None
        session["explicit_engine"] = None
        # Explicit auto for a named session ignores the global default —
        # this is the URL-driven escape hatch the user asked for.
        session["ignore_global"] = True
        global_pin = state.GLOBAL_DEFAULT.get("pinned_engine")
        global_note = (
            f"; overriding global pin ({global_pin})"
            if state.GLOBAL_DEFAULT.get("mode") == "pinned"
            else ""
        )
        state.save_state()

    logger.info(
        "browser-policy-router session=%s mode=auto ignore_global=True source=/browser-auto (%s)",
        session_id,
        diag,
    )
    return f"Browser policy for session {session_id!r}: auto (URL-driven){global_note}"


# ---------------------------------------------------------------------------
# /browser-local [session-name] (alias of /browser-chrome — kept for compat)
# /browser-chrome [session-name]
# ---------------------------------------------------------------------------


def cmd_browser_local(raw: str) -> str:
    """Pin to local Chrome (profile:main). Alias kept for Phase 1 compat.

    The Phase 3 plan introduces ``/browser-chrome`` as the explicit name
    and ``/browser-camofox`` for the Docker engine; ``/browser-local`` is
    intentionally **not** repointed at Camofox to avoid surprising users
    with muscle memory. New code should prefer ``/browser-chrome``.
    """
    return _pin_chrome(raw, source="/browser-local")


def cmd_browser_chrome(raw: str) -> str:
    """Pin to local Chrome (profile:main) — Phase 1 GUI-bound engine."""
    return _pin_chrome(raw, source="/browser-chrome")


def _pin_chrome(raw: str, source: str) -> str:
    config = routing.load_config(reload=True)
    arg = (raw or "").strip()

    if not arg:
        # Global default + eager env mutation (slash command is interactive).
        with state.ROUTER_LOCK:
            recovery = routing.ensure_local_profile(config)
            if not recovery.ok:
                return f"Local Chrome unavailable: {recovery.reason}"
            local_cfg = config.get("local_profile", {}) or {}
            route = routing.Route(
                engine="profile:main",
                cdp_url=local_cfg.get("cdp_url", "http://127.0.0.1:9222"),
                reason=f"pinned by {source}",
            )
            routing.cleanup_browsers()
            routing.apply_engine(route)
            state.set_global_default(
                mode="pinned",
                pinned_engine="profile:main",
                set_by=source,
                when=_now(),
            )
            state.save_state()
        suffix = " (recovered)" if recovery.recovered else ""
        return f"Browser policy (global): pinned to local Chrome profile{suffix}"

    session_id, diag = _resolve_session_id(arg)
    if session_id is None:
        return f"Could not resolve session {arg!r}: {diag}"

    # Per-session pin: do NOT mutate BROWSER_CDP_URL eagerly — env is
    # process-global and could be in use by another session. The wrapper
    # applies the engine under ROUTER_LOCK on this session's next call.
    recovery = routing.ensure_local_profile(config)
    if not recovery.ok:
        return f"Local Chrome unavailable: {recovery.reason}"

    local_cfg = config.get("local_profile", {}) or {}
    cdp_url = local_cfg.get("cdp_url", "http://127.0.0.1:9222")
    with state.ROUTER_LOCK:
        session = state.get_session(session_id)
        session["mode"] = "pinned"
        session["pinned_engine"] = "profile:main"
        session["explicit_engine"] = None
        # Pinning replaces any prior auto-override on this session.
        session["ignore_global"] = False
        session["cdp_url"] = cdp_url
        session["last_reason"] = f"pinned by {source} {arg}"
        session["last_changed_at"] = _now()
        state.save_state()

    suffix = " (recovered local Chrome)" if recovery.recovered else ""
    return f"Browser policy for session {session_id!r}: pinned to local Chrome profile{suffix}"


# ---------------------------------------------------------------------------
# /browser-camofox [session-name]
# ---------------------------------------------------------------------------


def cmd_browser_camofox(raw: str) -> str:
    """Pin to durable local Camofox (camofox:main) — Phase 3 Docker engine."""
    config = routing.load_config(reload=True)
    arg = (raw or "").strip()

    camo_cfg = config.get("camofox", {}) or {}
    no_vnc = camo_cfg.get("no_vnc_url") or ""
    vnc_note = f" (noVNC: {no_vnc})" if no_vnc else ""

    if not arg:
        with state.ROUTER_LOCK:
            recovery = routing.ensure_camofox(config)
            if not recovery.ok:
                return f"Camofox unavailable: {recovery.reason}"
            route = routing.Route(
                engine="camofox:main",
                reason="pinned by /browser-camofox",
            )
            routing._hydrate_camofox_route(route, config)
            routing.cleanup_browsers()
            routing.apply_engine(route)
            state.set_global_default(
                mode="pinned",
                pinned_engine="camofox:main",
                set_by="/browser-camofox",
                when=_now(),
            )
            state.save_state()
        suffix = " (recovered)" if recovery.recovered else ""
        return f"Browser policy (global): pinned to local Camofox (Docker/noVNC){suffix}{vnc_note}"

    session_id, diag = _resolve_session_id(arg)
    if session_id is None:
        return f"Could not resolve session {arg!r}: {diag}"

    recovery = routing.ensure_camofox(config)
    if not recovery.ok:
        return f"Camofox unavailable: {recovery.reason}"

    with state.ROUTER_LOCK:
        session = state.get_session(session_id)
        session["mode"] = "pinned"
        session["pinned_engine"] = "camofox:main"
        session["explicit_engine"] = None
        session["ignore_global"] = False
        session["camofox_url"] = camo_cfg.get("url")
        session["last_reason"] = f"pinned by /browser-camofox {arg}"
        session["last_changed_at"] = _now()
        state.save_state()

    suffix = " (recovered Camofox)" if recovery.recovered else ""
    return (
        f"Browser policy for session {session_id!r}: pinned to local Camofox "
        f"(Docker/noVNC){suffix}{vnc_note}"
    )


# ---------------------------------------------------------------------------
# /browser-cloud [session-name]
# ---------------------------------------------------------------------------


def cmd_browser_cloud(raw: str) -> str:
    config = routing.load_config(reload=True)
    arg = (raw or "").strip()

    if not arg:
        with state.ROUTER_LOCK:
            route = routing.Route(engine="cloud", reason="pinned by /browser-cloud")
            routing.cleanup_browsers()
            routing.apply_engine(route)
            state.set_global_default(
                mode="pinned",
                pinned_engine="cloud",
                set_by="/browser-cloud",
                when=_now(),
            )
            state.save_state()
        return (
            "Browser policy (global): pinned to configured cloud browser "
            f"(expected: {routing.cloud_provider_expected(config)})"
        )

    session_id, diag = _resolve_session_id(arg)
    if session_id is None:
        return f"Could not resolve session {arg!r}: {diag}"

    # Per-session pin, lazy env apply (see /browser-local rationale).
    with state.ROUTER_LOCK:
        session = state.get_session(session_id)
        session["mode"] = "pinned"
        session["pinned_engine"] = "cloud"
        session["explicit_engine"] = None
        # Pinning replaces any prior auto-override on this session.
        session["ignore_global"] = False
        session["cdp_url"] = None
        session["camofox_url"] = None
        session["last_reason"] = f"pinned by /browser-cloud {arg}"
        session["last_changed_at"] = _now()
        state.save_state()

    return (
        f"Browser policy for session {session_id!r}: pinned to configured "
        f"cloud browser (expected: {routing.cloud_provider_expected(config)})"
    )


# ---------------------------------------------------------------------------
# /browser-recover — Chrome + Camofox
# ---------------------------------------------------------------------------


def cmd_browser_recover(raw: str) -> str:
    config = routing.load_config(reload=True)
    with state.ROUTER_LOCK:
        chrome = routing.ensure_local_profile(config)
        camofox = routing.ensure_camofox(config)

    lines: list[str] = []
    if chrome.ok:
        if chrome.recovered:
            lines.append("Local Chrome CDP: recovered (launchctl kickstart succeeded)")
        else:
            lines.append("Local Chrome CDP: already up")
    else:
        lines.append(f"Local Chrome CDP: DOWN ({chrome.reason})")

    if camofox.ok:
        if camofox.recovered:
            lines.append("Camofox: recovered (Colima/docker start succeeded)")
        else:
            lines.append("Camofox: already up")
    else:
        lines.append(f"Camofox: DOWN ({camofox.reason})")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /browser-route [<session-name>] <url>
# ---------------------------------------------------------------------------


def cmd_browser_route(raw: str) -> str:  # noqa: PLR0912
    """Show the route that would be picked for a URL.

    Two forms:

    * ``/browser-route <url>`` — what a fresh session (no per-session
      pin) would do, taking the global default into account.
    * ``/browser-route <session-name> <url>`` — what the named session
      would do, taking its ``ignore_global``, pin, and ``explicit_engine``
      into account. Informational only; the per-session state is not
      mutated.

    Parsing is right-anchored: the **last** whitespace-separated token is
    always the URL, and anything before it (which may contain spaces,
    e.g. ``my session #2``) is treated as the session name.
    """
    text = (raw or "").strip()
    if not text:
        return "Usage: /browser-route [<session-name>] <url>"

    # Split off the last token as the URL; the remainder, if any, is
    # the session name. This is the only sane way to support session
    # titles with whitespace without inventing a quoting syntax.
    last_space = text.rfind(" ")
    if last_space == -1:
        url = text
        session_arg: str | None = None
    else:
        url = text[last_space + 1 :].strip()
        session_arg = text[:last_space].strip() or None

    if not url:
        return "Usage: /browser-route [<session-name>] <url>"

    config = routing.load_config(reload=True)

    session_id: str | None = None
    session_label: str = "(fresh / global default)"
    if session_arg:
        sid, diag = _resolve_session_id(session_arg)
        if sid is None:
            return f"Could not resolve session {session_arg!r}: {diag}"
        session_id = sid
        session_label = f"{session_arg} → {sid}"

    with state.ROUTER_LOCK:
        if session_id is not None and session_id in state.SESSION_STATE:
            # Reuse the live session state so ignore_global, pin and
            # explicit_engine are accounted for. We don't consume the
            # one-shot explicit_engine — this command is informational.
            session_snapshot = dict(state.SESSION_STATE[session_id])
        else:
            session_snapshot = {
                "mode": "auto",
                "pinned_engine": None,
                "explicit_engine": None,
                "ignore_global": False,
            }
        route = routing.decide_route(url, session_snapshot, "auto", config, consume_explicit=False)
        route = routing.ensure_route_available(route, config)

    parts = [
        f"Route for {url}",
        f"session: {session_label}",
        f"engine: {route.engine}",
        f"reason: {route.reason}",
    ]
    if route.cdp_url:
        parts.append(f"cdp_url: {route.cdp_url}")
    if route.camofox_url:
        parts.append(f"camofox_url: {route.camofox_url}")
    if route.browser_url and route.browser_url != url:
        parts.append(f"browser_url: {route.browser_url}")
    if route.fallback_from:
        parts.append(f"fallback_from: {route.fallback_from}")
    if route.error:
        parts.append(f"error: {route.error}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# /browser-sessions
# ---------------------------------------------------------------------------


def _list_recent_sessions(limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recent titled SessionDB entries (best-effort).

    Uses the public ``SessionDB.list_sessions_rich`` API so we don't
    poke at private sqlite internals. Tolerates Hermes versions whose
    signature differs from ours.
    """
    try:
        from hermes_state import (  # type: ignore[import-not-found,reportMissingImports]
            SessionDB,
        )
    except ImportError:
        return []
    try:
        db = SessionDB()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("SessionDB unavailable for /browser-sessions: %s", exc)
        return []

    list_fn = getattr(db, "list_sessions_rich", None)
    if list_fn is None:  # pragma: no cover - older Hermes
        logger.debug("SessionDB.list_sessions_rich missing on this Hermes")
        return []

    try:
        rich = list_fn(limit=limit, order_by_last_active=True)
    except TypeError:  # pragma: no cover - signature drift
        try:
            rich = list_fn(limit=limit)
        except Exception as exc:
            logger.debug("list_sessions_rich(limit=...) failed: %s", exc)
            return []
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("list_sessions_rich failed: %s", exc)
        return []

    rows: list[dict[str, Any]] = []
    for entry in rich or []:
        if not isinstance(entry, dict):
            continue
        title = entry.get("title")
        # /browser-sessions maps titles to browser policy; untitled
        # rows aren't actionable from the slash command.
        if not title:
            continue
        rows.append(
            {
                "id": entry.get("id", ""),
                "title": title,
                "started_at": entry.get("started_at"),
                "last_active": entry.get("last_active"),
            }
        )
    return rows


def cmd_browser_sessions(raw: str) -> str:
    """List recent sessions with their browser policy state."""
    recent = _list_recent_sessions(limit=20)

    with state.ROUTER_LOCK:
        gd = dict(state.GLOBAL_DEFAULT)
        local_state = {sid: dict(s) for sid, s in state.SESSION_STATE.items()}

    header = [
        "Browser sessions",
        f"global mode: {gd.get('mode')} pinned={gd.get('pinned_engine') or 'none'}",
        "",
    ]

    if not recent and not local_state:
        return "\n".join(header + ["(no sessions known)"])

    seen_ids: set[str] = set()
    rows: list[str] = []

    # Recent sessions from SessionDB (with titles).
    for entry in recent:
        sid = entry.get("id", "")
        title = entry.get("title", "")
        seen_ids.add(sid)
        s = local_state.get(sid, {})
        rows.append(_format_session_row(sid, title, s))

    # Sessions present in SESSION_STATE but not in the recent SessionDB
    # listing (e.g. older sessions still pinned, or sessions not titled).
    for sid, s in local_state.items():
        if sid in seen_ids:
            continue
        rows.append(_format_session_row(sid, "(no title)", s))

    return "\n".join(header + rows)


def _format_session_row(session_id: str, title: str, state_dict: dict[str, Any]) -> str:
    short = session_id[:12] + "..." if len(session_id) > 12 else session_id
    if not state_dict:
        return f"  {title!r} [{short}]: (no per-session state; inherits global)"
    mode = state_dict.get("mode", "auto")
    pinned = state_dict.get("pinned_engine") or "—"
    ignore_global = state_dict.get("ignore_global", False)
    last_url = state_dict.get("last_url") or "—"
    ig_flag = " ignore_global" if ignore_global else ""
    explicit = state_dict.get("explicit_engine")
    ex_flag = f" explicit={explicit}" if explicit else ""
    return (
        f"  {title!r} [{short}]: mode={mode} engine={pinned}{ig_flag}{ex_flag} last_url={last_url}"
    )
