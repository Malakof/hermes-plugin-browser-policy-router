"""Routing policy, URL classification, env mutation, local Chrome + Camofox recovery."""

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
from urllib.parse import urlparse, urlunparse

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
    engine: str  # "profile:main" | "camofox:main" | "cloud" | "fast_read"
    reason: str = ""
    cdp_url: str | None = None
    # Camofox-specific env applied by ``apply_engine``.
    camofox_url: str | None = None
    camofox_user_id: str | None = None
    camofox_session_key: str | None = None
    camofox_adopt_existing_tab: bool | None = None
    # If the wrapper rewrote ``url`` (e.g. localhost -> host.docker.internal)
    # the rewritten target lands here so the wrapper can pass it to the
    # underlying browser tool and annotate the response.
    browser_url: str | None = None
    fallback_from: str | None = None
    error: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "reason": self.reason,
            "cdp_url": self.cdp_url,
            "camofox_url": self.camofox_url,
            "camofox_user_id": self.camofox_user_id,
            "camofox_session_key": self.camofox_session_key,
            "camofox_adopt_existing_tab": self.camofox_adopt_existing_tab,
            "browser_url": self.browser_url,
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


# Class iteration order for ``decide_route``. The order is the priority:
# the first class whose pattern list matches the URL wins.
#
# ``hard_login`` is kept for backward compatibility with v1.0 configs;
# new deployments should use ``durable_local_login`` (camofox:main) for
# x.com/linkedin and ``human_profile_login`` (profile:main) for Google.
_CLASS_PRIORITY: tuple[str, ...] = (
    "internal_debug",
    "durable_local_login",
    "hard_login",
    "human_profile_login",
    "subscription_login",
    "public_read",
)


def _class_engine(cls_cfg: dict[str, Any]) -> str | None:
    """Return the engine for a class config, honouring ``strategy: fast_read``.

    ``strategy`` is the legacy field name for ``public_read``; we map it
    to engine ``fast_read`` so callers don't have to know about it.
    """
    if not isinstance(cls_cfg, dict):
        return None
    engine = cls_cfg.get("engine")
    if isinstance(engine, str) and engine.strip():
        return engine.strip()
    strategy = cls_cfg.get("strategy")
    if isinstance(strategy, str) and strategy.strip().lower() == "fast_read":
        return "fast_read"
    return None


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
# Camofox probing
# ---------------------------------------------------------------------------


_HTTP_OK_MIN = 200
_HTTP_OK_MAX = 300


