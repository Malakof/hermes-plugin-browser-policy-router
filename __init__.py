"""browser-policy-router plugin.

Routes Hermes browser work between the local logged-in Chrome profile
(``BROWSER_CDP_URL`` -> 127.0.0.1:9222), the configured cloud browser
(expected: Browserbase), and fast read/extract tools, without modifying
upstream Hermes code.

Only registration lives here. All side-effecting work (env mutation,
launchctl kickstart, CDP probes) happens inside tool/command handlers,
not at import time.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

# The Hermes plugin loader and our test conftest both load this module
# with ``importlib.util.spec_from_file_location(..., submodule_search_locations=[...])``,
# which sets ``__package__`` so relative imports resolve. When pytest
# walks up from a test file and accidentally tries to import this file
# as a free-standing module (``__package__`` is empty), the relative
# import fails harmlessly; the submodules are no-ops and ``register()``
# is never called in that context.
if TYPE_CHECKING:
    from . import commands, routing, schemas, state, tools, wrappers  # noqa: F401
else:
    try:
        from . import commands, schemas, state, tools, wrappers
    except ImportError:  # pragma: no cover - non-package import contexts only
        # Provide stubs so pyright still treats the symbols as defined.
        import types

        _stub = types.ModuleType("_unloaded")
        commands = schemas = state = tools = wrappers = _stub

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    # Restore persisted SESSION_STATE + GLOBAL_DEFAULT (per-session pins
    # and the slash-command global default survive gateway restarts).
    try:
        state.load_state()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "browser-policy-router: could not restore persisted state (%s); starting from defaults",
            exc,
        )
    ctx.register_tool(
        name="browser_policy_status",
        toolset="browser_policy_router",
        schema=schemas.BROWSER_POLICY_STATUS,
        handler=tools.browser_policy_status,
        description="Show browser routing status",
        emoji="🧭",
    )
    ctx.register_tool(
        name="browser_policy_set",
        toolset="browser_policy_router",
        schema=schemas.BROWSER_POLICY_SET,
        handler=tools.browser_policy_set,
        description="Pin browser engine for this session",
        emoji="📌",
    )
    ctx.register_tool(
        name="browser_policy_route",
        toolset="browser_policy_router",
        schema=schemas.BROWSER_POLICY_ROUTE,
        handler=tools.browser_policy_route,
        description="Decide local/cloud/fast-read route for a URL",
        emoji="🧭",
    )

    # ---- Phase 3b: transparent browser_* overrides ----
    try:
        schema_map = wrappers.get_browser_schemas()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "browser-policy-router: could not load built-in browser schemas (%s); "
            "skipping transparent wrappers",
            exc,
        )
        schema_map = {}

    check_fn = wrappers.get_check_browser_requirements()

    wrapper_specs = [
        ("browser_navigate", wrappers.browser_navigate_wrapped, "🌐"),
        ("browser_snapshot", wrappers.browser_snapshot_wrapped, "📸"),
        ("browser_click", wrappers.browser_click_wrapped, "👆"),
        ("browser_type", wrappers.browser_type_wrapped, "⌨️"),
        ("browser_scroll", wrappers.browser_scroll_wrapped, "📜"),
        ("browser_back", wrappers.browser_back_wrapped, "◀️"),
        ("browser_press", wrappers.browser_press_wrapped, "⌨️"),
        ("browser_console", wrappers.browser_console_wrapped, "🖥️"),
        ("browser_vision", wrappers.browser_vision_wrapped, "👁️"),
        ("browser_get_images", wrappers.browser_get_images_wrapped, "🖼️"),
    ]
    for name, handler, emoji in wrapper_specs:
        schema = schema_map.get(name)
        if schema is None:
            logger.warning(
                "browser-policy-router: no built-in schema for %s; skipping override",
                name,
            )
            continue
        ctx.register_tool(
            name=name,
            toolset="browser_policy_router",
            schema=schema,
            handler=handler,
            check_fn=check_fn,
            description=f"{name} (routed via browser-policy-router)",
            emoji=emoji,
            override=True,
        )

    ctx.register_command(
        "browser-status",
        handler=commands.cmd_browser_status,
        description="Browser router status (optionally for a session name)",
        args_hint="[session-name]",
    )
    ctx.register_command(
        "browser-auto",
        handler=commands.cmd_browser_auto,
        description="Use automatic browser policy (global, or for one session)",
        args_hint="[session-name]",
    )
    ctx.register_command(
        "browser-local",
        handler=commands.cmd_browser_local,
        description="Pin to local Chrome profile (global, or for one session)",
        args_hint="[session-name]",
    )
    ctx.register_command(
        "browser-cloud",
        handler=commands.cmd_browser_cloud,
        description="Pin to configured cloud browser (global, or for one session)",
        args_hint="[session-name]",
    )
    ctx.register_command(
        "browser-recover",
        handler=commands.cmd_browser_recover,
        description="Recover local Chrome profile via launchctl",
    )
    ctx.register_command(
        "browser-route",
        handler=commands.cmd_browser_route,
        description="Show the route for a URL (optionally in a specific session)",
        args_hint="[<session-name>] <url>",
    )
    ctx.register_command(
        "browser-sessions",
        handler=commands.cmd_browser_sessions,
        description="List recent sessions with their browser policy state",
    )

    commands.set_plugin_context(ctx)
    wrappers.set_plugin_context(ctx)
    logger.info("browser-policy-router plugin loaded")
