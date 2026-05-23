"""Tests for local-profile recovery and routing fallback semantics."""

from __future__ import annotations


class TestEnsureRouteAvailable:
    def test_local_recovery_fails_for_internal_debug_keeps_error(
        self, plugin, base_config, monkeypatch
    ):
        # CDP probe simulated DOWN — recovery fails.
        monkeypatch.setattr(plugin.routing, "cdp_ready", lambda *_a, **_k: False)
        monkeypatch.setattr(
            plugin.routing,
            "ensure_local_profile",
            lambda cfg: plugin.routing.RecoveryResult(
                ok=False, recovered=False, reason="sim CDP down"
            ),
        )
        # Build an internal/debug route manually
        route = plugin.routing.Route(engine="profile:main", reason="internal/debug route")
        result = plugin.routing.ensure_route_available(route, base_config)
        # Internal/debug never falls back to cloud (the cloud can't see localhost).
        assert result.engine == "profile:main"
        assert result.error == "sim CDP down"

    def test_local_recovery_fails_for_hard_login_falls_back_to_cloud(
        self, plugin, base_config, monkeypatch
    ):
        monkeypatch.setattr(
            plugin.routing,
            "ensure_local_profile",
            lambda cfg: plugin.routing.RecoveryResult(
                ok=False, recovered=False, reason="sim CDP down"
            ),
        )
        route = plugin.routing.Route(engine="profile:main", reason="hard-login route")
        result = plugin.routing.ensure_route_available(route, base_config)
        assert result.engine == "cloud"
        assert result.fallback_from == "profile:main"
        assert "sim CDP down" in result.reason

    def test_local_recovery_succeeds_keeps_route(self, plugin, base_config, monkeypatch):
        monkeypatch.setattr(
            plugin.routing,
            "ensure_local_profile",
            lambda cfg: plugin.routing.RecoveryResult(ok=True, recovered=False),
        )
        route = plugin.routing.Route(engine="profile:main", reason="hard-login route")
        result = plugin.routing.ensure_route_available(route, base_config)
        assert result.engine == "profile:main"
        assert result.cdp_url == "http://127.0.0.1:9222"
        assert result.error is None

    def test_local_recovery_succeeds_after_kickstart_annotates_reason(
        self, plugin, base_config, monkeypatch
    ):
        monkeypatch.setattr(
            plugin.routing,
            "ensure_local_profile",
            lambda cfg: plugin.routing.RecoveryResult(ok=True, recovered=True),
        )
        route = plugin.routing.Route(engine="profile:main", reason="hard-login route")
        result = plugin.routing.ensure_route_available(route, base_config)
        assert "recovered local Chrome" in result.reason

    def test_non_local_route_passes_through_unchanged(self, plugin, base_config, monkeypatch):
        # Cloud route should not invoke ensure_local_profile.
        def _boom(cfg):
            raise AssertionError("ensure_local_profile must not be called for cloud")

        monkeypatch.setattr(plugin.routing, "ensure_local_profile", _boom)
        route = plugin.routing.Route(engine="cloud", reason="forced by hint")
        result = plugin.routing.ensure_route_available(route, base_config)
        assert result.engine == "cloud"


class TestEnsureLocalProfileSubprocess:
    def test_kickstart_failure_surfaces_stderr_in_reason(self, plugin, base_config, monkeypatch):
        """`launchctl kickstart -k` non-zero exit must report stderr."""

        class FakeCompleted:
            returncode = 1
            stdout = ""
            stderr = "boot-out: 5: Input/output error"

        def fake_run(*_a, **_kw):
            return FakeCompleted()

        # CDP probe says DOWN so we enter the kickstart path.
        monkeypatch.setattr(plugin.routing, "cdp_ready", lambda *_a, **_k: False)
        monkeypatch.setattr(plugin.routing.subprocess, "run", fake_run)
        recovery = plugin.routing.ensure_local_profile(base_config)
        assert recovery.ok is False
        assert "rc=1" in recovery.reason
        assert "boot-out" in recovery.reason

    def test_kickstart_timeout_surfaces_in_reason(self, plugin, base_config, monkeypatch):
        # kickstart returns 0, but CDP never comes up — should timeout.
        class FakeCompleted:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr(plugin.routing.subprocess, "run", lambda *a, **k: FakeCompleted())
        monkeypatch.setattr(plugin.routing, "cdp_ready", lambda *_a, **_k: False)
        # ``recovery_timeout_s: 1`` and ``poll: 0.05`` in conftest config
        recovery = plugin.routing.ensure_local_profile(base_config)
        assert recovery.ok is False
        assert "cdp not ready" in recovery.reason
