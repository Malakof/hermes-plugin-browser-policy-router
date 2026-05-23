# hermes-plugin-browser-policy-router

Hermes plugin that routes browser work between:

- a local logged-in Chrome profile exposed through CDP;
- the configured Hermes cloud browser provider (expected: Browserbase);
- fast read tools for public pages (`web_extract`, `web_search`).

The plugin does not patch Hermes upstream code. It overrides the built-in
`browser_*` tools with `override=True`, sets or unsets `BROWSER_CDP_URL` before
delegating to the built-in browser implementation, and exposes slash commands
for manual browser policy changes.

## What problem this solves

Hermes normally has one active browser backend per process. That is awkward when
different jobs need different browser identities:

- public pages should be read quickly with search or extract tools;
- paid news sites may work best through the configured cloud browser;
- X, LinkedIn, Gmail, local dev apps, and other hard-login targets often need a
  real Chrome profile that the human user has already logged into;
- while debugging, a user may want to pin one conversation to local Chrome until
  they explicitly switch it back.

This plugin gives Hermes a policy layer. `browser_navigate` chooses an engine
from the URL and current session policy, then follow-up tools such as
`browser_click` and `browser_snapshot` reuse that route. No upstream Hermes code
changes are required.

## Routing model

For each `browser_navigate(url)`, the first matching rule wins:

1. A one-shot hint from `browser_policy_route(url, hint=...)`.
2. A per-session pin set by `browser_policy_set(...)` or a targeted slash
   command such as `/browser_local my-session`.
3. A global slash-command pin set by `/browser_local` or `/browser_cloud`.
4. URL classes from `config.yaml`:
   - `internal_debug` -> `profile:main`
   - `hard_login` -> `profile:main`
   - `subscription_login` -> `cloud`
   - `public_read` -> `fast_read`
5. `default_interactive_engine`, usually `cloud`.

`/browser_auto <session-name>` is a real per-session reset: that session becomes
URL-driven and ignores any global pin until it is pinned again.

## Install

From a Hermes host with `hermes_cli` available:

```bash
hermes plugins install Malakof/hermes-plugin-browser-policy-router --enable
```

This clones the repo into `~/.hermes/plugins/browser-policy-router/`, adds it
to `plugins.enabled`, and registers the tools at the next gateway startup.

Configuration is local:

```bash
cp ~/.hermes/plugins/browser-policy-router/config.yaml.example \
   ~/.hermes/plugins/browser-policy-router/config.yaml
$EDITOR ~/.hermes/plugins/browser-policy-router/config.yaml
```

Then restart the gateway:

```bash
sudo launchctl kickstart -k system/com.hermes.gateway-richard
```

Check that it loaded:

```bash
hermes plugins list | grep browser-policy-router || true
tail -80 ~/.hermes/logs/agent.log | grep browser_policy_router
```

Some Hermes versions under-report local standalone plugins in `plugins list`.
The `agent.log` line `browser-policy-router plugin loaded` and the
`browser_* ... overriding existing toolset 'browser'` lines are the
authoritative runtime check.

## Tools (model-facing)

- `browser_policy_status` — show current routing state for this session
- `browser_policy_set` — pin the session's engine (or restore auto)
- `browser_policy_route` — decide and apply a route for one URL, with an
  optional `hint` that latches as a one-shot for the next `browser_navigate`

The plugin also transparently wraps the built-in browser tools (all `override=True`):

