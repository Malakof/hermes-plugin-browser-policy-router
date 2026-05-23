"""Tests for the transparent ``browser_*`` wrappers and the fast-read chain."""

from __future__ import annotations

import json


class FakeCtx:
    """Minimal PluginContext stand-in: just ``dispatch_tool``."""

    def __init__(self, dispatch):
        self._dispatch = dispatch

    def dispatch_tool(self, name, args, **_kw):
        return self._dispatch(name, args)


class TestFastReadChain:
    def test_chain_stops_on_first_success(self, plugin, base_config):
        calls = []

        def dispatch(name, args):
            calls.append(name)
            if name == "web_extract":
                return json.dumps({"content": "extracted body"})
            raise AssertionError("should not have advanced past web_extract")

        plugin.wrappers.set_plugin_context(FakeCtx(dispatch))
        result = plugin.wrappers.try_fast_read_chain("https://en.wikipedia.org/x", base_config)
        assert result["ok"] is True
        assert result["tool"] == "web_extract"
        assert calls == ["web_extract"]

    def test_chain_advances_on_empty_content(self, plugin, base_config):
        def dispatch(name, args):
            if name == "web_extract":
                return json.dumps({"content": ""})
            if name == "web_search":
                return json.dumps({"results": [{"snippet": "from search"}]})
            raise AssertionError(f"unexpected tool {name!r}")

        plugin.wrappers.set_plugin_context(FakeCtx(dispatch))
        result = plugin.wrappers.try_fast_read_chain("https://news.ycombinator.com/", base_config)
        assert result["ok"] is True
        assert result["tool"] == "web_search"
        # Both attempts were recorded
        names = [a["tool"] for a in result["attempts"]]
        assert "web_extract" in names and "web_search" in names

    def test_chain_handoff_to_cloud_browser_sentinel(self, plugin, base_config):
        def dispatch(name, args):
            return json.dumps({"error": "sim"})

        plugin.wrappers.set_plugin_context(FakeCtx(dispatch))
        result = plugin.wrappers.try_fast_read_chain("https://example.org/", base_config)
        assert result["ok"] is False
        assert result["terminal"] is True
        # Last attempt is the cloud_browser sentinel
        assert result["attempts"][-1]["tool"] == "cloud_browser"

    def test_chain_handles_dispatch_exceptions(self, plugin, base_config):
        def dispatch(name, args):
            raise RuntimeError(f"boom on {name}")

        plugin.wrappers.set_plugin_context(FakeCtx(dispatch))
        result = plugin.wrappers.try_fast_read_chain("https://wiki.example/", base_config)
        # All attempts recorded their errors
        for a in result["attempts"]:
            if a["tool"] != "cloud_browser":
                assert a["ok"] is False


class TestFastReadResponse:
    def test_response_marks_requires_browser_for_interaction(self, plugin):
        # Build a fake fast result and Route, then assert the JSON shape
        route = plugin.routing.Route(engine="fast_read", reason="public read route")
        fast = {"tool": "web_extract", "content": "body", "attempts": []}
        raw = plugin.wrappers._fast_read_response("https://x.example/", route, fast)
        payload = json.loads(raw)
        assert payload["engine"] == "fast_read"
        assert payload["tool_used"] == "web_extract"
        assert payload["requires_browser_for_interaction"] is True
        assert "browser_policy_set" in payload["note"]


class TestExplicitEngineHonored:
    def test_explicit_fast_read_makes_x_com_go_fast_read(self, plugin, base_config):
        # x.com is hard_login by default, but explicit_engine="fast_read"
        # should win at priority 2 and the wrapper should run the chain.
        session = plugin.state._empty_session()
        session["explicit_engine"] = "fast_read"
        route = plugin.routing.decide_route(
            "https://x.com/home", session, "auto", base_config, consume_explicit=True
        )
        assert route.engine == "fast_read"
        assert session["explicit_engine"] is None  # consumed


