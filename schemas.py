"""Tool schemas exposed to the model."""

from __future__ import annotations

BROWSER_POLICY_STATUS = {
    "name": "browser_policy_status",
    "description": (
        "Show the current browser routing policy for this session: mode "
        "(auto/pinned), pinned engine if any, last engine used, last reason, "
        "local Chrome CDP readiness, Camofox /health readiness, and configured "
        "cloud provider. Call this before/after browser work to confirm where "
        "the next browser_navigate will run."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


BROWSER_POLICY_SET = {
    "name": "browser_policy_set",
    "description": (
        "Pin or unpin the browser engine for this session. "
        "engine='profile:main' uses the local logged-in Chrome profile via "
        "BROWSER_CDP_URL on 127.0.0.1:9222 (GUI-bound). "
        "engine='camofox:main' uses the durable local Camofox/Camoufox browser "
        "running in Docker via noVNC; persists across reboots and GUI logout. "
        "engine='cloud' unsets BROWSER_CDP_URL and CAMOFOX_URL so the configured "
        "cloud browser (expected: Browserbase) is used. "
        "engine='auto' makes this session genuinely URL-driven and stops it "
        "from inheriting any global default pin set by a slash command — use "
        "this when you explicitly want the router to classify each URL on its "
        "own."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "engine": {
                "type": "string",
                "enum": ["profile:main", "camofox:main", "cloud", "auto"],
                "description": (
                    "Engine to pin, or 'auto' to enable URL-driven routing "
                    "for this session (also overrides any global default "
                    "pin)."
                ),
            },
        },
        "required": ["engine"],
    },
}


BROWSER_POLICY_ROUTE = {
    "name": "browser_policy_route",
    "description": (
        "Decide and apply the routing for a URL according to the current "
        "policy and pinned mode, then return the chosen engine. "
        "Call this BEFORE browser_navigate when you want explicit routing "
        "control (e.g. unsupervised auto runs). The engine chosen is one of "
        "'profile:main' (local Chrome), 'camofox:main' (durable local Camofox "
        "in Docker), 'cloud' (Browserbase), or 'fast_read' (web_extract/"
        "web_search chain). hint='auto' (default) lets the policy decide; "
        "hint='chrome'|'camofox'|'cloud'|'fast_read' forces the choice and "
        "latches it as a one-shot for the next browser_navigate on this "
        "session."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Target URL to be navigated.",
            },
            "hint": {
                "type": "string",
                "enum": ["auto", "chrome", "camofox", "cloud", "fast_read"],
                "description": "Optional override; defaults to 'auto'.",
            },
        },
        "required": ["url"],
    },
}
