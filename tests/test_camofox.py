"""Tests for the camofox:main engine: routing, env mutation, recovery,
localhost rewrite, slash commands."""

from __future__ import annotations

import json
import os

# ---------------------------------------------------------------------------
# decide_route picks up the engine from class config
# ---------------------------------------------------------------------------


class TestRoutingPicksCamofoxFromConfig:
    def test_x_com_routes_to_camofox_when_durable_local_login_says_so(self, plugin, camofox_config):
        session = plugin.state._empty_session()
        route = plugin.routing.decide_route("https://x.com/home", session, "auto", camofox_config)
        assert route.engine == "camofox:main"
        assert route.reason == "durable local login route"

    def test_localhost_routes_to_camofox_when_internal_debug_says_so(self, plugin, camofox_config):
        session = plugin.state._empty_session()
        route = plugin.routing.decide_route("localhost:5173", session, "auto", camofox_config)
        assert route.engine == "camofox:main"
        assert route.reason == "internal/debug route"

    def test_google_routes_to_profile_main_when_human_profile_login_says_so(
        self, plugin, camofox_config
    ):
        session = plugin.state._empty_session()
        route = plugin.routing.decide_route(
            "https://accounts.google.com/x", session, "auto", camofox_config
        )
        assert route.engine == "profile:main"
        assert route.reason == "human profile login route"

    def test_hint_camofox_wins_over_url_class(self, plugin, base_config):
        session = plugin.state._empty_session()
        route = plugin.routing.decide_route(
            "https://www.lemonde.fr/article", session, "camofox", base_config
        )
        assert route.engine == "camofox:main"
        assert route.reason == "forced by hint"

    def test_hint_chrome_alias_works(self, plugin, base_config):
        # "chrome" is a new alias for profile:main.
        session = plugin.state._empty_session()
        route = plugin.routing.decide_route(
            "https://news.ycombinator.com", session, "chrome", base_config
        )
        assert route.engine == "profile:main"

    def test_hint_camofox_full_form_works(self, plugin, base_config):
        session = plugin.state._empty_session()
        route = plugin.routing.decide_route("https://x.com", session, "camofox:main", base_config)
        assert route.engine == "camofox:main"


# ---------------------------------------------------------------------------
# apply_engine is strict (clear + set, never leave stale env)
# ---------------------------------------------------------------------------


class TestInvalidEngineGuards:
    """The adversarial reviewer noted: a typo in ``engine:`` used to leave
    the previously-applied env in place (silent regression). The guards
    in `_class_engine` and `apply_engine` now refuse unknown names.
    """

    def test_class_with_unknown_engine_is_skipped_by_decide_route(self, plugin, base_config):
        cfg = dict(base_config)
        cfg["classes"] = dict(base_config["classes"])
        cfg["classes"]["hard_login"] = {
            "engine": "typo-engine",
            "domains": ["x.com"],
        }
        session = plugin.state._empty_session()
        route = plugin.routing.decide_route("https://x.com/home", session, "auto", cfg)
        # Unknown engine -> class skipped -> falls through to default.
        assert route.engine == "cloud"
        assert route.reason == "default interactive route"

    def test_apply_engine_unknown_clears_env_loudly(self, plugin, monkeypatch):
        # Pre-set both env families so we can assert apply_engine strips them.
        monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")
        monkeypatch.setenv("CAMOFOX_URL", "http://127.0.0.1:9377")
        route = plugin.routing.Route(engine="typo-engine", reason="test")
        plugin.routing.apply_engine(route)
        assert "BROWSER_CDP_URL" not in os.environ
        assert "CAMOFOX_URL" not in os.environ

    def test_valid_engines_set_is_what_we_expect(self, plugin):
        assert (
            frozenset({"profile:main", "camofox:main", "cloud", "fast_read"})
            == plugin.routing.VALID_ENGINES
        )


