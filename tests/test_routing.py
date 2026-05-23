"""Unit tests for the routing priority chain."""

from __future__ import annotations

from datetime import datetime, timezone


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Bare hosts (no scheme)
# ---------------------------------------------------------------------------


class TestBareHost:
    def test_bare_x_com_routes_hard_login(self, plugin, base_config):
        session = plugin.state._empty_session()
        route = plugin.routing.decide_route("x.com", session, "auto", base_config)
        assert route.engine == "profile:main"
        assert route.reason == "hard-login route"

    def test_bare_subdomain_routes_hard_login(self, plugin, base_config):
        session = plugin.state._empty_session()
        route = plugin.routing.decide_route("www.linkedin.com", session, "auto", base_config)
        assert route.engine == "profile:main"

    def test_bare_localhost_routes_internal_debug(self, plugin, base_config):
        session = plugin.state._empty_session()
        route = plugin.routing.decide_route("localhost:5173", session, "auto", base_config)
        assert route.engine == "profile:main"
        assert route.reason == "internal/debug route"

    def test_bare_wikipedia_routes_fast_read(self, plugin, base_config):
        session = plugin.state._empty_session()
        route = plugin.routing.decide_route("en.wikipedia.org", session, "auto", base_config)
        assert route.engine == "fast_read"

    def test_unknown_bare_routes_default(self, plugin, base_config):
        session = plugin.state._empty_session()
        route = plugin.routing.decide_route("example.org", session, "auto", base_config)
        assert route.engine == "cloud"
        assert route.reason == "default interactive route"


# ---------------------------------------------------------------------------
# Global default pin behaviour
# ---------------------------------------------------------------------------


class TestGlobalPin:
    def test_global_local_pins_unrelated_url(self, plugin, base_config):
        plugin.state.set_global_default("pinned", "profile:main", "/test", _ts())
        session = plugin.state._empty_session()
        route = plugin.routing.decide_route(
            "https://www.lemonde.fr/article", session, "auto", base_config
        )
        assert route.engine == "profile:main"
        assert route.reason == "global default pinned"

    def test_global_cloud_pins_local_login(self, plugin, base_config):
        plugin.state.set_global_default("pinned", "cloud", "/test", _ts())
        session = plugin.state._empty_session()
        route = plugin.routing.decide_route("https://x.com/home", session, "auto", base_config)
        assert route.engine == "cloud"

    def test_global_auto_falls_through_to_url_classification(self, plugin, base_config):
        plugin.state.reset_global_default()
        session = plugin.state._empty_session()
        route = plugin.routing.decide_route("https://x.com/home", session, "auto", base_config)
        assert route.engine == "profile:main"
        assert route.reason == "hard-login route"


# ---------------------------------------------------------------------------
# Per-session ignore_global
# ---------------------------------------------------------------------------


class TestIgnoreGlobal:
    def test_ignore_global_skips_global_pin(self, plugin, base_config):
        plugin.state.set_global_default("pinned", "profile:main", "/test", _ts())
        session = plugin.state._empty_session()
        session["ignore_global"] = True
        # lemonde would be cloud (subscription), global pin local; with
        # ignore_global=True we should land on URL classification = cloud.
        route = plugin.routing.decide_route(
            "https://www.lemonde.fr/article", session, "auto", base_config
        )
        assert route.engine == "cloud"
        assert route.reason == "subscription cloud route"

    def test_ignore_global_genuinely_url_driven(self, plugin, base_config):
        plugin.state.set_global_default("pinned", "cloud", "/test", _ts())
        session = plugin.state._empty_session()
        session["ignore_global"] = True
        # global cloud pin would normally win; ignore_global skips it.
        route = plugin.routing.decide_route("https://x.com/home", session, "auto", base_config)
        assert route.engine == "profile:main"
        assert route.reason == "hard-login route"

    def test_session_pin_wins_over_ignore_global(self, plugin, base_config):
        # If a session is pinned, it doesn't matter whether ignore_global
        # is set — priority 3 (session pin) fires before either.
        plugin.state.set_global_default("pinned", "profile:main", "/test", _ts())
        session = plugin.state._empty_session()
        session["mode"] = "pinned"
        session["pinned_engine"] = "cloud"
        session["ignore_global"] = False
        route = plugin.routing.decide_route("https://x.com/home", session, "auto", base_config)
        assert route.engine == "cloud"
        assert route.reason == "session pinned"


# ---------------------------------------------------------------------------
# Explicit hint (one-shot) — including fast_read
# ---------------------------------------------------------------------------


class TestHint:
    def test_hint_cloud_wins_over_url_class(self, plugin, base_config):
        session = plugin.state._empty_session()
        route = plugin.routing.decide_route("https://x.com/home", session, "cloud", base_config)
        assert route.engine == "cloud"
        assert route.reason == "forced by hint"

    def test_hint_fast_read_wins_over_url_class(self, plugin, base_config):
        session = plugin.state._empty_session()
        route = plugin.routing.decide_route("https://x.com/home", session, "fast_read", base_config)
        assert route.engine == "fast_read"
        assert route.reason == "forced by hint"

    def test_explicit_engine_latched_consumed(self, plugin, base_config):
        session = plugin.state._empty_session()
        session["explicit_engine"] = "fast_read"
        # consume_explicit=True clears after read
        route = plugin.routing.decide_route(
            "https://x.com/home", session, "auto", base_config, consume_explicit=True
        )
        assert route.engine == "fast_read"
        assert route.reason == "session explicit hint"
        assert session["explicit_engine"] is None

    def test_explicit_engine_latched_not_consumed_when_flag_off(self, plugin, base_config):
        session = plugin.state._empty_session()
        session["explicit_engine"] = "cloud"
        route = plugin.routing.decide_route(
            "https://x.com/home", session, "auto", base_config, consume_explicit=False
        )
        assert route.engine == "cloud"
        assert session["explicit_engine"] == "cloud"

    def test_explicit_engine_beats_global_pin(self, plugin, base_config):
        plugin.state.set_global_default("pinned", "cloud", "/test", _ts())
        session = plugin.state._empty_session()
        session["explicit_engine"] = "profile:main"
        route = plugin.routing.decide_route(
            "https://x.com/home", session, "auto", base_config, consume_explicit=True
        )
        assert route.engine == "profile:main"
        assert route.reason == "session explicit hint"
