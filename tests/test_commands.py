"""Tests for slash command handlers + per-session arg resolution."""

from __future__ import annotations


class FakeSessionDB:
    """Stand-in for ``hermes_state.SessionDB`` in tests."""

    def __init__(self, titles=None):
        self._titles = titles or {"my-research": "s_abc123", "work": "s_def456"}

    def resolve_session_id(self, x):
        # Mimic prefix match on raw ids.
        return x if x.startswith("s_") and len(x) > 3 else None

    def resolve_session_by_title(self, title):
        return self._titles.get(title)


class _HermesStateStub:
    SessionDB = FakeSessionDB


def _install_hermes_state(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "hermes_state", _HermesStateStub)


class TestSlashAutoOverride:
    def test_browser_auto_session_sets_ignore_global_true(
        self, plugin, monkeypatch, base_config, local_chrome_available
    ):
        _install_hermes_state(monkeypatch)
        # Pre-condition: global pin = local
        plugin.commands.cmd_browser_local("")
        assert plugin.state.GLOBAL_DEFAULT["mode"] == "pinned"
        # Then override one session
        out = plugin.commands.cmd_browser_auto("my-research")
        assert "overriding global pin (profile:main)" in out
        sess = plugin.state.SESSION_STATE["s_abc123"]
        assert sess["mode"] == "auto"
        assert sess["ignore_global"] is True

    def test_browser_auto_session_makes_lemonde_route_cloud(
        self, plugin, monkeypatch, base_config, tmp_path, local_chrome_available
    ):
        _install_hermes_state(monkeypatch)
        plugin.commands.cmd_browser_local("")
        plugin.commands.cmd_browser_auto("my-research")
        sess = plugin.state.SESSION_STATE["s_abc123"]
        # lemonde is subscription_login -> cloud; with ignore_global the
        # local global pin is bypassed and URL classification wins.
        route = plugin.routing.decide_route(
            "https://www.lemonde.fr/article", sess, "auto", base_config
        )
        assert route.engine == "cloud"
        assert route.reason == "subscription cloud route"

    def test_other_sessions_still_inherit_global_pin(
        self, plugin, monkeypatch, base_config, local_chrome_available
    ):
        _install_hermes_state(monkeypatch)
        plugin.commands.cmd_browser_local("")
        plugin.commands.cmd_browser_auto("my-research")
        # Untouched session
        sess = plugin.state._empty_session()
        route = plugin.routing.decide_route(
            "https://www.lemonde.fr/article", sess, "auto", base_config
        )
        assert route.engine == "profile:main"
        assert route.reason == "global default pinned"

    def test_pinning_clears_ignore_global(self, plugin, monkeypatch, local_chrome_available):
        _install_hermes_state(monkeypatch)
        plugin.commands.cmd_browser_auto("my-research")
        assert plugin.state.SESSION_STATE["s_abc123"]["ignore_global"] is True
        plugin.commands.cmd_browser_cloud("my-research")
        assert plugin.state.SESSION_STATE["s_abc123"]["ignore_global"] is False
        assert plugin.state.SESSION_STATE["s_abc123"]["pinned_engine"] == "cloud"


class TestSlashSessionResolution:
    def test_unknown_session_returns_clear_error(self, plugin, monkeypatch):
        _install_hermes_state(monkeypatch)
        out = plugin.commands.cmd_browser_local("does-not-exist")
        assert "Could not resolve session 'does-not-exist'" in out
        assert "no session matches title" in out

    def test_session_resolution_via_title(self, plugin, monkeypatch):
        _install_hermes_state(monkeypatch)
        out = plugin.commands.cmd_browser_cloud("work")
        assert "'s_def456'" in out