class TestApplyEngineStrict:
    def test_apply_camofox_clears_cdp(self, plugin, monkeypatch):
        monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")
        route = plugin.routing.Route(
            engine="camofox:main",
            camofox_url="http://127.0.0.1:9377",
            camofox_user_id="hermes-main",
            camofox_session_key="main",
            camofox_adopt_existing_tab=True,
        )
        plugin.routing.apply_engine(route)
        assert "BROWSER_CDP_URL" not in os.environ
        assert os.environ["CAMOFOX_URL"] == "http://127.0.0.1:9377"
        assert os.environ["CAMOFOX_USER_ID"] == "hermes-main"
        assert os.environ["CAMOFOX_SESSION_KEY"] == "main"
        assert os.environ["CAMOFOX_ADOPT_EXISTING_TAB"] == "true"

    def test_apply_profile_main_clears_camofox(self, plugin, monkeypatch):
        monkeypatch.setenv("CAMOFOX_URL", "http://127.0.0.1:9377")
        monkeypatch.setenv("CAMOFOX_USER_ID", "hermes-main")
        route = plugin.routing.Route(
            engine="profile:main",
            cdp_url="http://127.0.0.1:9222",
        )
        plugin.routing.apply_engine(route)
        assert os.environ["BROWSER_CDP_URL"] == "http://127.0.0.1:9222"
        assert "CAMOFOX_URL" not in os.environ
        assert "CAMOFOX_USER_ID" not in os.environ

    def test_apply_cloud_clears_both(self, plugin, monkeypatch):
        monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")
        monkeypatch.setenv("CAMOFOX_URL", "http://127.0.0.1:9377")
        route = plugin.routing.Route(engine="cloud")
        plugin.routing.apply_engine(route)
        assert "BROWSER_CDP_URL" not in os.environ
        assert "CAMOFOX_URL" not in os.environ


# ---------------------------------------------------------------------------
# camofox_ready / ensure_camofox
# ---------------------------------------------------------------------------


class TestEnsureCamofox:
    def test_ready_returns_immediately(self, plugin, base_config, monkeypatch):
        monkeypatch.setattr(plugin.routing, "camofox_ready", lambda *_a, **_k: True)
        result = plugin.routing.ensure_camofox(base_config)
        assert result.ok
        assert not result.recovered

    def test_kicks_colima_and_docker_then_polls(self, plugin, base_config, monkeypatch):
        # First /health probe says DOWN; after kickstart/docker info/
        # docker start the next probe says UP -- exercising the
        # phased recovery code path added in v1.1.0-beta.2.
        calls = {"count": 0}

        def fake_ready(url, timeout=1.5):
            calls["count"] += 1
            return calls["count"] >= 2

        monkeypatch.setattr(plugin.routing, "camofox_ready", fake_ready)

        run_log: list[list[str]] = []

        class FakeProc:
            def __init__(self, stdout="", returncode=0, stderr=""):
                self.stdout = stdout
                self.returncode = returncode
                self.stderr = stderr

        def fake_run(args, **_kw):
            run_log.append(args)
            # `docker info --format ...` must report a non-empty
            # ServerVersion to declare the socket ready. Other calls
            # (launchctl, docker start) return empty stdout, success.
            if len(args) >= 2 and "info" in args:
                return FakeProc(stdout="20.10.0\n")
            return FakeProc()

        monkeypatch.setattr(plugin.routing.subprocess, "run", fake_run)

        result = plugin.routing.ensure_camofox(base_config)
        assert result.ok
        assert result.recovered
        # We should see launchctl kickstart, docker info, and docker start.
        assert any("kickstart" in " ".join(args) for args in run_log)
        assert any("info" in args for args in run_log)
        assert any("start" in args and "camofox-browser" in args for args in run_log)

    def test_recovery_fails_when_docker_socket_never_comes_up(
        self, plugin, base_config, monkeypatch
    ):
        # New behaviour: if `docker info` never succeeds, the recovery
        # bails out before trying to `docker start` (which would just
        # fail with "Cannot connect to the Docker daemon" anyway).
        monkeypatch.setattr(plugin.routing, "camofox_ready", lambda *_a, **_k: False)

        class FakeProc:
            returncode = 1
            stdout = ""
            stderr = "Cannot connect to the Docker daemon"

        run_log: list[list[str]] = []

        def fake_run(args, **_kw):
            run_log.append(args)
            return FakeProc()

        monkeypatch.setattr(plugin.routing.subprocess, "run", fake_run)
        result = plugin.routing.ensure_camofox(base_config)
        assert not result.ok
        assert "docker socket never answered" in result.reason
        # docker start should NOT have been attempted -- the socket
        # readiness probe gate stopped us.
        assert not any("start" in args and "camofox-browser" in args for args in run_log)

    def test_timeout_surfaces_reason(self, plugin, base_config, monkeypatch):
        # /health stays DOWN forever, but kickstart + docker info +
        # docker start all succeed -- so we exercise the /health
        # polling timeout (not the docker-socket-never-up branch).
        monkeypatch.setattr(plugin.routing, "camofox_ready", lambda *_a, **_k: False)

        class FakeProc:
            def __init__(self, stdout="", returncode=0, stderr=""):
                self.stdout = stdout
                self.returncode = returncode
                self.stderr = stderr

        def fake_run(args, **_kw):
            # `docker info --format ...` must report a non-empty server
            # version for `_docker_socket_ready` to clear; everything
            # else (launchctl, docker start) returns success/empty.
            if len(args) >= 2 and "info" in args:
                return FakeProc(stdout="20.10.0\n")
            return FakeProc()

        monkeypatch.setattr(plugin.routing.subprocess, "run", fake_run)
        result = plugin.routing.ensure_camofox(base_config)
        assert not result.ok
        assert "not ready" in result.reason

    def test_recovery_disabled_short_circuits(self, plugin, base_config, monkeypatch):
        cfg = dict(base_config)
        cfg["camofox"] = dict(base_config["camofox"])
        cfg["camofox"]["recovery_enabled"] = False
        monkeypatch.setattr(plugin.routing, "camofox_ready", lambda *_a, **_k: False)
        boom = {"called": False}

        def _boom(*_a, **_k):
            boom["called"] = True
            raise AssertionError("subprocess.run must not be called")

        monkeypatch.setattr(plugin.routing.subprocess, "run", _boom)
        result = plugin.routing.ensure_camofox(cfg)
        assert not result.ok
        assert "recovery disabled" in result.reason
        assert boom["called"] is False


