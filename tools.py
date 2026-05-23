"""Model-facing tool handlers.

All handlers catch exceptions and return JSON strings (never raise into the
tool loop).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from . import routing, state

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_session_key(kwargs: dict[str, Any]) -> str:
    return kwargs.get("session_id") or kwargs.get("task_id") or "default"


def _error(tool: str, exc: Exception) -> str:
    msg = str(exc)
    logger.warning("%s returning error: %s", tool, msg)
    return json.dumps(
        {"success": False, "error": msg, "tool": tool},
        ensure_ascii=False,
    )


def _ok(payload: dict[str, Any]) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# browser_policy_status
# ---------------------------------------------------------------------------


def browser_policy_status(args: dict[str, Any], **kwargs) -> str:
    try:
        return _browser_policy_status_impl(args, **kwargs)
    except Exception as exc:
        logger.exception("browser_policy_status crashed")
        return _error("browser_policy_status", exc)


def _browser_policy_status_impl(args: dict[str, Any], **kwargs) -> str:
    config = routing.load_config(reload=True)
    session_key = get_session_key(kwargs)

    with state.ROUTER_LOCK:
        session = state.get_session(session_key)
        mode = session.get("mode", "auto")
        pinned = session.get("pinned_engine")
        explicit = session.get("explicit_engine")
        ignore_global = bool(session.get("ignore_global"))
        last_engine = session.get("last_engine")
        last_reason = session.get("last_reason")
        last_url = session.get("last_url")
        last_changed_at = session.get("last_changed_at")

        global_default = dict(state.GLOBAL_DEFAULT)

        local_cfg = config.get("local_profile", {}) or {}
        local_cdp = local_cfg.get("cdp_url", "http://127.0.0.1:9222")
        cdp_ok = routing.cdp_ready(local_cdp)
        agent_state = routing.launchctl_state(local_cfg.get("launchctl_label", ""))
        cdp_env = os.environ.get("BROWSER_CDP_URL", "")

    payload = {
        "session_key": session_key,
        "mode": mode,
        "pinned_engine": pinned,
        "explicit_engine": explicit,
        "ignore_global": ignore_global,
        "last_engine": last_engine,
        "last_url": last_url,
        "last_reason": last_reason,
        "last_changed_at": last_changed_at,
        "global_default": global_default,
        "local_profile": {
            "cdp_url": local_cdp,
            "cdp_ready": cdp_ok,
            "launchctl_label": local_cfg.get("launchctl_label", ""),
            "launchctl_state": agent_state,
        },
        "cloud_provider_expected": routing.cloud_provider_expected(config),
        "browser_cdp_url_env": cdp_env,
    }
    return _ok(payload)


# ---------------------------------------------------------------------------
# browser_policy_set
# ---------------------------------------------------------------------------


def browser_policy_set(args: dict[str, Any], **kwargs) -> str:
    try:
        return _browser_policy_set_impl(args, **kwargs)
    except Exception as exc:
        logger.exception("browser_policy_set crashed")
        return _error("browser_policy_set", exc)


def _browser_policy_set_impl(args: dict[str, Any], **kwargs) -> str:
    engine = (args.get("engine") or "").strip().lower()
    if engine not in {"profile:main", "cloud", "auto"}:
        return _error(
            "browser_policy_set",
            ValueError(f"unsupported engine {engine!r}; want profile:main/cloud/auto"),
        )

    config = routing.load_config(reload=True)
    session_key = get_session_key(kwargs)

    with state.ROUTER_LOCK:
        session = state.get_session(session_key)

        if engine == "auto":
            # Explicit auto-override: this session is now genuinely
            # URL-driven and stops inheriting the global default pin.
            session["mode"] = "auto"
            session["pinned_engine"] = None
            session["explicit_engine"] = None
            session["ignore_global"] = True
            global_pin = state.GLOBAL_DEFAULT.get("pinned_engine")
            note = (
                f"; overriding global default pin ({global_pin})"
                if state.GLOBAL_DEFAULT.get("mode") == "pinned"
                else ""
            )
            state.save_state()
            return _ok(
                {
                    "session_key": session_key,
                    "mode": "auto",
                    "ignore_global": True,
                    "message": f"Browser policy: auto (URL-driven){note}",
                }
            )

        if engine == "profile:main":
            recovery = routing.ensure_local_profile(config)
            if not recovery.ok:
                return _error(
                    "browser_policy_set",
                    RuntimeError(f"local Chrome unavailable: {recovery.reason}"),
                )
            local_cfg = config.get("local_profile", {}) or {}
            route = routing.Route(
                engine="profile:main",
                cdp_url=local_cfg.get("cdp_url", "http://127.0.0.1:9222"),
                reason="pinned by browser_policy_set",
            )
            routing.maybe_cleanup_on_switch(session, route)
            routing.apply_engine(route)
            routing.update_session_after_route(session, route, None)
            session["mode"] = "pinned"
            session["pinned_engine"] = "profile:main"
            session["explicit_engine"] = None
            # Pinning replaces any prior auto-override on this session.
            session["ignore_global"] = False
            state.save_state()
            return _ok(
                {
                    "session_key": session_key,
                    "mode": "pinned",
                    "pinned_engine": "profile:main",
                    "cdp_url": route.cdp_url,
                    "recovered": recovery.recovered,
                    "message": "Browser policy: pinned to local Chrome profile",
                }
            )

        # engine == "cloud"
        route = routing.Route(
            engine="cloud",
            reason="pinned by browser_policy_set",
        )
        routing.maybe_cleanup_on_switch(session, route)
        routing.apply_engine(route)
        routing.update_session_after_route(session, route, None)
        session["mode"] = "pinned"
        session["pinned_engine"] = "cloud"
        session["explicit_engine"] = None
        # Pinning replaces any prior auto-override on this session.
        session["ignore_global"] = False
        state.save_state()
        return _ok(
            {
                "session_key": session_key,
                "mode": "pinned",
                "pinned_engine": "cloud",
                "cloud_provider_expected": routing.cloud_provider_expected(config),
                "message": "Browser policy: pinned to configured cloud browser",
            }
        )


# ---------------------------------------------------------------------------
# browser_policy_route
# ---------------------------------------------------------------------------


def browser_policy_route(args: dict[str, Any], **kwargs) -> str:
    try:
        return _browser_policy_route_impl(args, **kwargs)
    except Exception as exc:
        logger.exception("browser_policy_route crashed")
        return _error("browser_policy_route", exc)


def _browser_policy_route_impl(args: dict[str, Any], **kwargs) -> str:
    url = (args.get("url") or "").strip()
    hint = (args.get("hint") or "auto").strip().lower()
    if not url:
        return _error(
            "browser_policy_route",
            ValueError("url is required"),
        )

    config = routing.load_config(reload=True)
    session_key = get_session_key(kwargs)

    with state.ROUTER_LOCK:
        session = state.get_session(session_key)
        route = routing.decide_route(url, session, hint, config)
        route = routing.ensure_route_available(route, config)
        routing.maybe_cleanup_on_switch(session, route)
        routing.apply_engine(route)
        routing.update_session_after_route(session, route, url)

        # If the caller asked for a specific engine, latch it as a one-shot
        # so the next browser_navigate honours it — including ``fast_read``,
        # which the wrapper handles via its chain even for URLs that aren't
        # classified ``public_read``.
        explicit_set = False
        if hint != "auto" and routing._engine_from_hint(hint) is not None:
            session["explicit_engine"] = route.engine
            session["last_changed_at"] = datetime.now(timezone.utc).isoformat()
            explicit_set = True
        else:
            session["explicit_engine"] = None
        state.save_state()

    payload = {
        "session_key": session_key,
        "url": url,
        "hint": hint,
        "explicit_engine_latched": explicit_set,
        **route.to_dict(),
    }
    return _ok(payload)