class TestBrowserRouteCommand:
    def test_browser_route_no_arg(self, plugin):
        out = plugin.commands.cmd_browser_route("")
        assert "Usage:" in out

    def test_browser_route_url_only(self, plugin, base_config, monkeypatch):
        monkeypatch.setattr(plugin.routing, "load_config", lambda reload=False: base_config)
        monkeypatch.setattr(
            plugin.routing,
            "ensure_route_available",
            lambda r, c: r,  # don't probe CDP in tests
        )
        out = plugin.commands.cmd_browser_route("https://x.com/home")
        assert "engine: profile:main" in out
        assert "reason: hard-login route" in out
        assert "session: (fresh / global default)" in out

    def test_browser_route_with_session_uses_session_state(self, plugin, base_config, monkeypatch):
        _install_hermes_state(monkeypatch)
        monkeypatch.setattr(plugin.routing, "load_config", lambda reload=False: base_config)
        monkeypatch.setattr(plugin.routing, "ensure_route_available", lambda r, c: r)
        # Pin my-research to cloud, then ask what x.com would route to in it.
        plugin.commands.cmd_browser_cloud("my-research")
        out = plugin.commands.cmd_browser_route("my-research https://x.com/home")
        assert "engine: cloud" in out
        assert "reason: session pinned" in out
        assert "my-research → s_abc123" in out

    def test_browser_route_with_unknown_session(self, plugin, monkeypatch):
        _install_hermes_state(monkeypatch)
        out = plugin.commands.cmd_browser_route("nope https://x.com/home")
        assert "Could not resolve session 'nope'" in out

    def test_browser_route_accepts_session_name_with_spaces(self, plugin, monkeypatch, base_config):
        # Session title containing whitespace must still parse: the
        # URL is the *last* whitespace-separated token, everything
        # before it is the session name.
        class SpacedSessionDB:
            def resolve_session_id(self, x):
                return None  # forces title lookup path

            def resolve_session_by_title(self, title):
                if title == "my big project":
                    return "s_spaced42"
                return None

        import sys

        monkeypatch.setitem(
            sys.modules, "hermes_state", type("M", (), {"SessionDB": SpacedSessionDB})
        )
        monkeypatch.setattr(plugin.routing, "load_config", lambda reload=False: base_config)
        monkeypatch.setattr(plugin.routing, "ensure_route_available", lambda r, c: r)
        # Set a pin on the spaced session to make output predictable
        plugin.state.get_session("s_spaced42")["mode"] = "pinned"
        plugin.state.get_session("s_spaced42")["pinned_engine"] = "cloud"

        out = plugin.commands.cmd_browser_route("my big project https://x.com/home")
        assert "engine: cloud" in out
        assert "session: my big project → s_spaced42" in out

    def test_browser_route_session_only_treated_as_url(self, plugin, monkeypatch):
        # A single non-URL token is still treated as the URL (informational
        # mode). url_host is forgiving and routes a bare hostname.
        _install_hermes_state(monkeypatch)
        monkeypatch.setattr(plugin.routing, "ensure_route_available", lambda r, c: r)
        out = plugin.commands.cmd_browser_route("x.com")
        assert "Route for x.com" in out
        # No session was given → fresh session label
        assert "(fresh / global default)" in out


class TestBrowserSessionsCommand:
    def test_browser_sessions_with_no_sessions(self, plugin, monkeypatch):
        # No hermes_state module → no recent sessions
        import sys

        monkeypatch.setitem(sys.modules, "hermes_state", None)
        out = plugin.commands.cmd_browser_sessions("")
        assert "Browser sessions" in out

    def test_browser_sessions_shows_per_session_state(self, plugin, monkeypatch):
        _install_hermes_state(monkeypatch)
        plugin.commands.cmd_browser_cloud("my-research")
        plugin.commands.cmd_browser_auto("work")
        # Local SESSION_STATE has both entries
        out = plugin.commands.cmd_browser_sessions("")
        # Either via SessionDB recent (which our fake doesn't populate) or
        # via the SESSION_STATE fallback
        assert "s_abc123" in out
        assert "s_def456" in out
        assert "ignore_global" in out  # at least one session has the flag

    def test_browser_sessions_uses_list_sessions_rich(self, plugin, monkeypatch):
        # Verify we go through the public API rather than poking at
        # SessionDB internals.
        rich_called = {"value": False, "kwargs": None}

        class RichSessionDB:
            def list_sessions_rich(self, **kwargs):
                rich_called["value"] = True
                rich_called["kwargs"] = kwargs
                return [
                    {
                        "id": "s_titled1",
                        "title": "alpha",
                        "started_at": "2026-05-23T10:00:00",
                        "last_active": "2026-05-23T11:00:00",
                    },
                    {
                        "id": "s_titled2",
                        "title": "beta",
                        "started_at": "2026-05-23T09:00:00",
                        "last_active": "2026-05-23T09:30:00",
                    },
                    # Untitled entry should be dropped
                    {"id": "s_no_title", "title": None},
                ]

        import sys

        monkeypatch.setitem(
            sys.modules, "hermes_state", type("M", (), {"SessionDB": RichSessionDB})
        )
        out = plugin.commands.cmd_browser_sessions("")
        assert rich_called["value"] is True
        assert rich_called["kwargs"].get("limit") == 20
        assert "alpha" in out
        assert "beta" in out
        # Untitled entries don't appear in /browser-sessions output
        assert "s_no_title" not in out

    def test_browser_sessions_falls_back_when_list_sessions_rich_missing(self, plugin, monkeypatch):
        # Older Hermes versions may not expose list_sessions_rich; the
        # plugin should fall back gracefully instead of crashing.
        class OldSessionDB:
            pass  # no list_sessions_rich

        import sys

        monkeypatch.setitem(sys.modules, "hermes_state", type("M", (), {"SessionDB": OldSessionDB}))
        # Should not raise; just returns an empty recent list. Local
        # SESSION_STATE still feeds the output.
        out = plugin.commands.cmd_browser_sessions("")
        assert "Browser sessions" in out