# ---------------------------------------------------------------------------
# ensure_route_available for camofox:main
# ---------------------------------------------------------------------------


class TestEnsureRouteAvailableCamofox:
    def test_camofox_up_hydrates_fields(self, plugin, base_config, monkeypatch):
        monkeypatch.setattr(
            plugin.routing,
            "ensure_camofox",
            lambda cfg: plugin.routing.RecoveryResult(ok=True, recovered=False),
        )
        route = plugin.routing.Route(engine="camofox:main", reason="durable local login route")
        result = plugin.routing.ensure_route_available(route, base_config)
        assert result.engine == "camofox:main"
        assert result.camofox_url == "http://127.0.0.1:9377"
        assert result.camofox_user_id == "hermes-main"
        assert result.camofox_session_key == "main"
        assert result.camofox_adopt_existing_tab is True

    def test_camofox_down_internal_debug_falls_back_to_chrome_if_up(
        self, plugin, base_config, monkeypatch
    ):
        # Camofox down, Chrome up → internal_debug routes to profile:main
        # so localhost work survives even if the container is dead.
        monkeypatch.setattr(
            plugin.routing,
            "ensure_camofox",
            lambda cfg: plugin.routing.RecoveryResult(ok=False, recovered=False, reason="sim down"),
        )
        monkeypatch.setattr(plugin.routing, "cdp_ready", lambda *_a, **_k: True)
        route = plugin.routing.Route(engine="camofox:main", reason="internal/debug route")
        result = plugin.routing.ensure_route_available(route, base_config)
        assert result.engine == "profile:main"
        assert result.fallback_from == "camofox:main"

    def test_camofox_down_internal_debug_no_chrome_returns_error(
        self, plugin, base_config, monkeypatch
    ):
        # Camofox down AND Chrome down → internal_debug never falls back
        # to cloud (cloud can't see localhost).
        monkeypatch.setattr(
            plugin.routing,
            "ensure_camofox",
            lambda cfg: plugin.routing.RecoveryResult(ok=False, recovered=False, reason="sim down"),
        )
        monkeypatch.setattr(plugin.routing, "cdp_ready", lambda *_a, **_k: False)
        route = plugin.routing.Route(engine="camofox:main", reason="internal/debug route")
        result = plugin.routing.ensure_route_available(route, base_config)
        assert result.engine == "camofox:main"
        assert result.error == "sim down"

    def test_camofox_down_durable_local_login_falls_back_to_chrome_if_up(
        self, plugin, base_config, monkeypatch
    ):
        monkeypatch.setattr(
            plugin.routing,
            "ensure_camofox",
            lambda cfg: plugin.routing.RecoveryResult(ok=False, recovered=False, reason="sim down"),
        )
        monkeypatch.setattr(plugin.routing, "cdp_ready", lambda *_a, **_k: True)
        route = plugin.routing.Route(engine="camofox:main", reason="durable local login route")
        result = plugin.routing.ensure_route_available(route, base_config)
        assert result.engine == "profile:main"
        assert result.fallback_from == "camofox:main"

    def test_camofox_down_durable_local_login_falls_back_to_cloud_if_no_chrome(
        self, plugin, base_config, monkeypatch
    ):
        monkeypatch.setattr(
            plugin.routing,
            "ensure_camofox",
            lambda cfg: plugin.routing.RecoveryResult(ok=False, recovered=False, reason="sim down"),
        )
        monkeypatch.setattr(plugin.routing, "cdp_ready", lambda *_a, **_k: False)
        route = plugin.routing.Route(engine="camofox:main", reason="durable local login route")
        result = plugin.routing.ensure_route_available(route, base_config)
        assert result.engine == "cloud"
        assert result.fallback_from == "camofox:main"