def camofox_ready(health_url: str, timeout: float = 1.5) -> bool:
    """True if the Camofox /health endpoint returns 200."""
    if not health_url:
        return False
    try:
        req = urllib.request.Request(health_url, headers={"Connection": "close"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return _HTTP_OK_MIN <= response.status < _HTTP_OK_MAX
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        OSError,
    ):
        return False


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
# Camofox recovery
# ---------------------------------------------------------------------------


def _camofox_cfg(config: dict[str, Any]) -> dict[str, Any]:
    cfg = (config or {}).get("camofox", {}) or {}
    return cfg if isinstance(cfg, dict) else {}


def _camofox_health_url(camo: dict[str, Any]) -> str:
    health = camo.get("health_url")
    if isinstance(health, str) and health.strip():
        return health.strip()
    base = camo.get("url")
    if isinstance(base, str) and base.strip():
        return base.strip().rstrip("/") + "/health"
    return ""


def ensure_camofox(config: dict[str, Any]) -> RecoveryResult:  # noqa: PLR0911,PLR0912
    # Recovery mirrors ``ensure_local_profile``: probe, then escalate via
    # the Colima daemon kickstart + ``docker start`` of the container.
    # Each branch keeps a distinct ``reason`` so /browser-status can show
    # why the engine is unavailable.
    camo = _camofox_cfg(config)
    health_url = _camofox_health_url(camo)
    if not health_url:
        return RecoveryResult(ok=False, reason="camofox not configured (missing url/health_url)")

    if camofox_ready(health_url):
        return RecoveryResult(ok=True, recovered=False)

    if not camo.get("recovery_enabled", True):
        return RecoveryResult(ok=False, reason="camofox down; recovery disabled")

    timeout_s = float(camo.get("recovery_timeout_s", 20))
    poll_s = float(camo.get("recovery_poll_interval_s", 1.0))

    attempts: list[str] = []

    # 1. Make sure the Colima daemon is up — if it isn't, ``docker start``
    #    below cannot reach the Docker socket.
    label = camo.get("colima_launchctl_label", "")
    if label:
        try:
            proc = subprocess.run(
                ["/bin/launchctl", "kickstart", "-k", label],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            attempts.append(f"launchctl kickstart {label} raised {exc}")
        else:
            if proc.returncode != 0:
                attempts.append(
                    f"launchctl kickstart {label} rc={proc.returncode} "
                    f"stderr={_truncate(proc.stderr)}"
                )

    # 2. ``docker start`` the container. ``--restart unless-stopped``
    #    handles daemon restarts; this path covers the case where the
    #    container was explicitly stopped or the daemon was kicked.
    container = camo.get("docker_container", "camofox-browser")
    docker_bin = camo.get("docker_binary", "/opt/homebrew/bin/docker")
    if container:
        try:
            proc = subprocess.run(
                [docker_bin, "start", container],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            attempts.append(f"{docker_bin} start {container} raised {exc}")
        else:
            if proc.returncode != 0:
                attempts.append(
                    f"docker start {container} rc={proc.returncode} stderr={_truncate(proc.stderr)}"
                )

    # 3. Poll until /health returns, or timeout.
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if camofox_ready(health_url):
            return RecoveryResult(ok=True, recovered=True)
        time.sleep(poll_s)

    suffix = "; " + "; ".join(attempts) if attempts else ""
    reason = f"camofox /health not ready within {timeout_s:.1f}s{suffix}"
    return RecoveryResult(ok=False, reason=reason)


# ---------------------------------------------------------------------------
# Localhost rewrite
# ---------------------------------------------------------------------------


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def maybe_rewrite_localhost_for_camofox(url: str, config: dict[str, Any]) -> tuple[str, str | None]:
    """Rewrite ``localhost``/``127.0.0.1`` so Camofox (in a container)
    can reach services on the macOS host.

    Returns ``(browser_url, target_host)`` where ``target_host`` is the
    alias that was substituted (``host.docker.internal`` by default) or
    ``None`` if no rewrite was applied. ``browser_url`` is the URL that
    should be handed to the underlying browser tool.
    """
    camo = _camofox_cfg(config)
    rewrite = camo.get("localhost_rewrite", {}) or {}
    if not rewrite or not rewrite.get("enabled"):
        return url, None
    target = (rewrite.get("target_host") or "host.docker.internal").strip()
    if not target:
        return url, None

    if not url:
        return url, None

    s = url.strip()
    low = s.lower()
    needs_scheme = not any(low.startswith(p) for p in _SCHEME_PREFIXES)
    parse_input = ("https://" + s) if needs_scheme else s
    try:
        parsed = urlparse(parse_input)
    except ValueError:
        return url, None
    host = (parsed.hostname or "").lower()
    if host not in _LOOPBACK_HOSTS:
        return url, None

    port = parsed.port
    new_netloc = target if port is None else f"{target}:{port}"
    rewritten = parsed._replace(netloc=new_netloc)
    rebuilt = urlunparse(rewritten)
    if needs_scheme and rebuilt.startswith("https://"):
        rebuilt = rebuilt[len("https://") :]
    return rebuilt, target


# ---------------------------------------------------------------------------
# Decide / apply / ensure
# ---------------------------------------------------------------------------


_LOCAL_HINTS = {"local", "profile", "profile:main", "chrome"}
_CAMOFOX_HINTS = {"camofox", "camofox:main", "docker", "local-docker"}
_CLOUD_HINTS = {"cloud", "browserbase", "cloud_browser"}
_FAST_HINTS = {"fast_read", "fast", "read"}
_AUTO_HINTS = {"auto", "", None}


def _engine_from_hint(hint: str | None) -> str | None:
    if hint is None:
        return None
    h = hint.strip().lower()
    if h in _LOCAL_HINTS:
        return "profile:main"
    if h in _CAMOFOX_HINTS:
        return "camofox:main"
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
    5. URL classification (engine read from each class's config)
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

    # 5. URL classification — iterate classes in priority order and read
    #    the engine from each class config. ``public_read``'s legacy
    #    ``strategy: fast_read`` is mapped to engine ``fast_read``.
    classes_cfg = cfg.get("classes") or {}
    host = url_host(url)
    if host:
        for class_name in _CLASS_PRIORITY:
            cls_cfg = classes_cfg.get(class_name)
            if not isinstance(cls_cfg, dict):
                continue
            if not matches_class(host, class_name, cfg):
                continue
            engine = _class_engine(cls_cfg)
            if not engine:
                continue
            return Route(
                engine=engine,
                reason=f"{_reason_for_class(class_name)} route",
            )

    # 6. default
    default_engine = (cfg.get("default_interactive_engine") or "cloud").strip()
    return Route(engine=default_engine, reason="default interactive route")


# Stable, human-readable reasons that existing tests + status output rely on.
_CLASS_REASONS: dict[str, str] = {
    "internal_debug": "internal/debug",
    "durable_local_login": "durable local login",
    "hard_login": "hard-login",
    "human_profile_login": "human profile login",
    "subscription_login": "subscription cloud",
    "public_read": "public read",
}


def _reason_for_class(class_name: str) -> str:
    return _CLASS_REASONS.get(class_name, class_name)


def clear_browser_env() -> None:
    """Strip every engine-specific env var.

    Both Chrome (CDP) and Camofox env vars are process-global. To keep
    ``apply_engine`` strict — i.e. setting one engine never leaves stale
    state for another — every entry point that switches engines first
    calls this helper.
    """
    os.environ.pop("BROWSER_CDP_URL", None)
    os.environ.pop("BROWSER_CLOUD_PROVIDER", None)
    os.environ.pop("CAMOFOX_URL", None)
    os.environ.pop("CAMOFOX_USER_ID", None)
    os.environ.pop("CAMOFOX_SESSION_KEY", None)
    os.environ.pop("CAMOFOX_ADOPT_EXISTING_TAB", None)


def apply_engine(route: Route) -> None:
    """Mutate process env to match ``route``.

    Caller must hold ``state.ROUTER_LOCK``. ``BROWSER_CDP_URL`` takes
    priority over Camofox in Hermes (see ``tools/browser_camofox.py``
    ``is_camofox_mode``), so we clear both env families before setting
    either one — otherwise a Chrome -> Camofox switch would silently
    keep using Chrome.
    """
    if route.engine == "profile:main":
        clear_browser_env()
        if route.cdp_url:
            os.environ["BROWSER_CDP_URL"] = route.cdp_url
        logger.info(
            "browser-policy-router set engine=profile:main cdp=%s reason=%s",
            route.cdp_url,
            route.reason,
        )
        return

    if route.engine == "camofox:main":
        clear_browser_env()
        if route.camofox_url:
            os.environ["CAMOFOX_URL"] = route.camofox_url
        if route.camofox_user_id:
            os.environ["CAMOFOX_USER_ID"] = route.camofox_user_id
        if route.camofox_session_key:
            os.environ["CAMOFOX_SESSION_KEY"] = route.camofox_session_key
        if route.camofox_adopt_existing_tab is not None:
            os.environ["CAMOFOX_ADOPT_EXISTING_TAB"] = (
                "true" if route.camofox_adopt_existing_tab else "false"
            )
        logger.info(
            "browser-policy-router set engine=camofox:main url=%s user_id=%s reason=%s",
            route.camofox_url,
            route.camofox_user_id,
            route.reason,
        )
        return

    if route.engine == "cloud":
        clear_browser_env()
        logger.info(
            "browser-policy-router unset browser env (cloud) reason=%s",
            route.reason,
        )
        return

    # fast_read does not touch browser env until a fallback is needed.
    logger.info(
        "browser-policy-router fast_read selected (no env change) reason=%s",
        route.reason,
    )


def _hydrate_camofox_route(route: Route, config: dict[str, Any]) -> None:
    """Fill camofox_* fields on ``route`` from config defaults."""
    camo = _camofox_cfg(config)
    if route.camofox_url is None:
        url = camo.get("url")
        if isinstance(url, str) and url.strip():
            route.camofox_url = url.strip()
    if route.camofox_user_id is None:
        uid = camo.get("user_id")
        if isinstance(uid, str) and uid.strip():
            route.camofox_user_id = uid.strip()
    if route.camofox_session_key is None:
        sk = camo.get("session_key")
        if isinstance(sk, str) and sk.strip():
            route.camofox_session_key = sk.strip()
    if route.camofox_adopt_existing_tab is None:
        route.camofox_adopt_existing_tab = bool(camo.get("adopt_existing_tab"))


def _class_for_reason(reason: str) -> str | None:
    """Reverse-lookup the class name from a route reason string."""
    if not reason:
        return None
    base = reason.split(";", 1)[0].strip()
    for cname, label in _CLASS_REASONS.items():
        if base.startswith(f"{label} "):
            return cname
        if base == label:
            return cname
    return None


def ensure_route_available(route: Route, config: dict[str, Any]) -> Route:
    """Run recovery for local engines; fall back where allowed."""
    if route.engine == "profile:main":
        return _ensure_profile_main(route, config)
    if route.engine == "camofox:main":
        return _ensure_camofox_main(route, config)
    return route


def _ensure_profile_main(route: Route, config: dict[str, Any]) -> Route:
    recovery = ensure_local_profile(config)
    local_cfg = (config or {}).get("local_profile", {}) or {}
    if recovery.ok:
        route.cdp_url = local_cfg.get("cdp_url", "http://127.0.0.1:9222")
        if recovery.recovered:
            route.reason = (route.reason or "") + "; recovered local Chrome"
        return route

    cls = _class_for_reason(route.reason)
    if cls == "internal_debug":
        # Cloud cannot reach localhost / internal targets.
        return route.with_error(recovery.reason)

    return Route(
        engine="cloud",
        reason=f"profile:main unavailable ({recovery.reason}); fallback cloud",
        fallback_from="profile:main",
    )


def _ensure_camofox_main(route: Route, config: dict[str, Any]) -> Route:
    recovery = ensure_camofox(config)
    if recovery.ok:
        _hydrate_camofox_route(route, config)
        if recovery.recovered:
            route.reason = (route.reason or "") + "; recovered camofox"
        return route

    # Fallback policy depends on the class that produced this route:
    # - internal_debug: never falls back to cloud (cloud can't see localhost).
    # - durable_local_login: try profile:main if Chrome is up; otherwise
    #   only fall back to cloud if the class doesn't ban it.
    # - any other class: fall back to cloud unless the class bans it.
    classes_cfg = (config or {}).get("classes") or {}
    cls = _class_for_reason(route.reason)
    cls_cfg = classes_cfg.get(cls, {}) if cls else {}
    no_cloud_fallback = bool(cls_cfg.get("no_cloud_fallback"))

    if cls == "internal_debug":
        # Try profile:main as a host-side fallback (Chrome can reach the
        # Mac's loopback even if Camofox is down). If Chrome is up, swap;
        # otherwise surface the original Camofox error.
        if cdp_ready((config or {}).get("local_profile", {}).get("cdp_url", "")):
            return Route(
                engine="profile:main",
                cdp_url=(config or {})
                .get("local_profile", {})
                .get("cdp_url", "http://127.0.0.1:9222"),
                reason=f"camofox unavailable ({recovery.reason}); fallback profile:main",
                fallback_from="camofox:main",
            )
        return route.with_error(recovery.reason)

    if cls == "durable_local_login" and cdp_ready(
        (config or {}).get("local_profile", {}).get("cdp_url", "")
    ):
        # Prefer the human Chrome profile if it's up — same identity in
        # most cases, just GUI-bound.
        return Route(
            engine="profile:main",
            cdp_url=(config or {}).get("local_profile", {}).get("cdp_url", "http://127.0.0.1:9222"),
            reason=f"camofox unavailable ({recovery.reason}); fallback profile:main",
            fallback_from="camofox:main",
        )

    if no_cloud_fallback:
        return route.with_error(recovery.reason)

    return Route(
        engine="cloud",
        reason=f"camofox:main unavailable ({recovery.reason}); fallback cloud",
        fallback_from="camofox:main",
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
    session_state["camofox_url"] = route.camofox_url
    session_state["browser_url"] = route.browser_url


def cloud_provider_expected(config: dict[str, Any]) -> str:
    return (config or {}).get("cloud_provider_expected", "browserbase")
