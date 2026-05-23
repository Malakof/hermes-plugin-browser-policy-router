"""Routing policy, URL classification, env mutation, local Chrome recovery."""

from __future__ import annotations

import fnmatch
import importlib
import json
import logging
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import yaml  # type: ignore[reportMissingModuleSource]
except ImportError:  # pragma: no cover - yaml is part of Hermes base deps
    yaml = None  # type: ignore[assignment]

from . import state as router_state

logger = logging.getLogger(__name__)

_CONFIG_FILENAME = "config.yaml"
_config_cache: dict[str, Any] | None = None
_config_mtime: float | None = None


def _plugin_dir() -> Path:
    return Path(__file__).parent


def load_config(reload: bool = False) -> dict[str, Any]:
    """Read ``config.yaml`` with mtime-based caching."""
    # Module-level cache shared across calls; mutation guarded by file mtime,
    # not by a lock, because reads are idempotent and writes always observe a
    # newer mtime than what's cached.
    global _config_cache, _config_mtime  # noqa: PLW0603

    path = _plugin_dir() / _CONFIG_FILENAME
    if not path.exists():
        _config_cache = {}
        _config_mtime = None
        return _config_cache

    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None

    if not reload and _config_cache is not None and _config_mtime == mtime:
        return _config_cache

    if yaml is None:
        logger.error("PyYAML not available — browser-policy-router config disabled")
        _config_cache = {}
        _config_mtime = mtime
        return _config_cache

    try:
        with open(path) as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as exc:
        logger.exception("Failed to read %s: %s", path, exc)
        data = {}

    if not isinstance(data, dict):
        data = {}

    _config_cache = data
    _config_mtime = mtime
    return _config_cache


# ---------------------------------------------------------------------------
# Route value
# ---------------------------------------------------------------------------


@dataclass
class Route:
    engine: str  # "profile:main" | "cloud" | "fast_read"
    reason: str = ""
    cdp_url: str | None = None
    fallback_from: str | None = None
    error: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "reason": self.reason,
            "cdp_url": self.cdp_url,
            "fallback_from": self.fallback_from,
            "error": self.error,
            "attempts": self.attempts,
        }

    def with_error(self, err: str) -> Route:
        self.error = err
        return self


# ---------------------------------------------------------------------------
# URL / host parsing
# ---------------------------------------------------------------------------


_SCHEME_PREFIXES = ("http://", "https://", "ftp://", "file://", "about:")


def url_host(url: str) -> str:
    """Return the lowercased host for ``url``.

    Handles bare hostnames (``x.com``) and ``host:port`` (``localhost:5173``)
    in addition to fully-qualified URLs, since the model often passes either
    shape.
    """
    if not url:
        return ""
    s = url.strip()
    if not s:
        return ""
    low = s.lower()
    if not any(low.startswith(p) for p in _SCHEME_PREFIXES):
        # urlparse needs a scheme to populate ``hostname``.
        s = "https://" + s
    try:
        parsed = urlparse(s)
    except ValueError:
        return ""
    return (parsed.hostname or "").lower().strip()


def host_matches(host: str, pattern: str) -> bool:
    pat = pattern.lower().strip()
    if not pat:
        return False
    if pat == host:
        return True
    if pat.startswith("*."):
        suffix = pat[2:]
        return host == suffix or host.endswith("." + suffix)
    return fnmatch.fnmatchcase(host, pat)


def matches_class(host: str, class_name: str, config: dict[str, Any]) -> bool:
    classes = config.get("classes", {}) or {}
    cls = classes.get(class_name, {}) or {}
    patterns = cls.get("domains", []) or []
    return any(host_matches(host, pattern) for pattern in patterns)


# ---------------------------------------------------------------------------
# CDP probing
# ---------------------------------------------------------------------------


