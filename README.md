# hermes-plugin-browser-policy-router

Hermes plugin that routes browser work between:

- a local logged-in Chrome profile exposed through CDP (`profile:main`);
- a local Camofox/Camoufox browser running in Docker with noVNC, durable
  across reboots and GUI logout (`camofox:main`);
- the configured Hermes cloud browser provider (`cloud`, expected: Browserbase);
- fast read tools for public pages (`fast_read`: `web_extract`, `web_search`).

The plugin does not patch Hermes upstream code. It overrides the built-in
`browser_*` tools with `override=True`, sets or unsets `BROWSER_CDP_URL` and
`CAMOFOX_URL` before delegating to the built-in browser implementation, and
exposes slash commands for manual browser policy changes.

## What problem this solves

Hermes normally has one active browser backend per process. That is awkward when
different jobs need different browser identities:

- public pages should be read quickly with search or extract tools;
- paid news sites may work best through the configured cloud browser;
- X, LinkedIn, and other hard-login targets benefit from a durable local
  Firefox profile (Camofox) that survives reboots and GUI logout;
- Google/Gmail accounts often want the real Chrome + 1Password profile;
- local dev apps (`localhost`, `*.local`) need a browser that can reach the
  Mac's host loopback — Camofox via `host.docker.internal`, or Chrome directly;
- while debugging, a user may want to pin one conversation to one engine until
  they explicitly switch it back.

This plugin gives Hermes a policy layer. `browser_navigate` chooses an engine
from the URL and current session policy, then follow-up tools such as
`browser_click` and `browser_snapshot` reuse that route under a single lock so
no two sessions race on the process-global `BROWSER_CDP_URL` / `CAMOFOX_URL`.
No upstream Hermes code changes are required.

## Engines

| Engine | Effect | Where it runs |
|---|---|---|
| `profile:main` | Sets `BROWSER_CDP_URL=http://127.0.0.1:9222` and clears Camofox env. Hermes' browser tools connect to the local Chrome over CDP. | macOS GUI session (Chrome LaunchAgent, GUI-bound) |
| `camofox:main` | Sets `CAMOFOX_URL=http://127.0.0.1:9377` plus `CAMOFOX_USER_ID`/`CAMOFOX_SESSION_KEY`/`CAMOFOX_ADOPT_EXISTING_TAB` and clears `BROWSER_CDP_URL`. Hermes routes browser tools through `tools/browser_camofox.py`. | Docker container under Colima, durable across reboots and GUI logout. Manual login via noVNC on `127.0.0.1:6080`. |
| `cloud` | Clears `BROWSER_CDP_URL` and `CAMOFOX_URL`. Hermes uses its configured cloud provider (expected Browserbase). | Cloud (no local resources used) |
| `fast_read` | Skips a real browser; tries each tool in `fast_read_chain` (default: `web_extract`, `web_search`) until one returns usable content. Responses are tagged `requires_browser_for_interaction: true` so the model knows it cannot click/type on the result. If the chain exhausts without success, falls back to `cloud`. | In-process (calls other Hermes tools) |

`BROWSER_CDP_URL` and `CAMOFOX_URL` are process-global, and `BROWSER_CDP_URL`
takes priority over Camofox in Hermes' built-in resolution
(`tools/browser_camofox.py: is_camofox_mode`). The plugin therefore strictly
clears the env vars of every other engine before setting the chosen one — a
Chrome→Camofox switch never leaves Chrome's CDP URL behind, and vice-versa.

All mutations and all wrapped browser calls serialize through a single
`ROUTER_LOCK`. This is acceptable for a personal gateway where browser usage
is mostly serial; it is not a robust multi-tenant browser isolation model.

## Routing model

For each `browser_navigate(url)`, the first matching rule wins:

1. A one-shot hint from `browser_policy_route(url, hint=...)`.
2. A per-session pin set by `browser_policy_set(...)` or a targeted slash
   command such as `/browser-camofox my-session`.
3. A global slash-command pin set by `/browser-chrome`, `/browser-camofox`,
   `/browser-cloud`, or the legacy alias `/browser-local`.
4. URL classes from `config.yaml`, iterated in priority order:
   - `internal_debug` (localhost / *.local) → engine from class config
   - `durable_local_login` (x.com, linkedin, ...) → engine from class config
   - `hard_login` (legacy Phase 1 class) → engine from class config
   - `human_profile_login` (accounts.google, mail.google, ...) → engine from class config
   - `subscription_login` (lemonde.fr, ft.com, ...) → engine from class config
   - `public_read` (wikipedia, news.ycombinator, ...) → `fast_read`
5. `default_interactive_engine`, usually `cloud`.

Each class's `engine:` field is read directly from `config.yaml`, so moving
a domain from Chrome to Camofox is a config edit only — no code change.

`/browser-auto <session-name>` is a real per-session reset: that session becomes
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

### Camofox prerequisites (`camofox:main` only)

Routing to `camofox:main` requires a local Camofox container reachable on
`127.0.0.1:9377`. See `phase-3-colima-camofox-local-browser.md` for the
canonical setup; the short version is:

```bash
brew install colima docker
sudo launchctl bootstrap system /Library/LaunchDaemons/com.hermes.colima-richard.plist
docker run -d --name camofox-browser --restart unless-stopped \
  -p 127.0.0.1:9377:9377 -p 127.0.0.1:6080:6080 -p 127.0.0.1:5901:5900 \
  -e CAMOFOX_PORT=9377 -e ENABLE_VNC=1 -e VNC_RESOLUTION=1920x1080 \
  -e BROWSER_IDLE_TIMEOUT_MS=0 -e CAMOFOX_CRASH_REPORT_ENABLED=false \
  -v /Users/richard/.hermes/camofox:/root/.camofox \
  camofox-browser:135.0.1-aarch64
curl -s http://127.0.0.1:9377/health
```

Logins to `camofox:main` are not shared with Chrome. Open
`http://127.0.0.1:6080` (or tunnel via SSH) and log in once per site.

## Tools (model-facing)

- `browser_policy_status` — show current routing state for this session
- `browser_policy_set` — pin the session's engine (`profile:main` /
  `camofox:main` / `cloud` / `auto`)
- `browser_policy_route` — decide and apply a route for one URL, with an
  optional `hint` that latches as a one-shot for the next `browser_navigate`

Available hints: `auto`, `chrome`, `camofox`, `cloud`, `fast_read`.

The plugin also transparently wraps the built-in browser tools (all `override=True`):

- `browser_navigate` (decides the route, rewrites `localhost` for Camofox if
  configured)