# ---------------------------------------------------------------------------
# localhost rewrite — only for camofox:main
# ---------------------------------------------------------------------------


class TestLocalhostRewrite:
    def test_rewrites_localhost_to_host_docker_internal(self, plugin, base_config):
        new, target = plugin.routing.maybe_rewrite_localhost_for_camofox(
            "http://localhost:8765/foo", base_config
        )
        assert target == "host.docker.internal"
        assert new == "http://host.docker.internal:8765/foo"

    def test_rewrites_127_0_0_1(self, plugin, base_config):
        new, target = plugin.routing.maybe_rewrite_localhost_for_camofox(
            "http://127.0.0.1:3000/x", base_config
        )
        assert target == "host.docker.internal"
        assert new == "http://host.docker.internal:3000/x"

    def test_does_not_rewrite_external_hosts(self, plugin, base_config):
        new, target = plugin.routing.maybe_rewrite_localhost_for_camofox(
            "https://x.com/home", base_config
        )
        assert target is None
        assert new == "https://x.com/home"

    def test_disabled_means_no_rewrite(self, plugin, base_config):
        cfg = dict(base_config)
        cfg["camofox"] = dict(base_config["camofox"])
        cfg["camofox"]["localhost_rewrite"] = {"enabled": False}
        new, target = plugin.routing.maybe_rewrite_localhost_for_camofox(
            "http://localhost:8765/x", cfg
        )
        assert target is None
        assert new == "http://localhost:8765/x"

    def test_no_port_preserved(self, plugin, base_config):
        new, target = plugin.routing.maybe_rewrite_localhost_for_camofox(
            "http://localhost/foo", base_config
        )
        assert target == "host.docker.internal"
        assert new == "http://host.docker.internal/foo"


# ---------------------------------------------------------------------------
# Wrapper rewrites localhost only when engine is camofox:main
# ---------------------------------------------------------------------------


class TestWrapperLocalhostRewrite:
    def test_navigate_with_camofox_rewrites_localhost(
        self, plugin, camofox_config, camofox_available, monkeypatch
    ):
        monkeypatch.setattr(
            plugin.routing,
            "load_config",
            lambda reload=False: camofox_config,
        )

        captured = {}

        class FakeBT:
            @staticmethod
            def browser_navigate(url, task_id=None):
                captured["url"] = url
                return '{"ok": true}'

        monkeypatch.setattr(plugin.wrappers, "_bt", lambda: FakeBT)

        out = plugin.wrappers.browser_navigate_wrapped(
            {"url": "http://localhost:8765/x"}, task_id="t1"
        )
        payload = json.loads(out)
        assert captured["url"] == "http://host.docker.internal:8765/x"
        assert payload["_router"]["browser_url"] == "http://host.docker.internal:8765/x"
        assert payload["_router"]["url"] == "http://localhost:8765/x"
        assert payload["_router"]["engine"] == "camofox:main"
        assert "rewrote localhost" in payload["_router"]["reason"]

    def test_navigate_with_profile_main_keeps_localhost(
        self, plugin, base_config, local_chrome_available, monkeypatch
    ):
        # base_config has internal_debug -> profile:main, so localhost
        # should NOT get rewritten — Chrome can reach the host loopback.
        monkeypatch.setattr(
            plugin.routing,
            "load_config",
            lambda reload=False: base_config,
        )
        monkeypatch.setattr(plugin.routing, "ensure_route_available", lambda r, c: r)

        captured = {}

        class FakeBT:
            @staticmethod
            def browser_navigate(url, task_id=None):
                captured["url"] = url
                return '{"ok": true}'

        monkeypatch.setattr(plugin.wrappers, "_bt", lambda: FakeBT)

        plugin.wrappers.browser_navigate_wrapped({"url": "http://localhost:5173/x"}, task_id="t2")
        assert captured["url"] == "http://localhost:5173/x"


# ---------------------------------------------------------------------------
# Slash command: /browser-camofox
# ---------------------------------------------------------------------------