class TestPersistence:
    def test_state_round_trips_through_disk(self, plugin, monkeypatch):
        _install_hermes_state(monkeypatch)
        plugin.commands.cmd_browser_local("")  # global pin
        plugin.commands.cmd_browser_cloud("my-research")  # per-session pin
        plugin.commands.cmd_browser_auto("work")  # ignore_global

        # Snapshot live state, clear, then load — should restore identically.
        gd_before = dict(plugin.state.GLOBAL_DEFAULT)
        sess_before = {k: dict(v) for k, v in plugin.state.SESSION_STATE.items()}

        plugin.state.SESSION_STATE.clear()
        plugin.state.reset_global_default()
        plugin.state.load_state()

        assert plugin.state.GLOBAL_DEFAULT["mode"] == gd_before["mode"]
        assert plugin.state.GLOBAL_DEFAULT["pinned_engine"] == gd_before["pinned_engine"]
        for sid in sess_before:
            assert sid in plugin.state.SESSION_STATE
            assert plugin.state.SESSION_STATE[sid]["mode"] == sess_before[sid]["mode"]
            assert (
                plugin.state.SESSION_STATE[sid]["ignore_global"]
                == sess_before[sid]["ignore_global"]
            )

    def test_corrupt_state_file_is_ignored(self, plugin, tmp_path):
        path = tmp_path / ".state.json"
        path.write_text("{not valid json")
        plugin.state.set_persistence_path(path)
        # Should not raise — corrupt file is logged + skipped.
        plugin.state.load_state()
        assert plugin.state.GLOBAL_DEFAULT["mode"] == "auto"
        assert plugin.state.SESSION_STATE == {}


# ---------------------------------------------------------------------------
# /browser-help
# ---------------------------------------------------------------------------


class TestBrowserHelp:
    """``/browser-help`` is the operator-facing docs surface on
    Telegram/Discord. These tests pin the contract that the overview
    references every engine and every other slash command, and that the
    per-topic drill-down works for both engine names and command names.
    """

    def test_overview_lists_all_engines(self, plugin):
        out = plugin.commands.cmd_browser_help("")
        for engine in ("profile:main", "camofox:main", "cloud", "fast_read"):
            assert engine in out, f"overview missing engine {engine!r}"

    def test_overview_lists_all_slash_commands(self, plugin):
        out = plugin.commands.cmd_browser_help("")
        for cmd in (
            "/browser-status",
            "/browser-auto",
            "/browser-chrome",
            "/browser-camofox",
            "/browser-cloud",
            "/browser-recover",
            "/browser-route",
            "/browser-sessions",
            "/browser-help",
        ):
            assert cmd in out, f"overview missing command {cmd!r}"

    def test_overview_lists_model_tools(self, plugin):
        out = plugin.commands.cmd_browser_help("")
        for tool in (
            "browser_policy_status",
            "browser_policy_set",
            "browser_policy_route",
        ):
            assert tool in out

    def test_overview_points_at_current_paths(self, plugin):
        out = plugin.commands.cmd_browser_help("")
        # Persistence path moved to ~/.hermes/state/ — the help text must
        # not regress to the legacy "next to plugin" claim.
        assert "~/.hermes/state/browser-policy-router/state.json" in out
        assert ".state.json next to" not in out
        assert "README.md" in out
        assert "config.yaml" in out

    def test_topic_engine_returns_focused_section(self, plugin):
        out = plugin.commands.cmd_browser_help("profile:main")
        assert out.startswith("engine profile:main")
        # Other engine bodies must not bleed in.
        assert "Camoufox in Docker" not in out
        assert "fast_read" not in out

    def test_topic_command_with_slash_prefix(self, plugin):
        # Operators paste ``/browser-help /browser-recover`` from chat —
        # both the slash and ``browser-`` prefix must be stripped.
        out = plugin.commands.cmd_browser_help("/browser-recover")
        assert out.startswith("/browser-recover")
        assert "kickstart" in out.lower()

    def test_topic_command_with_browser_prefix(self, plugin):
        out = plugin.commands.cmd_browser_help("browser-recover")
        assert out.startswith("/browser-recover")

    def test_topic_command_bare_name(self, plugin):
        out = plugin.commands.cmd_browser_help("status")
        assert out.startswith("/browser-status")

    def test_topic_local_is_aliased_to_its_own_section(self, plugin):
        # ``/browser-local`` is a kept-for-compat alias of ``/browser-chrome``
        # but gets its own help section so users searching for the alias
        # don't hit "unknown topic".
        out = plugin.commands.cmd_browser_help("local")
        assert "/browser-local" in out
        assert "alias" in out.lower()

    def test_unknown_topic_lists_available_ones(self, plugin):
        out = plugin.commands.cmd_browser_help("foobar")
        assert "No help topic" in out
        assert "engines:" in out
        assert "commands:" in out
        assert "profile:main" in out
        assert "recover" in out

    def test_topic_is_case_insensitive_and_whitespace_tolerant(self, plugin):
        out = plugin.commands.cmd_browser_help("  STATUS  ")
        assert out.startswith("/browser-status")