- `browser_navigate` (decides the route)
- `browser_snapshot`, `browser_click`, `browser_type`, `browser_scroll`,
  `browser_back`, `browser_press`, `browser_console`, `browser_vision`,
  `browser_get_images` (reuse the session's last route under `ROUTER_LOCK`)

### Model-facing examples

Use local Chrome for the current agent session until changed:

```text
browser_policy_set({"engine": "profile:main"})
browser_navigate({"url": "https://x.com/home"})
browser_snapshot({})
```

Return the current session to URL-driven policy:

```text
browser_policy_set({"engine": "auto"})
browser_navigate({"url": "https://news.ycombinator.com"})
```

Force one URL through Browserbase/cloud as a one-shot, then navigate:

```text
browser_policy_route({"url": "https://www.lemonde.fr", "hint": "cloud"})
browser_navigate({"url": "https://www.lemonde.fr"})
```

Read a public page through the fast-read chain without opening a browser:

```text
browser_policy_route({"url": "https://en.wikipedia.org/wiki/Mac_Mini", "hint": "fast_read"})
browser_navigate({"url": "https://en.wikipedia.org/wiki/Mac_Mini"})
```

Fast-read results are not interactive. If the result says
`requires_browser_for_interaction: true`, pin `cloud` or `profile:main` and
navigate again before calling `browser_click`, `browser_type`, or
`browser_snapshot`.

## Slash Commands

| Command | Without argument | With `<session-name>` |
|---|---|---|
| `/browser-status [<session-name>]` | Show global state + active pins | Also show that session's per-session state |
| `/browser-auto [<session-name>]` | Reset the global default to auto | Mark that session as **genuinely** URL-driven (`ignore_global=True`) — overrides any global pin |
| `/browser-local [<session-name>]` | Set the global default to local Chrome; mutates `BROWSER_CDP_URL` eagerly | Pin that session to local Chrome; lazy env apply on the session's next browser call |
| `/browser-cloud [<session-name>]` | Set the global default to cloud; clears `BROWSER_CDP_URL` eagerly | Pin that session to cloud; lazy env apply |
| `/browser-recover` | Probe local CDP, `launchctl kickstart -k` if down | — |
| `/browser-route [<session-name>] <url>` | Show the route a fresh session would pick | Show the route the named session would pick (informational, no mutation) |
| `/browser-sessions` | List recent sessions with their browser policy | — |

### Telegram usage

The Telegram bot displays plugin slash commands with `_` substituted for `-`,
so `/browser-status` appears as `/browser_status`. Common flows:

```text
/browser_status                       # check current routing
/browser_local                        # all sessions use local Chrome
/browser_cloud my-research            # only "my-research" uses Browserbase
/browser_auto my-research             # "my-research" is URL-driven (ignore global)
/browser_route x.com                  # what would happen on a fresh session
/browser_route my-research x.com      # what would happen in "my-research"
/browser_sessions                     # list recent sessions + policy
```

Session names with spaces are supported by commands that take a session name:

```text
/browser_local my long research thread
/browser_route my long research thread https://x.com/home
/browser_auto my long research thread
```

Typical workflows:

```text
# Debug a local app from Telegram. All sessions use local Chrome until changed.
/browser_local
open http://localhost:5173 and inspect the console

# Only pin one named conversation to local Chrome.
/browser_local x-debug
in x-debug, open https://x.com/home and check notifications

# Keep a research conversation URL-driven even if the global default is local.
/browser_auto research
in research, summarize https://news.ycombinator.com

# Recover Chrome after it was closed.
/browser_recover
/browser_status
```

### Session resolution

When a slash command is given a `<session-name>`, the plugin resolves it via
`hermes_state.SessionDB.resolve_session_by_title`. Numbered lineage variants
(`my session #2`, `#3`, ...) resolve to the most recent. Raw session-id
prefixes are also accepted via `resolve_session_id`. The gateway invokes
browser tools with `task_id=session_id`, so the wrapper reads
`SESSION_STATE[session_id]` and the per-session pin takes effect on that
session's next browser call.

The plugin does not (and cannot) detect "the current session" from a slash
command — Hermes does not pass the gateway session id to plugin handlers.
Use the explicit `<session-name>` form to target a specific session.

### Auto semantics

A session that has never been touched follows the global default. Once you
run `/browser-auto <session-name>` (or call `browser_policy_set(engine="auto")`
from the model side), that session is flagged `ignore_global=True` and is
*genuinely* URL-driven — it does **not** inherit the global pin. Pinning the
session with `/browser-local` / `/browser-cloud` (or `browser_policy_set`)
clears the flag.

This was a deliberate fix in v1.0.0-beta.1: previously, a session reset to
auto would still inherit a global pin even when the user explicitly asked for
URL-driven routing.

## Configuration

Copy `config.yaml.example` to `config.yaml` and adjust the local CDP endpoint,
LaunchAgent label, domain classes, and fast-read chain.

`config.yaml` is reread on every routing call via mtime-based caching, so
domain edits do not require a gateway restart.

**`config.yaml` contains machine-local policy and is gitignored — never
commit it.** Personal domain lists and `gui/<uid>/...` LaunchAgent labels
belong in the local copy only.

Minimal example:

```yaml
default_interactive_engine: cloud
cloud_provider_expected: browserbase

local_profile:
  name: main
  cdp_url: http://127.0.0.1:9222
  launchctl_label: gui/501/com.hermes.chrome-profile-main
  recovery_enabled: true
  recovery_timeout_s: 8

classes:
  internal_debug:
    domains:
      - localhost
      - "127.0.0.1"
      - "*.local"
  hard_login:
    domains:
      - x.com
      - "*.x.com"
      - linkedin.com
      - "*.linkedin.com"
  subscription_login:
    domains:
      - lemonde.fr
      - "*.lemonde.fr"
  public_read:
    domains:
      - wikipedia.org
      - "*.wikipedia.org"
      - news.ycombinator.com

fast_read_chain:
  - web_extract
  - web_search
  - cloud_browser
```

## Persistence

`SESSION_STATE` and `GLOBAL_DEFAULT` are persisted to `.state.json` next
to the plugin on every mutation (atomic tmp-then-rename) and reloaded at
plugin register-time. Per-session pins and the global default survive a
gateway restart. The file is gitignored.

## Engine model

| Engine | Effect |
|---|---|
| `profile:main` | Sets `BROWSER_CDP_URL=http://127.0.0.1:9222`; Hermes' browser_tool connects to the local Chrome over CDP |
| `cloud` | Unsets `BROWSER_CDP_URL`; Hermes uses its configured cloud provider (expected Browserbase) |
| `fast_read` | Skips a real browser; tries each tool in `fast_read_chain` (default: `web_extract`, `web_search`) until one returns usable content. Responses are tagged `requires_browser_for_interaction: true` so the model knows it cannot click/type on the result. If the chain exhausts without success, falls back to `cloud` |

`BROWSER_CDP_URL` is process-global. The plugin serializes every mutation
and every wrapped browser call through a single `ROUTER_LOCK`. This is
acceptable for a personal gateway where browser usage is mostly serial; it
is not a robust multi-tenant browser isolation model.

## Local Chrome prerequisites

The `profile:main` engine assumes a Chrome process is already available through
CDP, normally from a macOS LaunchAgent created during setup:

```bash
launchctl print gui/501/com.hermes.chrome-profile-main
nc -z 127.0.0.1 9222 && echo "CDP OK"
curl -s http://127.0.0.1:9222/json/version
```

The Chrome profile directory contains cookies and login state. Keep it outside
Git and treat it as sensitive:

```text
~/.hermes/chrome-profiles/main
```

## Security

The local-profile engine connects to Chrome's DevTools Protocol on
`127.0.0.1:9222`. **CDP gives full control of the browser to anyone who
can reach the port**: read cookies and localStorage for every site you're
logged into, execute arbitrary JavaScript in any tab, take screenshots,
download files. Treat the port like a root shell on your browsing
identity.

Recommendations:

- Keep CDP bound to `127.0.0.1` (the example LaunchAgent does). Never
  expose it on a non-loopback interface.
- Run the gateway under your own user account (not root, not shared).
- Do not enable this plugin on a multi-tenant machine where another
  user has shell access — they can curl `http://127.0.0.1:9222/json` and
  drive your browser session.
- The fast-read chain hits third-party APIs (e.g. Firecrawl). If you
  configure a provider with an API key, that key sees the URLs you
  query. Use providers you trust for the workload.
- `.state.json` may contain session ids and last-visited URLs. It is
  gitignored, but treat the plugin directory like the rest of
  `~/.hermes/` (it holds tokens, .env, session transcripts).

## Troubleshooting

Plugin does not appear:

```bash
hermes plugins list
grep -A12 '^plugins:' ~/.hermes/config.yaml
tail -100 ~/.hermes/logs/agent.log | grep browser_policy_router
sudo launchctl kickstart -k system/com.hermes.gateway-richard
```

Local Chrome route falls back to cloud:

```bash
launchctl print gui/501/com.hermes.chrome-profile-main | grep -E 'state|last exit'
nc -z 127.0.0.1 9222 && echo "CDP OK" || echo "CDP DOWN"
curl -s http://127.0.0.1:9222/json/version
```

Fast-read never extracts full pages:

```bash
hermes plugins list | grep -E 'web/firecrawl|web/brave_free'
grep -A5 '^web:' ~/.hermes/config.yaml
```

`browser_snapshot` returns `requires_browser_for_interaction`:

```text
browser_policy_set({"engine": "cloud"})
browser_navigate({"url": "https://the-page-you-want.example"})
browser_snapshot({})
```

## Development

```bash
python -m pip install -e ".[dev]"
python -m ruff check .
python -m ruff format --check .
python -m pyright .
python -m pytest tests/
```

CI runs the same checks on every PR via `.github/workflows/ci.yml`.