class TestFollowUpRefusesAfterFastRead:
    """High-severity safety: after a fast_read, follow-up tools must NOT
    silently delegate (BROWSER_CDP_URL may still point at another
    session's Chrome from an earlier turn).
    """

    def test_follow_up_returns_error_when_last_engine_is_fast_read(self, plugin):
        session = plugin.state.get_session("after-fast-read")
        session["last_engine"] = "fast_read"
        session["last_url"] = "https://en.wikipedia.org/wiki/X"

        delegated = {"called": False}

        def _boom():
            delegated["called"] = True
            return "{}"

        out = plugin.wrappers._follow_up(
            "browser_snapshot", {}, {"task_id": "after-fast-read"}, _boom
        )
        import json

        payload = json.loads(out)
        assert delegated["called"] is False
        assert payload["success"] is False
        assert payload["requires_browser_for_interaction"] is True
        assert "fast_read" in payload["error"]

    def test_follow_up_returns_error_when_no_prior_navigate(self, plugin):
        # Fresh session: no prior browser_navigate, so wrapper must refuse.
        delegated = {"called": False}

        def _boom():
            delegated["called"] = True
            return "{}"

        out = plugin.wrappers._follow_up(
            "browser_click", {"ref": "@e1"}, {"task_id": "fresh-session"}, _boom
        )
        import json

        payload = json.loads(out)
        assert delegated["called"] is False
        assert payload["success"] is False
        assert "browser_navigate" in payload["error"]

    def test_follow_up_delegates_when_last_engine_is_real(self, plugin):
        session = plugin.state.get_session("with-route")
        session["last_engine"] = "profile:main"
        session["cdp_url"] = "http://127.0.0.1:9222"

        called = {"value": False}

        def _delegate():
            called["value"] = True
            return '{"ok": true}'

        out = plugin.wrappers._follow_up(
            "browser_snapshot", {}, {"task_id": "with-route"}, _delegate
        )
        assert called["value"] is True
        assert "ok" in out


class TestExplicitEnginePersistedOnConsumption:
    """Medium-severity: consumed explicit_engine must be flushed to disk
    so a gateway restart cannot replay the one-shot.
    """

    def test_save_state_called_after_navigate_via_fast_read(self, plugin, monkeypatch):
        # Pre-populate a latched fast_read hint for a session
        session = plugin.state.get_session("hint-session")
        session["explicit_engine"] = "fast_read"

        saves = {"count": 0}

        original_save = plugin.state.save_state

        def counting_save():
            saves["count"] += 1
            original_save()

        monkeypatch.setattr(plugin.state, "save_state", counting_save)

        # Mock the fast-read chain to succeed quickly
        plugin.wrappers.set_plugin_context(
            type("Ctx", (), {"dispatch_tool": lambda self, n, a, **kw: '{"content": "x"}'})()
        )

        out = plugin.wrappers.browser_navigate_wrapped(
            {"url": "https://x.com/home"}, task_id="hint-session"
        )
        # explicit_engine was consumed by decide_route + path persisted
        assert plugin.state.SESSION_STATE["hint-session"]["explicit_engine"] is None
        assert saves["count"] >= 1, "save_state must be called after one-shot consumption"
        import json

        assert json.loads(out)["engine"] == "fast_read"

    def test_save_state_called_after_normal_navigate(self, plugin, monkeypatch):
        session = plugin.state.get_session("normal-nav")
        session["explicit_engine"] = "cloud"

        saves = {"count": 0}
        original_save = plugin.state.save_state

        def counting_save():
            saves["count"] += 1
            original_save()

        monkeypatch.setattr(plugin.state, "save_state", counting_save)

        # Stub the underlying browser_navigate so we don't actually run Chrome.
        called = {"value": False}

        class FakeBT:
            @staticmethod
            def browser_navigate(url, task_id=None):
                called["value"] = True
                return '{"ok": true}'

        monkeypatch.setattr(plugin.wrappers, "_bt", lambda: FakeBT)
        # Skip recovery probe
        monkeypatch.setattr(plugin.routing, "ensure_route_available", lambda r, c: r)

        plugin.wrappers.browser_navigate_wrapped(
            {"url": "https://x.com/home"}, task_id="normal-nav"
        )
        assert plugin.state.SESSION_STATE["normal-nav"]["explicit_engine"] is None
        assert saves["count"] >= 1
        assert called["value"] is True
