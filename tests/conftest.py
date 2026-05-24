"""Shared pytest fixtures for browser-policy-router tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_plugin_module():
    """Load the plugin as a top-level package without depending on Hermes."""
    plugin_dir = Path(__file__).resolve().parent.parent
    pkg_name = "browser_policy_router"
    if pkg_name in sys.modules:
        return sys.modules[pkg_name]
    spec = importlib.util.spec_from_file_location(
        pkg_name,
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = mod
    # __init__.py imports submodules at top-level for ``register()``; that's
    # fine because none of them have import-time side effects beyond
    # defining module-level vars.
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def plugin():
    """Reset module-level state on every test."""
    mod = _load_plugin_module()
    state = mod.state
    routing = mod.routing
    # Point persistence to a per-test temp file so production .state.json
    # never gets clobbered.
    return type(
        "Plugin",
        (),
        {
            "state": state,
            "routing": routing,
            "tools": mod.tools,
            "commands": mod.commands,
            "wrappers": mod.wrappers,
            "schemas": mod.schemas,
        },
    )


@pytest.fixture(autouse=True)
def isolated_state(plugin, tmp_path, monkeypatch):
    """Isolate SESSION_STATE / GLOBAL_DEFAULT / persistence per test."""
    plugin.state.SESSION_STATE.clear()
    plugin.state.reset_global_default()
    plugin.state.set_persistence_path(tmp_path / ".state.json")
    # Make sure BROWSER_CDP_URL doesn't leak between tests.
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    yield
    plugin.state.SESSION_STATE.clear()
    plugin.state.reset_global_default()
    plugin.state.set_persistence_path(None)


@pytest.fixture
def local_chrome_available(plugin, monkeypatch):
    """Make ``ensure_local_profile`` succeed without a real Chrome.

    Tests that call ``/browser-local`` (or ``browser_policy_set(engine="profile:main")``)
    need ``ensure_local_profile`` to return ok so the global default
    actually gets pinned. CI runners don't have Chrome on 127.0.0.1:9222
    nor ``/bin/launchctl``, so we stub the probe + recovery path.
    """
    monkeypatch.setattr(plugin.routing, "cdp_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(
        plugin.routing,
        "ensure_local_profile",
        lambda cfg: plugin.routing.RecoveryResult(ok=True, recovered=False),
    )
    # Avoid touching the real browser_tool cleanup on switch.
    monkeypatch.setattr(plugin.routing, "cleanup_browsers", lambda: None)


@pytest.fixture
def camofox_available(plugin, monkeypatch):
    """Make ``ensure_camofox`` succeed without a real Camofox/Docker.

    CI runners don't have Colima or the Camofox container, so we stub
    the /health probe + the recovery path the same way we do for Chrome.
    """
    monkeypatch.setattr(plugin.routing, "camofox_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(
        plugin.routing,
        "ensure_camofox",
        lambda cfg: plugin.routing.RecoveryResult(ok=True, recovered=False),
    )
    monkeypatch.setattr(plugin.routing, "cleanup_browsers", lambda: None)


@pytest.fixture
def base_config():
    """Realistic config dict — mirrors config.yaml.example domain classes."""
    return {
        "default_mode": "auto",
        "default_interactive_engine": "cloud",
        "cloud_provider_expected": "browserbase",
        "local_profile": {
            "name": "main",
            "cdp_url": "http://127.0.0.1:9222",
            "launchctl_label": "gui/501/com.hermes.chrome-profile-main",
            "recovery_enabled": True,
            "recovery_timeout_s": 1,
            "recovery_poll_interval_s": 0.05,
        },
        "camofox": {
            "name": "main",
            "url": "http://127.0.0.1:9377",
            "health_url": "http://127.0.0.1:9377/health",
            "no_vnc_url": "http://127.0.0.1:6080",
            "user_id": "hermes-main",
            "session_key": "main",
            "adopt_existing_tab": True,
            "recovery_enabled": True,
            "colima_launchctl_label": "system/com.hermes.colima-richard",
            "docker_container": "camofox-browser",
            "docker_binary": "/opt/homebrew/bin/docker",
            "recovery_timeout_s": 1,
            "recovery_poll_interval_s": 0.05,
            "localhost_rewrite": {
                "enabled": True,
                "target_host": "host.docker.internal",
            },
        },
        "classes": {
            "internal_debug": {
                "engine": "profile:main",
                "domains": ["localhost", "*.localhost", "127.0.0.1", "*.local"],
            },
            "hard_login": {
                "engine": "profile:main",
                "domains": [
                    "x.com",
                    "*.x.com",
                    "twitter.com",
                    "*.twitter.com",
                    "linkedin.com",
                    "*.linkedin.com",
                ],
            },
            "subscription_login": {
                "engine": "cloud",
                "domains": ["lemonde.fr", "*.lemonde.fr"],
            },
            "public_read": {
                "strategy": "fast_read",
                "domains": ["wikipedia.org", "*.wikipedia.org"],
            },
        },
        "fast_read_chain": ["web_extract", "web_search", "cloud_browser"],
    }


@pytest.fixture
def camofox_config(base_config):
    """Config dict where internal_debug + durable_local_login route to camofox:main.

    Mirrors what the production config.yaml.example sets after Phase 3.
    Existing tests keep using ``base_config`` (Phase 1 routing) so we
    don't accidentally invalidate them.
    """
    cfg = dict(base_config)
    cfg["classes"] = dict(base_config["classes"])
    cfg["classes"]["internal_debug"] = {
        "engine": "camofox:main",
        "no_cloud_fallback": True,
        "domains": ["localhost", "*.localhost", "127.0.0.1", "*.local"],
    }
    cfg["classes"]["durable_local_login"] = {
        "engine": "camofox:main",
        "domains": [
            "x.com",
            "*.x.com",
            "twitter.com",
            "*.twitter.com",
            "linkedin.com",
            "*.linkedin.com",
        ],
    }
    cfg["classes"]["human_profile_login"] = {
        "engine": "profile:main",
        "domains": ["accounts.google.com", "mail.google.com"],
    }
    # Remove hard_login so durable_local_login takes priority for x.com.
    cfg["classes"].pop("hard_login", None)
    return cfg
