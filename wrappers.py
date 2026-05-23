"""Transparent ``override=True`` wrappers around the built-in ``browser_*``
tools.

``browser_navigate`` is the only wrapper that decides routing; all follow-up
tools (snapshot/click/type/...) reuse the session's last route. Every wrapper
holds ``state.ROUTER_LOCK`` from route resolution through the delegated tool
call so two concurrent browser turns cannot race on the process-global
``BROWSER_CDP_URL``.
"""

from __future__ import annotations

import importlib
import json
import logging
from collections.abc import Callable
from types import ModuleType
from typing import Any

from . import routing, state
from .tools import get_session_key

logger = logging.getLogger(__name__)

# ``ctx`` from register() — needed for dispatch_tool() in fast_read chain.
_ctx: Any = None


def set_plugin_context(ctx: Any) -> None:
    # Module-level ``_ctx`` is the only handle through which wrappers can
    # dispatch fast-read tools; ctx is supplied once at register() time.
    global _ctx  # noqa: PLW0603
    _ctx = ctx


# ---------------------------------------------------------------------------
# Lazy import of the built-in browser_tool functions
# ---------------------------------------------------------------------------


def _bt() -> ModuleType:
    """Return the ``tools.browser_tool`` module (lazy import).

    Uses :func:`importlib.import_module` rather than ``from tools import
    browser_tool`` so static type checkers don't try to resolve
    ``tools/`` from this standalone plugin checkout (Hermes adds it to
    ``sys.path`` at runtime). The runtime behaviour is identical.
    """
    # Deferred import: see the same rationale in routing.cleanup_browsers.
    return importlib.import_module("tools.browser_tool")


# ---------------------------------------------------------------------------
# Fast read chain
# ---------------------------------------------------------------------------


# Tool-name -> args-builder for each entry in ``fast_read_chain``. The
# "cloud_browser" sentinel terminates the chain (handled by caller).
def _args_for_web_extract(url: str) -> dict[str, Any]:
    return {"urls": [url]}


def _args_for_web_search(url: str) -> dict[str, Any]:
    # Searching for the URL itself surfaces the page (or a usable summary)
    # when direct extraction has failed.
    return {"query": url, "limit": 3}


_CHAIN_ARG_BUILDERS: dict[str, Callable[[str], dict[str, Any]]] = {
    "web_extract": _args_for_web_extract,
    "web_search": _args_for_web_search,
}


