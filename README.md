# hermes-plugin-browser-policy-router

Hermes plugin that routes browser work between:

- a local logged-in Chrome profile exposed through CDP;
- the configured Hermes cloud browser provider (expected: Browserbase);
- fast read tools for public pages (`web_extract`, `web_search`).

The plugin does not patch Hermes upstream code. It overrides the built-in
`browser_*` tools with `override=True`, sets or unsets `BROWSER_CDP_URL` before
delegating to the built-in browser implementation, and exposes slash commands
for manual browser policy changes.

## Install

From a Hermes host with `hermes_cli` available:

```bash
hermes plugins install anpicasso/hermes-plugin-browser-policy-router --enable
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

## Development

```bash
python -m pip install -e ".[dev]"
python -m ruff check .
python -m ruff format --check .
python -m pyright .
python -m pytest tests/
```

CI runs the same checks on every PR via `.github/workflows/ci.yml`.