class FakeSessionDB:
    def __init__(self, titles=None):
        self._titles = titles or {"my-research": "s_abc123"}

    def resolve_session_id(self, x):
        return x if x.startswith("s_") and len(x) > 3 else None

    def resolve_session_by_title(self, title):
        return self._titles.get(title)


def _install_hermes_state(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "hermes_state", type("M", (), {"SessionDB": FakeSessionDB}))


class TestSlashBrowserCamofox:
    def test_global_pin_to_camofox(self, plugin, monkeypatch, camofox_available):
        out = plugin.commands.cmd_browser_camofox("")
        assert "Camofox" in out
        assert plugin.state.GLOBAL_DEFAULT["mode"] == "pinned"
        assert plugin.state.GLOBAL_DEFAULT["pinned_engine"] == "camofox:main"

    def test_session_pin_to_camofox(self, plugin, monkeypatch, camofox_available):
        _install_hermes_state(monkeypatch)
        out = plugin.commands.cmd_browser_camofox("my-research")
        assert "s_abc123" in out
        sess = plugin.state.SESSION_STATE["s_abc123"]
        assert sess["mode"] == "pinned"
        assert sess["pinned_engine"] == "camofox:main"

    def test_pin_when_camofox_down_returns_error(self, plugin, monkeypatch):
        monkeypatch.setattr(
            plugin.routing,
            "ensure_camofox",
            lambda cfg: plugin.routing.RecoveryResult(
                ok=False, recovered=False, reason="not running"
            ),
        )
        out = plugin.commands.cmd_browser_camofox("")
        assert "Camofox unavailable" in out
        # Global default not pinned
        assert plugin.state.GLOBAL_DEFAULT["mode"] == "auto"


class TestSlashBrowserStatusShowsCamofox:
    def test_status_includes_camofox_section(self, plugin, monkeypatch, base_config):
        monkeypatch.setattr(plugin.routing, "load_config", lambda reload=False: base_config)
        monkeypatch.setattr(plugin.routing, "cdp_ready", lambda *_a, **_k: False)
        monkeypatch.setattr(plugin.routing, "camofox_ready", lambda *_a, **_k: True)
        monkeypatch.setattr(plugin.routing, "launchctl_state", lambda label: "running")
        out = plugin.commands.cmd_browser_status("")
        assert "Camofox: OK" in out
        assert "noVNC:" in out


# ---------------------------------------------------------------------------
# browser_policy_set accepts camofox:main
# ---------------------------------------------------------------------------


class TestBrowserPolicySetCamofox:
    def test_set_camofox_main_pins(self, plugin, monkeypatch, base_config, camofox_available):
        monkeypatch.setattr(plugin.routing, "load_config", lambda reload=False: base_config)
        out = plugin.tools.browser_policy_set({"engine": "camofox:main"}, task_id="some-task")
        payload = json.loads(out)
        assert payload["success"] is True
        assert payload["pinned_engine"] == "camofox:main"
        assert payload["camofox_url"] == "http://127.0.0.1:9377"

    def test_set_camofox_main_when_down_errors(self, plugin, monkeypatch, base_config):
        monkeypatch.setattr(plugin.routing, "load_config", lambda reload=False: base_config)
        monkeypatch.setattr(
            plugin.routing,
            "ensure_camofox",
            lambda cfg: plugin.routing.RecoveryResult(
                ok=False, recovered=False, reason="not running"
            ),
        )
        out = plugin.tools.browser_policy_set({"engine": "camofox:main"}, task_id="some-task")
        payload = json.loads(out)
        assert payload["success"] is False
        assert "Camofox unavailable" in payload["error"]


# ---------------------------------------------------------------------------
# Follow-up wrappers reuse camofox:main route
# ---------------------------------------------------------------------------


class TestFollowUpWithCamofoxRoute:
    def test_follow_up_reuses_camofox(self, plugin, monkeypatch, base_config):
        monkeypatch.setattr(plugin.routing, "load_config", lambda reload=False: base_config)
        session = plugin.state.get_session("camo-session")
        session["last_engine"] = "camofox:main"
        session["camofox_url"] = "http://127.0.0.1:9377"

        called = {"value": False}

        def _delegate():
            called["value"] = True
            # apply_engine should have set CAMOFOX_URL by now
            assert os.environ.get("CAMOFOX_URL") == "http://127.0.0.1:9377"
            return '{"ok": true}'

        out = plugin.wrappers._follow_up(
            "browser_snapshot", {}, {"task_id": "camo-session"}, _delegate
        )
        assert called["value"] is True
        assert "ok" in out