def _parse_tool_output(raw: Any) -> dict[str, Any]:
    """Best-effort: turn a tool's raw output into a dict."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {"_raw": raw}
    try:
        loaded = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {"_raw": raw}
    if isinstance(loaded, dict):
        return loaded
    return {"_raw": loaded}


_CONTENT_FIELDS = ("content", "text", "extracted", "markdown", "results", "snippets")


def _extract_content(payload: dict[str, Any]) -> str | None:
    """Pull usable text content out of a parsed tool result."""
    if not payload:
        return None
    if payload.get("error"):
        return None

    for field in _CONTENT_FIELDS:
        val = payload.get(field)
        if isinstance(val, str) and val.strip():
            return val
        if isinstance(val, list) and val:
            # web_search returns a list of result dicts; concatenate text-y fields.
            parts: list[str] = []
            for item in val:
                if isinstance(item, dict):
                    for sub in ("snippet", "content", "text", "title", "url"):
                        v = item.get(sub)
                        if isinstance(v, str) and v.strip():
                            parts.append(v.strip())
                elif isinstance(item, str) and item.strip():
                    parts.append(item.strip())
            if parts:
                return "\n\n".join(parts)
    raw = payload.get("_raw")
    if isinstance(raw, str) and raw.strip():
        return raw
    return None


def _chain_steps(config: dict[str, Any]) -> list[str]:
    chain = config.get("fast_read_chain") or []
    if not isinstance(chain, list):
        return []
    return [str(c).strip() for c in chain if c]


def try_fast_read_chain(url: str, config: dict[str, Any]) -> dict[str, Any]:
    """Iterate the configured fast-read chain.

    Returns ``{ok, tool, content, attempts, terminal}``. ``terminal=True``
    means the chain explicitly handed off to ``cloud_browser``; callers
    should then fall back to a real browser run.
    """
    attempts: list[dict[str, Any]] = []

    if _ctx is None or not hasattr(_ctx, "dispatch_tool"):
        attempts.append({"tool": "(plugin)", "ok": False, "error": "plugin ctx unavailable"})
        return {
            "ok": False,
            "tool": None,
            "content": None,
            "attempts": attempts,
            "terminal": False,
        }

    steps = _chain_steps(config)
    if not steps:
        steps = ["web_extract", "cloud_browser"]

    for tool_name in steps:
        if tool_name == "cloud_browser":
            attempts.append(
                {
                    "tool": "cloud_browser",
                    "ok": True,
                    "note": "chain handoff to browser",
                }
            )
            return {
                "ok": False,
                "tool": None,
                "content": None,
                "attempts": attempts,
                "terminal": True,
            }

        builder = _CHAIN_ARG_BUILDERS.get(tool_name)
        if builder is None:
            attempts.append(
                {
                    "tool": tool_name,
                    "ok": False,
                    "error": "no args builder for this chain entry",
                }
            )
            continue

        try:
            raw = _ctx.dispatch_tool(tool_name, builder(url))
        except Exception as exc:
            attempts.append({"tool": tool_name, "ok": False, "error": str(exc)})
            continue

        payload = _parse_tool_output(raw)
        content = _extract_content(payload)
        if content:
            attempts.append({"tool": tool_name, "ok": True, "chars": len(content)})
            return {
                "ok": True,
                "tool": tool_name,
                "content": content,
                "attempts": attempts,
                "terminal": False,
            }

        attempts.append(
            {
                "tool": tool_name,
                "ok": False,
                "error": str(payload.get("error", "no usable content")),
            }
        )

    return {
        "ok": False,
        "tool": None,
        "content": None,
        "attempts": attempts,
        "terminal": False,
    }


def _fast_read_response(url: str, route: routing.Route, fast: dict[str, Any]) -> str:
    return json.dumps(
        {
            "success": True,
            "via": "browser-policy-router",
            "engine": "fast_read",
            "tool_used": fast.get("tool"),
            "url": url,
            "reason": route.reason,
            "content": fast.get("content"),
            "attempts": fast.get("attempts", []),
            # Hard signal for the model: no DOM was loaded, so follow-up
            # browser_click / browser_type / browser_snapshot calls will
            # fail. The model should call browser_policy_set first if
            # interaction is needed.
            "requires_browser_for_interaction": True,
            "note": (
                "Result obtained via fast read instead of a real browser. "
                "Call browser_policy_set(engine='cloud') or 'profile:main' first "
                "if you need to interact with the page (click, type, snapshot)."
            ),
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# browser_navigate wrapper
# ---------------------------------------------------------------------------


def browser_navigate_wrapped(args: dict[str, Any], **kwargs) -> str:
    try:
        return _browser_navigate_impl(args, **kwargs)
    except Exception as exc:
        logger.exception("browser_navigate_wrapped failed")
        return json.dumps(
            {
                "success": False,
                "error": str(exc),
                "tool": "browser_navigate",
            },
            ensure_ascii=False,
        )


def _browser_navigate_impl(args: dict[str, Any], **kwargs) -> str:
    url = (args.get("url") or "").strip()
    if not url:
        return json.dumps(
            {
                "success": False,
                "error": "url is required",
                "tool": "browser_navigate",
            },
            ensure_ascii=False,
        )

    config = routing.load_config()
    session_key = get_session_key(kwargs)

    with state.ROUTER_LOCK:
        session = state.get_session(session_key)
        route = routing.decide_route(
            url,
            session,
            "auto",
            config,
            consume_explicit=True,
        )

    if route.engine == "fast_read":
        fast = try_fast_read_chain(url, config)
        if fast.get("ok"):
            with state.ROUTER_LOCK:
                session = state.get_session(session_key)
                routing.update_session_after_route(session, route, url)
                # Persist: the explicit_engine one-shot was consumed up
                # above and last_engine just moved to fast_read. Without
                # this save, a gateway restart would re-apply the
                # already-consumed latch on next navigate.
                state.save_state()
            return _fast_read_response(url, route, fast)
        attempts = fast.get("attempts", [])
        route = routing.Route(
            engine="cloud",
            reason="fast_read chain failed; fallback cloud",
            fallback_from="fast_read",
            attempts=list(attempts) if attempts else [],
        )

    with state.ROUTER_LOCK:
        session = state.get_session(session_key)
        route = routing.ensure_route_available(route, config)
        routing.maybe_cleanup_on_switch(session, route)
        routing.apply_engine(route)
        routing.update_session_after_route(session, route, url)
        # Persist the post-consumption state (explicit_engine cleared,
        # last_engine/last_url updated) so a gateway restart cannot
        # resurrect a stale one-shot or stale CDP route.
        state.save_state()

        if route.error:
            return json.dumps(
                {
                    "success": False,
                    "via": "browser-policy-router",
                    "engine": route.engine,
                    "reason": route.reason,
                    "error": route.error,
                    "tool": "browser_navigate",
                },
                ensure_ascii=False,
            )

        result = _bt().browser_navigate(url=url, task_id=kwargs.get("task_id"))

    return _annotate(result, route, url)


# ---------------------------------------------------------------------------
# Follow-up wrappers: reuse the session's last route
# ---------------------------------------------------------------------------


def _reuse_session_route(session: dict[str, Any]) -> routing.Route | None:
    """Return a reusable route for follow-up tools, or ``None``.

    ``None`` means there is no real browser session to reuse (no prior
    navigate, or the last navigate was a ``fast_read`` chain result with
    no DOM loaded). The caller MUST NOT delegate to the underlying
    browser tool in that case — the process-global ``BROWSER_CDP_URL``
    might still point at another session's profile, which would attach
    the follow-up to the wrong browser.
    """
    last_engine = session.get("last_engine")
    if not last_engine or last_engine == "fast_read":
        return None
    return routing.Route(
        engine=last_engine,
        cdp_url=session.get("cdp_url"),
        reason="reuse session route",
    )


def _no_reusable_route_error(tool_name: str, session: dict[str, Any]) -> str:
    last_engine = session.get("last_engine")
    if last_engine == "fast_read":
        reason = (
            "the session's last browser action was a fast_read "
            "(no DOM was loaded); call browser_policy_set(engine='cloud' "
            "or 'profile:main') and browser_navigate again before "
            "interactive tools"
        )
    else:
        reason = "no prior browser_navigate on this session; call browser_navigate first"
    return json.dumps(
        {
            "success": False,
            "via": "browser-policy-router",
            "error": reason,
            "tool": tool_name,
            "requires_browser_for_interaction": True,
        },
        ensure_ascii=False,
    )


def _follow_up(
    tool_name: str,
    args: dict[str, Any],
    kwargs: dict[str, Any],
    delegate: Callable[[], str],
) -> str:
    session_key = get_session_key(kwargs)
    with state.ROUTER_LOCK:
        session = state.get_session(session_key)
        route = _reuse_session_route(session)
        if route is None:
            # Refuse to delegate: BROWSER_CDP_URL may still point at
            # another session's Chrome profile from an earlier turn, and
            # silently snapshotting/clicking on the wrong browser is the
            # exact bug this wrapper is meant to prevent.
            return _no_reusable_route_error(tool_name, session)
        routing.apply_engine(route)
        try:
            return delegate()
        except Exception as exc:
            logger.exception("%s wrapper failed", tool_name)
            return json.dumps(
                {
                    "success": False,
                    "error": str(exc),
                    "tool": tool_name,
                },
                ensure_ascii=False,
            )


def browser_snapshot_wrapped(args: dict[str, Any], **kwargs) -> str:
    return _follow_up(
        "browser_snapshot",
        args,
        kwargs,
        lambda: _bt().browser_snapshot(
            full=args.get("full", False),
            task_id=kwargs.get("task_id"),
            user_task=kwargs.get("user_task"),
        ),
    )


def browser_click_wrapped(args: dict[str, Any], **kwargs) -> str:
    return _follow_up(
        "browser_click",
        args,
        kwargs,
        lambda: _bt().browser_click(
            ref=args.get("ref", ""),
            task_id=kwargs.get("task_id"),
        ),
    )


def browser_type_wrapped(args: dict[str, Any], **kwargs) -> str:
    return _follow_up(
        "browser_type",
        args,
        kwargs,
        lambda: _bt().browser_type(
            ref=args.get("ref", ""),
            text=args.get("text", ""),
            task_id=kwargs.get("task_id"),
        ),
    )


def browser_scroll_wrapped(args: dict[str, Any], **kwargs) -> str:
    return _follow_up(
        "browser_scroll",
        args,
        kwargs,
        lambda: _bt().browser_scroll(
            direction=args.get("direction", "down"),
            task_id=kwargs.get("task_id"),
        ),
    )


def browser_back_wrapped(args: dict[str, Any], **kwargs) -> str:
    return _follow_up(
        "browser_back",
        args,
        kwargs,
        lambda: _bt().browser_back(task_id=kwargs.get("task_id")),
    )


def browser_press_wrapped(args: dict[str, Any], **kwargs) -> str:
    return _follow_up(
        "browser_press",
        args,
        kwargs,
        lambda: _bt().browser_press(
            key=args.get("key", ""),
            task_id=kwargs.get("task_id"),
        ),
    )


def browser_console_wrapped(args: dict[str, Any], **kwargs) -> str:
    return _follow_up(
        "browser_console",
        args,
        kwargs,
        lambda: _bt().browser_console(
            clear=args.get("clear", False),
            expression=args.get("expression"),
            task_id=kwargs.get("task_id"),
        ),
    )


def browser_vision_wrapped(args: dict[str, Any], **kwargs) -> str:
    return _follow_up(
        "browser_vision",
        args,
        kwargs,
        lambda: _bt().browser_vision(
            question=args.get("question", ""),
            annotate=args.get("annotate", False),
            task_id=kwargs.get("task_id"),
        ),
    )


def browser_get_images_wrapped(args: dict[str, Any], **kwargs) -> str:
    return _follow_up(
        "browser_get_images",
        args,
        kwargs,
        lambda: _bt().browser_get_images(task_id=kwargs.get("task_id")),
    )


# ---------------------------------------------------------------------------
# Schema + check_fn accessors
# ---------------------------------------------------------------------------


def get_browser_schemas() -> dict[str, dict]:
    """Read the live ``BROWSER_TOOL_SCHEMAS`` from ``tools.browser_tool``."""
    schemas_list = getattr(_bt(), "BROWSER_TOOL_SCHEMAS", [])
    return {s["name"]: s for s in schemas_list}


def get_check_browser_requirements():
    """Return the live ``check_browser_requirements`` callable, if available."""
    return getattr(_bt(), "check_browser_requirements", None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _annotate(raw_result: str, route: routing.Route, url: str) -> str:
    if not isinstance(raw_result, str) or not raw_result.strip().startswith("{"):
        return raw_result
    try:
        payload = json.loads(raw_result)
    except json.JSONDecodeError:
        return raw_result
    if not isinstance(payload, dict):
        return raw_result
    payload["_router"] = {
        "engine": route.engine,
        "reason": route.reason,
        "fallback_from": route.fallback_from,
        "url": url,
    }
    return json.dumps(payload, ensure_ascii=False)