- `browser_snapshot`, `browser_click`, `browser_type`, `browser_scroll`,
  `browser_back`, `browser_press`, `browser_console`, `browser_vision`,
  `browser_get_images` (reuse the session's last route under `ROUTER_LOCK`)

### Model-facing examples

Pin one session to durable local Camofox:

```text
browser_policy_set({"engine": "camofox:main"})
browser_navigate({"url": "https://x.com/home"})
browser_snapshot({})
```

Pin one session to GUI-bound Chrome (Google / 1Password):

```text
browser_policy_set({"engine": "profile:main"})
browser_navigate({"url": "https://mail.google.com"})
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
`requires_browser_for_interaction: true`, pin `cloud`, `profile:main`, or
`camofox:main` and navigate again before calling `browser_click`,
`browser_type`, or `browser_snapshot`.

## Slash Commands

| Command | Without argument | With `<session-name>` |
|---|---|---|
| `/browser-status [<session-name>]` | Show global state + Chrome and Camofox health | Also show that session's per-session state |
| `/browser-auto [<session-name>]` | Reset the global default to auto | Mark that session as **genuinely** URL-driven (`ignore_global=True`) — overrides any global pin |
| `/browser-chrome [<session-name>]` | Pin global default to local Chrome (`profile:main`); eager env mutation | Pin that session to local Chrome; lazy env apply |
| `/browser-local [<session-name>]` | **Alias of `/browser-chrome`**, kept for Phase 1 muscle memory | Alias of `/browser-chrome <session>` |
| `/browser-camofox [<session-name>]` | Pin global default to local Camofox (`camofox:main`); eager env mutation | Pin that session to Camofox; lazy env apply |
| `/browser-cloud [<session-name>]` | Pin global default to cloud; clears `BROWSER_CDP_URL` + `CAMOFOX_URL` | Pin that session to cloud; lazy env apply |
| `/browser-recover` | Probe Chrome CDP and Camofox `/health`, kickstart the LaunchAgent / LaunchDaemon and `docker start` as needed | — |
| `/browser-route [<session-name>] <url>` | Show the route a fresh session would pick | Show the route the named session would pick (informational, no mutation) |
| `/browser-sessions` | List recent sessions with their browser policy | — |

`/browser-local` is **intentionally not repointed** at Camofox — it remains
an alias for `/browser-chrome` so anything scripted against the Phase 1 plugin
keeps working. New code should prefer `/browser-chrome` and `/browser-camofox`.

### Telegram usage

The Telegram bot displays plugin slash commands with `_` substituted for `-`,
so `/browser-status` appears as `/browser_status`. Common flows:

```text
/browser_status                       # check Chrome + Camofox health
/browser_camofox                      # all sessions use Camofox (Docker/noVNC)
/browser_chrome my-research           # only "my-research" uses local Chrome
/browser_cloud my-research            # only "my-research" uses Browserbase
/browser_auto my-research             # "my-research" is URL-driven (ignore global)
/browser_route x.com                  # what would happen on a fresh session
/browser_route my-research x.com      # what would happen in "my-research"
/browser_sessions                     # list recent sessions + policy
```

Session names with spaces are supported by commands that take a session name:

```text
/browser_chrome my long research thread
/browser_route my long research thread https://x.com/home
/browser_auto my long research thread
```

Typical workflows:

```text
# Long-running X.com session that survives a reboot.
/browser_camofox
in x-research, go to https://x.com/home and scan the timeline

# Debug a local app from Telegram via Camofox; localhost rewritten to host.docker.internal.
/browser_camofox debug
in debug, open http://localhost:5173 and inspect the console

# Pin one named conversation to Chrome for Gmail.
/browser_chrome gmail
in gmail, open https://mail.google.com and read the latest threads

# Keep a research conversation URL-driven even if the global default is camofox.
/browser_auto research
in research, summarize https://news.ycombinator.com

# Recover both browsers after they were closed / the daemon was restarted.
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
session with `/browser-chrome` / `/browser-camofox` / `/browser-cloud` (or
`browser_policy_set`) clears the flag.

## Configuration

Copy `config.yaml.example` to `config.yaml` and adjust the local CDP endpoint,
LaunchAgent label, Camofox URL, Colima LaunchDaemon label, domain classes,
and fast-read chain.

`config.yaml` is reread on every routing call via mtime-based caching, so
domain edits do not require a gateway restart.

**`config.yaml` contains machine-local policy and is gitignored — never
commit it.** Personal domain lists, `gui/<uid>/...` LaunchAgent labels, and
`system/com.hermes.colima-...` LaunchDaemon labels belong in the local copy
only.

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

camofox:
  name: main
  url: http://127.0.0.1:9377
  health_url: http://127.0.0.1:9377/health
  no_vnc_url: http://127.0.0.1:6080
  user_id: hermes-main
  session_key: main
  adopt_existing_tab: true
  recovery_enabled: true
  colima_launchctl_label: system/com.hermes.colima-richard
  docker_container: camofox-browser
  docker_binary: /opt/homebrew/bin/docker
  localhost_rewrite:
    enabled: true
    target_host: host.docker.internal

classes:
  internal_debug:
    engine: camofox:main
    no_cloud_fallback: true
    domains:
      - localhost
      - "127.0.0.1"
      - "*.local"
  durable_local_login:
    engine: camofox:main
    domains:
      - x.com
      - "*.x.com"
      - linkedin.com
      - "*.linkedin.com"
  human_profile_login:
    engine: profile:main
    domains:
      - accounts.google.com
      - mail.google.com
  subscription_login:
    engine: cloud
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

### localhost rewrite

Camofox runs in a Linux VM (Colima), so `localhost` inside the container
points at the container itself, not the Mac host. When
`camofox.localhost_rewrite.enabled: true`, `browser_navigate` rewrites
`http://localhost:PORT/...` → `http://host.docker.internal:PORT/...` (or
whatever `target_host` is set to) for routes that land on `camofox:main`.

The annotation in the returned payload preserves both URLs:

```json
{
  "_router": {
    "engine": "camofox:main",
    "url": "http://localhost:8765/x",
    "browser_url": "http://host.docker.internal:8765/x",
    "reason": "internal/debug route; rewrote localhost to host.docker.internal"
  }
}
```

Verify the alias works under your Colima setup before relying on it:

```bash
python3 -m http.server 8765 --bind 0.0.0.0 &
docker run --rm curlimages/curl http://host.docker.internal:8765
```

If `host.docker.internal` does not resolve under your Colima version, try
`host.lima.internal` or the VM gateway IP.

## Persistence

`SESSION_STATE` and `GLOBAL_DEFAULT` are persisted to `.state.json` next
to the plugin on every mutation (atomic tmp-then-rename) and reloaded at
plugin register-time. Per-session pins and the global default survive a
gateway restart. The file is gitignored.

Camofox's own browser profile lives in `~/.hermes/camofox` (host) /
`/root/.camofox` (container) and is persisted by the Camofox server itself;
the plugin doesn't manage it.

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

`profile:main` is **GUI-bound**: the LaunchAgent runs in `gui/501`, which only
exists while the macOS user is logged in. After a reboot without GUI login,
`profile:main` will be unavailable but `camofox:main` continues to work.

## Security

Both local engines bind to `127.0.0.1` only. **Anyone who can reach those
ports has full control of the corresponding browser**: cookies, localStorage,
JavaScript execution, screenshots, downloads. Treat them like a root shell on
your browsing identity.

Recommendations:

- Keep CDP and Camofox bound to `127.0.0.1`. Never expose either on a
  non-loopback interface.
- For remote access, tunnel over SSH: `ssh -L 6080:127.0.0.1:6080 -L
  9377:127.0.0.1:9377 user@host`.
- Run the gateway under your own user account (not root, not shared).
- Do not enable this plugin on a multi-tenant machine where another user has
  shell access — they can curl `http://127.0.0.1:9222/json` or
  `http://127.0.0.1:9377/tabs` and drive your browser session.
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

Camofox route falls back / errors out:

```bash
sudo launchctl print system/com.hermes.colima-richard | grep state
colima status
docker ps --filter name=camofox-browser
curl -s http://127.0.0.1:9377/health
```

Fast-read never extracts full pages:

```bash
hermes plugins list | grep -E 'web/firecrawl|web/brave_free'
grep -A5 '^web:' ~/.hermes/config.yaml
```

`browser_snapshot` returns `requires_browser_for_interaction`:

```text
browser_policy_set({"engine": "camofox:main"})  # or "profile:main" or "cloud"
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