def _is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def cdp_ready(cdp_url: str, timeout: float = 1.5) -> bool:
    """True if the CDP /json/version endpoint reports a debugger URL."""
    if not cdp_url:
        return False
    try:
        url = cdp_url.rstrip("/") + "/json/version"
        req = urllib.request.Request(url, headers={"Connection": "close"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
            return "webSocketDebuggerUrl" in data
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        json.JSONDecodeError,
        TimeoutError,
        OSError,
    ):
        return False


def launchctl_state(label: str) -> str:
    """Return ``running``/``stopped``/``unknown`` for a launchd label."""
    if not label:
        return "unknown"
    try:
        proc = subprocess.run(
            ["/bin/launchctl", "print", label],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "unknown"
    if proc.returncode != 0:
        return "unknown"
    out = proc.stdout
    if "state = running" in out:
        return "running"
    if "state = " in out:
        return "stopped"
    return "unknown"


# ---------------------------------------------------------------------------
# Local profile recovery
# ---------------------------------------------------------------------------


@dataclass
class RecoveryResult:
    ok: bool
    recovered: bool = False
    reason: str = ""


def _truncate(text: str, limit: int = 240) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def ensure_local_profile(config: dict[str, Any]) -> RecoveryResult:  # noqa: PLR0911
    # Each branch maps to a distinct recovery state (already up, disabled,
    # missing label, kickstart failure, kickstart timeout, recovered, etc.);
    # flattening into one return path would obscure the diagnostic reason.
    local = (config or {}).get("local_profile", {}) or {}
    cdp_url = local.get("cdp_url", "http://127.0.0.1:9222")

    if cdp_ready(cdp_url):
        return RecoveryResult(ok=True, recovered=False)

    if not local.get("recovery_enabled", True):
        return RecoveryResult(ok=False, reason="cdp down; recovery disabled")

    label = local.get("launchctl_label", "")
    if not label:
        return RecoveryResult(ok=False, reason="cdp down; no launchctl_label configured")

    try:
        proc = subprocess.run(
            ["/bin/launchctl", "kickstart", "-k", label],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return RecoveryResult(ok=False, reason=f"launchctl kickstart failed: {exc}")

    if proc.returncode != 0:
        stderr = _truncate(proc.stderr)
        stdout = _truncate(proc.stdout)
        detail = stderr or stdout or f"exit={proc.returncode}"
        reason = f"launchctl kickstart {label} failed (rc={proc.returncode}): {detail}"
        return RecoveryResult(ok=False, reason=reason)

    timeout_s = float(local.get("recovery_timeout_s", 8))
    poll_s = float(local.get("recovery_poll_interval_s", 0.5))
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if cdp_ready(cdp_url):
            return RecoveryResult(ok=True, recovered=True)
        time.sleep(poll_s)

    last_stderr = _truncate(proc.stderr)
    suffix = f"; launchctl stderr: {last_stderr}" if last_stderr else ""
    reason = f"cdp not ready within {timeout_s:.1f}s after launchctl kickstart {label}{suffix}"
    return RecoveryResult(ok=False, reason=reason)


# ---------------------------------------------------------------------------
# Decide / apply / ensure
# ---------------------------------------------------------------------------


_LOCAL_HINTS = {"local", "profile", "profile:main"}
_CLOUD_HINTS = {"cloud", "browserbase", "cloud_browser"}
_FAST_HINTS = {"fast_read", "fast", "read"}
_AUTO_HINTS = {"auto", "", None}


def _engine_from_hint(hint: str | None) -> str | None:
    if hint is None:
        return None
    h = hint.strip().lower()
    if h in _LOCAL_HINTS:
        return "profile:main"
    if h in _CLOUD_HINTS:
        return "cloud"
    if h in _FAST_HINTS:
        return "fast_read"
    return None


def decide_route(  # noqa: PLR0911
    # Returns-per-priority is intentional: 6 documented priority levels +
    # default. Collapsing them would require nested branches and obscure
    # which rule fired.
    url: str,
    session_state: dict[str, Any],
    hint: str = "auto",
    config: dict[str, Any] | None = None,
    *,
    consume_explicit: bool = False,
) -> Route:
    """Decide which engine to use for ``url`` given session + global state.

    Priority order:

    1. ``hint`` arg (when not ``auto``)
    2. session ``explicit_engine`` (one-shot from ``browser_policy_route``);
       only consumed when ``consume_explicit=True``
    3. session ``mode == "pinned"``
    4. global default ``mode == "pinned"`` — **skipped** when the
       session has ``ignore_global=True``, so an explicit auto override
       gives genuine URL-driven behaviour even if a global pin exists
    5. URL classification
    6. ``default_interactive_engine``
    """
    cfg = config if config is not None else load_config()

    # 1. explicit hint argument
    hint_engine = _engine_from_hint(hint)
    if hint_engine:
        return Route(engine=hint_engine, reason="forced by hint")

    # 2. session one-shot explicit_engine
    explicit = session_state.get("explicit_engine")
    if explicit:
        if consume_explicit:
            session_state["explicit_engine"] = None
        return Route(engine=explicit, reason="session explicit hint")

    # 3. session pin
    if session_state.get("mode") == "pinned":
        pinned = session_state.get("pinned_engine") or "cloud"
        return Route(engine=pinned, reason="session pinned")

    # 4. global default pin (slash-command driven), unless the session
    #    has explicitly opted out via ``ignore_global``.
    if not session_state.get("ignore_global"):
        global_default = router_state.GLOBAL_DEFAULT
        if global_default.get("mode") == "pinned":
            pinned = global_default.get("pinned_engine") or "cloud"
            return Route(engine=pinned, reason="global default pinned")

    # 5. URL classification
    host = url_host(url)
    if host:
        if matches_class(host, "internal_debug", cfg):
            return Route(engine="profile:main", reason="internal/debug route")
        if matches_class(host, "hard_login", cfg):
            return Route(engine="profile:main", reason="hard-login route")
        if matches_class(host, "subscription_login", cfg):
            return Route(engine="cloud", reason="subscription cloud route")
        if matches_class(host, "public_read", cfg):
            return Route(engine="fast_read", reason="public read route")

    # 6. default
    default_engine = (cfg.get("default_interactive_engine") or "cloud").strip()
    return Route(engine=default_engine, reason="default interactive route")


def apply_engine(route: Route) -> None:
    """Mutate process env to match ``route``.

    Caller must hold ``state.ROUTER_LOCK``.
    """
    if route.engine == "profile:main":
        if route.cdp_url:
            os.environ["BROWSER_CDP_URL"] = route.cdp_url
        os.environ.pop("BROWSER_CLOUD_PROVIDER", None)
        logger.info(
            "browser-policy-router set engine=profile:main cdp=%s reason=%s",
            route.cdp_url,
            route.reason,
        )
        return

    if route.engine == "cloud":
        os.environ.pop("BROWSER_CDP_URL", None)
        os.environ.pop("BROWSER_CLOUD_PROVIDER", None)
        logger.info(
            "browser-policy-router unset BROWSER_CDP_URL reason=%s",
            route.reason,
        )
        return

    # fast_read does not touch browser env until a fallback is needed.
    logger.info(
        "browser-policy-router fast_read selected (no env change) reason=%s",
        route.reason,
    )


def ensure_route_available(route: Route, config: dict[str, Any]) -> Route:
    """Run recovery for local profile; fall back to cloud where allowed."""
    if route.engine != "profile:main":
        return route

    recovery = ensure_local_profile(config)
    local_cfg = (config or {}).get("local_profile", {}) or {}
    if recovery.ok:
        route.cdp_url = local_cfg.get("cdp_url", "http://127.0.0.1:9222")
        if recovery.recovered:
            route.reason = (route.reason or "") + "; recovered local Chrome"
        return route

    if route.reason.startswith("internal/debug"):
        # Cloud cannot reach localhost / internal targets.
        return route.with_error(recovery.reason)

    return Route(
        engine="cloud",
        reason=f"profile:main unavailable ({recovery.reason}); fallback cloud",
        fallback_from="profile:main",
    )


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def cleanup_browsers() -> None:
    """Best-effort flush of active browser sessions."""
    # Deferred dynamic import: importing tools.browser_tool at module top
    # would trigger Hermes browser-stack initialisation during plugin
    # load (no import-time side effects). ``importlib.import_module``
    # also keeps static type checkers from trying to resolve ``tools/``
    # from this standalone plugin checkout.
    try:
        browser_tool = importlib.import_module("tools.browser_tool")
    except ImportError:
        return
    try:
        browser_tool.cleanup_all_browsers()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("cleanup_all_browsers raised: %s", exc)


def maybe_cleanup_on_switch(session_state: dict[str, Any], new_route: Route) -> None:
    old_engine = session_state.get("last_engine")
    if old_engine and old_engine != new_route.engine:
        cleanup_browsers()


# ---------------------------------------------------------------------------
# Session state plumbing
# ---------------------------------------------------------------------------


def update_session_after_route(
    session_state: dict[str, Any],
    route: Route,
    url: str | None,
) -> None:
    session_state["last_engine"] = route.engine
    session_state["last_url"] = url
    session_state["last_reason"] = route.reason
    session_state["last_changed_at"] = datetime.now(timezone.utc).isoformat()
    session_state["cdp_url"] = route.cdp_url


def cloud_provider_expected(config: dict[str, Any]) -> str:
    return (config or {}).get("cloud_provider_expected", "browserbase")
