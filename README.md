# jtr

A local CLI for daily work with Jira / Track & Release tickets.

```
jtr list mine
jtr search "summary ~ 'release'"
jtr view PROJ-123
jtr comment PROJ-123 "Looked into this."
jtr transition PROJ-123 "In Progress"
```

## Supported Jira deployments

| Deployment | Status |
|---|---|
| **Jira Server / Data Center** (self-hosted, incl. behind an SSO gateway) | ✅ Supported |
| **Jira Cloud** (`*.atlassian.net`) | ✅ Supported |

The deployment is detected from the base URL, and everything that differs
between the two follows from it:

| | Server / Data Center | Cloud |
|---|---|---|
| Auth | PAT (Bearer), or browser SSO | Account email + API token (Basic) |
| Setup | `jtr auth pat` / `jtr auth sso` | `jtr auth token` |
| Search paging | `--start-at` (offsets, with a total) | `--cursor` (tokens, no total) |
| `jtr assign` takes | a username | an email, display name, or accountId |

Detection is a hostname guess. If yours misleads — a Cloud tenant on a
vanity domain, or a self-hosted instance on something Cloud-shaped — pin it:

```bash
jtr config deployment cloud     # or: server, or auto to go back to detecting
jtr init --base-url <url> --deployment cloud
```

Jira Server/DC 8.14+ is needed for PAT support; `jtr auth sso` works on any
version. `jtr auth sso` is Server/DC only: Atlassian doesn't accept browser
session cookies as REST credentials, so it refuses on Cloud rather than
opening a browser that can't help.

### Jira Cloud setup

```bash
jtr init --base-url https://your-tenant.atlassian.net
jtr auth token          # prompts for email + API token
```

Create the API token at
<https://id.atlassian.com/manage-profile/security/api-tokens>.

Two Cloud differences are worth knowing about, because they're Atlassian's
and not something `jtr` can paper over:

- **Search results have no total.** Cloud's `/search/jql` endpoint replaced
  the removed `/search`, and it pages by opaque cursor. `jtr` prints
  `N ticket(s)` instead of `N of M`, and `--json` reports `"total": null`
  with a `next_page_token` to feed back via `--cursor`.
- **People are identified by `accountId`.** `jtr assign PROJ-1 j@acme.com`
  looks the account up for you; pass `--account-id` when you already hold
  the id. An ambiguous name is reported rather than guessed at.

`JTR_API_VERSION` exists to force REST v2 or v3 and normally stays unset —
v3's only real difference is ADF-formatted comment bodies, which this CLI
has no use for. It's an escape hatch, not a setting to tune.

## SSO gateways (Server/DC)

Where the tracker sits behind a WebSEAL-style gateway, that gateway requires
browser SSO — `jtr auth sso` captures the session cookies in a one-time
login. A Personal Access Token (PAT) is supported and sent on top of
the cookies if you set one, but a PAT alone doesn't pass the gateway.

See [EXAMPLES.md](EXAMPLES.md) for copy-paste recipes (JQL by label,
status, release; transitions; audit-log queries; etc.).

## Requirements

- Python 3.11+ (the installer takes care of `uv`)
- Microsoft Edge, Google Chrome or Chromium — for `jtr auth sso` only

`jtr auth sso` drives the browser you already have. Nothing is
downloaded, so there is nothing for a corporate proxy to block. Edge is
preferred, then Chrome, then Chromium; set `JTR_BROWSER_CHANNEL=chrome`
to pick one explicitly, or `JTR_BROWSER=<path-to-executable>` if yours
is installed somewhere unusual.

The browser opens with jtr's own profile at `~/.jtr/browser-profile`, so
it never touches your everyday browser profile. That profile is kept
between runs, letting the identity provider skip the login form; `jtr
auth logout` deletes it.

## Install

No GitHub CLI, no tokens, no SSH keys. Download the release zip in your
browser, extract it, and run the bundled installer — same flow on macOS,
Linux, and Windows.

1. Download the **Source code (zip)** from the jtr **Releases** page.
2. Extract it and run the bundled installer:

```bash
# macOS / Linux
cd jtr-1.0.0        # adjust to the version you downloaded
./install.sh
```

```powershell
# Windows
cd .\jtr-1.0.0\
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The installer installs `uv` if you don't already have it, then runs
`uv tool install` on the extracted source.

To upgrade later: download the newer zip and re-run the installer.
To remove: `uv tool uninstall jtr`.

See [INSTALL.md](INSTALL.md) for the manual (no-installer) steps and
troubleshooting.

## First-run setup

One command does the whole thing — config, skill, and login:

```bash
jtr init --ticket https://tracker.example.com/jira/browse/PROJ-123 --auth sso
```

`jtr init` parses the base URL and project key out of the ticket URL
and writes them into `./.jtr/.env` in the cwd (this is what makes the
directory project-local — see [Storage](#storage) for the global
alternative). It also copies the bundled `/jtr` Claude Code skill into
`./.claude/skills/` (`--no-skills` to skip), and `--auth` then runs that
login immediately and remembers the choice.

Run it with no arguments and it prompts for whatever it needs. Or pass
the parts explicitly, in any combination:

```bash
jtr init --base-url https://tracker.example.com/jira \
         --project PROJ \
         --auth sso \
         --browser msedge          # optional; which browser SSO drives
```

| Flag | Meaning |
| --- | --- |
| `--ticket <url>` | Ticket URL to derive base URL + project from (same as the positional argument). A bare base URL works too. |
| `--base-url <url>` | Base URL, including the context path. Wins over `--ticket`. |
| `--project <KEY>` | Default project scope. Wins over `--ticket`. |
| `--auth sso\|pat` | Save the auth method and run it now. |
| `--pat <value>` | Token value; implies `--auth pat`. Omit it to be prompted without echo. |
| `--browser <channel>` | `msedge`, `chrome` or `chromium` for SSO. Saved in the *global* config — it describes the machine, not the project. |
| `--no-auth` | Scaffold only; authenticate later. |
| `--force` | Update an existing `./.jtr/` in place instead of failing. |
| `--timeout <s>` | Seconds to wait for SSO (default 300). |
| `--no-gitignore` | Don't append `.jtr/` to `./.gitignore`. |
| `--no-skills` | Don't install the bundled `/jtr` Claude Code skill. |
| `--bare` | Config only — implies `--no-gitignore --no-skills`. |
| `--dir <path>` | Initialize `<path>/.jtr/` instead of the cwd's. |
| `--json` | Emit the resulting config as JSON, and never prompt. See [Driving jtr from another tool](#driving-jtr-from-another-tool). |

Because the method is saved, later logins need no argument:

```bash
jtr auth                    # re-runs whichever method this config uses
jtr auth --method pat       # switch the saved default, and use it now
```

Setting fields individually instead:

```bash
jtr config base-url https://tracker.example.com/jira
jtr config project PROJ     # default project scope (optional)
jtr auth sso                # browser SSO login — required for SSO-gated trackers
```

## Claude Code integration

`jtr init` drops the `/jtr` skill into `./.claude/skills/` so Claude
Code can drive jtr on your behalf — no extra install step.

- **`/jtr`** — auto-activates when you mention Jira, a ticket key
  (`PROJ-123`), "my tickets", workflow language ("transition", "in
  progress"), or type `/jtr` directly. Claude reads tickets freely
  (list/search/view) and writes (comment/edit/label/assign/transition)
  through the same preview → confirm → audit pattern as the CLI.
  Writes require your explicit per-turn approval; Claude is
  instructed never to pass `--yes` unsolicited.

Re-running `jtr init` in another project folder installs the same
skill there. It lives alongside jtr in the package, so
`uv tool upgrade jtr` keeps it in sync.

## worca-cc integration

[`worca-jira-source/`](worca-jira-source/) is a **worca-cc plugin** that
pulls Jira tickets into worca's New Pipeline through this CLI. jtr owns
everything Jira-specific — auth, instance config, the REST dialect — and
the connector only shells out to `jtr … --json`, so both deployments
work, including Server/DC behind an SSO gateway.

Setup runs entirely from the plugin's settings pane: paste any ticket
URL, press **Connect**. Highlights:

- **Multiple profiles** — one install can hold several Jira instances,
  each with its own token and its own jtr config dir, so sessions are
  never shared.
- **Per-run write-back** — a finished pipeline can be posted back onto
  its ticket as a comment (summary, branch, PR link), with an optional
  workflow transition (e.g. `Done`) on success.
- **Ticket → pipeline fast path** — paste a ticket key or browse URL
  into the task browser and that exact ticket comes back, whatever the
  filter says.

It's a plugin to link into worca, not part of the installed CLI, and it
requires **jtr ≥ 0.10.0**. See
[worca-jira-source/README.md](worca-jira-source/README.md) for setup,
config, and troubleshooting.

## Develop locally

If you're hacking on jtr itself rather than just using it:

```bash
git clone git@github.com:BD-AI-SDLC/jtr.git
cd jtr
uv sync
uv run jtr whoami
```

`uv run jtr …` executes the working tree, so edits take effect with no
reinstall step. The repo has its own `./.jtr/`, so commands run from the
repo root use *that* config rather than your global one — which is what
makes it safe to experiment here.

### Testing a change

`whoami` is the smoke test: it exercises config loading, credentials and
a real Jira round trip in one call.

```bash
uv run jtr whoami          # identity — proves the session works
uv run jtr list mine       # a real read
uv run jtr view <KEY>      # detail rendering
```

Writes are preview → confirm, so you can start one and abort at the
prompt without touching a real ticket. Only pass `--yes` when you mean
it — that skips the prompt and posts.

To re-test the SSO flow without waiting for cookies to expire:

```bash
uv run jtr auth sso --force      # always opens a browser
uv run jtr auth status           # cookie count + capture time
```

`--force` skips the reuse shortcut. On success the login prints which
browser it picked (`Opened chrome for login.`) — useful for checking the
Edge/Chrome selection, which you can steer:

```bash
JTR_BROWSER_CHANNEL=msedge uv run jtr auth sso --force
JTR_BROWSER=/path/to/browser uv run jtr auth sso --force
```

### Testing what clients actually get

The above runs from source. To rehearse a real install:

```bash
uv tool install --force .        # install from the working tree
cd ~ && jtr whoami               # runs the installed copy
```

Outside the repo there's no `./.jtr/`, so the installed copy falls back
to the global `~/.jtr/` — a separate config with its own credentials. A
fresh directory is the cleanest way to rehearse a new user's first run:

```bash
mkdir /tmp/jtr-trial && cd /tmp/jtr-trial
jtr init --ticket <ticket-url> --auth sso
ls .claude/skills/               # confirms the bundled skill landed
```

### Lint

```bash
uv run ruff check src/
```

## Commands

```
jtr init [<ticket-url>] [--ticket <url>] [--base-url <url>] [--project KEY]
         [--auth sso|pat] [--pat <v>] [--browser CHANNEL] [--timeout N]
         [--no-auth] [--force] [--no-gitignore] [--no-skills] [--bare]
         [--dir <path>] [--json]           set up project-local config + auth
jtr reset [--yes] [--json]                 delete all jtr-managed data (active dir)
jtr whoami [--json]                        print authenticated user
jtr projects [--json]                      projects you can see (for a picker)
jtr list mine [--all] [--all-statuses]     tickets assigned to me
                  [--project KEY] [--limit N] [--start-at N] [--json]
jtr search "<JQL>" [--all] [--project KEY] [--limit N] [--start-at N] [--json]
jtr view <KEY> [--json]                    header + fields + comments
jtr comment <KEY> "<text>"                 add a comment
jtr edit <KEY> <field> <value>             edit one field (full replace)
jtr label add | remove <KEY> <name>        single-label add/remove (idempotent)
jtr assign <KEY> <user> | --unassign       set/clear the assignee
jtr transition <KEY> [<status>] [-m "..."] move through workflow
jtr auth [--method sso|pat] [--json]       authenticate with the saved method
jtr auth pat | sso [--json]                set / refresh credentials
jtr auth logout [--cookies | --pat] [--json]  clear both (default), or just one
jtr auth status [--json]                   what's configured
jtr config base-url <url> | project <key> | show       all accept [--json]
```

Every command accepts `--json`; every write also accepts `--yes`.

Every write is preview → confirm → POST/PUT. Pass `--yes` / `-y` to
skip the prompt (for scripting). Both successful and failed writes
append a JSONL line to `.jtr_audit.jsonl`; cancellations don't.

Audit row shape — also what a write returns under `--json`, plus a
`changed` boolean (`false` when the ticket was already in the requested
state and no API call was made):

```json
{"ts":"2026-06-14T09:12:33+00:00","action":"transition","key":"PROJ-123",
 "ok":true,"before":{"status":"Open"},
 "after":{"status":"In Progress","transition_id":"21","comment":null},
 "result":null}
```

### Editable fields

| Field         | Value syntax                                  |
|---------------|-----------------------------------------------|
| `summary`     | string                                        |
| `description` | string                                        |
| `priority`    | priority name, e.g. `High`                    |
| `labels`      | comma-separated list, e.g. `release,blocker`  |
| `fixVersions` | comma-separated list of version names         |

For adding or removing a single label without overwriting the rest of
the list, use `jtr label add/remove` — both are idempotent (no API
call if the label is already in the desired state).

For transitions, `jtr transition <KEY>` (no status arg) lists the
available transitions on that ticket. Match is on the transition name
or its target status, case-insensitive, partial OK if unambiguous.

### Project scoping

If `JTR_PROJECT` is set in `.env`, every list/search query is
auto-AND'd with `project = <KEY>` unless:

- the JQL already mentions `project`, or
- `--all` is passed, or
- `--project OTHER` overrides.

The actual JQL sent is printed above each result table so you always
see what ran.

### Paging

`list mine` and `search` return one page at a time. `--limit` sizes the
page, `--start-at` picks the offset, and the JSON payload carries the
counters you need to walk the rest:

```bash
jtr search "project = PROJ" --limit 50 --json | jq '{total, next_start_at}'
jtr search "project = PROJ" --limit 50 --start-at 50 --json
```

`next_start_at` is `null` on the last page, so a caller can loop until
it stops being a number. The human table prints the same hint.

### JSON output

Every command accepts `--json` and emits a machine-readable payload to
stdout — no Rich formatting, safe to pipe to `jq`.

```bash
jtr list mine --json | jq '.tickets[].key'
jtr view PROJ-123 --json | jq '.ticket.status'
jtr whoami --json | jq -r '.key'
jtr comment PROJ-123 "done" --yes --json | jq '.ok'
```

Shapes:

- list / search → `{"jql", "count", "start_at", "max_results", "total", "next_start_at", "tickets": [...]}`
- view          → `{"ticket": {...}, "comments": [...]}`
- whoami        → `{"name", "display_name", "key", "email"}`
- projects      → `{"count", "projects": [{"key", "name", "id", "lead"}]}`
- auth status   → same dict as the table view (env paths, pat/cookies state)
- init / auth / config show / base-url / project → the config state below
- writes        → the audit row that was appended, plus `changed`
- `transition <KEY>` with no status → `{"key", "transitions": [{"id", "name", "to_status"}]}`
- reset         → `{"config_dir", "removed": [...], "folder_removed"}`

Config state (returned by `init`, the `auth` commands, and every
`config` subcommand — the setters return the *new* state, so a
third-party tool can drive setup and verify in one round trip):

```json
{
  "mode": "project_local",
  "config_dir": "/path/.jtr",
  "env_file": "/path/.jtr/.env",
  "session_file": "/path/.jtr/.jtr_session.json",
  "audit_log": "/path/.jtr/.jtr_audit.jsonl",
  "base_url": "https://tracker.example.com/jira",
  "project": "PROJ",
  "pat_set": false,
  "auth_method": "sso",
  "gitignore_updated": false,
  "skills_installed": ["jtr"],
  "authenticated": true
}
```

The PAT value is never returned by any command — only `pat_set`.
`gitignore_updated` / `skills_installed` / `authenticated` appear on
`init` and the `auth` commands; `config show` omits them.

### Driving jtr from another tool

`--json` is a promise that stdout is parseable, so under it jtr never
prompts. That changes two things:

- **Setup must be non-interactive.** Supply everything up front —
  `jtr init --ticket <url> --auth pat --pat "$TOKEN" --json`. A missing
  value that would otherwise be prompted for is an `input_required`
  error, not a hang.
- **Writes need `--yes`.** With no terminal to confirm on, a write
  without `--yes` fails with `confirmation_required` rather than
  writing unconfirmed. If stdin *is* a terminal, the preview and prompt
  go to **stderr** so stdout stays a single JSON object.

Two flags keep jtr out of a directory that isn't a user's project:

```bash
jtr init --ticket "$URL" --auth pat --pat "$TOKEN" --bare --json
```

`--bare` (= `--no-gitignore --no-skills`) writes config and nothing
else — no `/jtr` skill, no `.gitignore` edit.

To place that config somewhere other than the cwd, set
**`JTR_CONFIG_DIR`** to an absolute path. It names the config directory
outright and takes precedence over both `./.jtr/` and `~/.jtr/`, and
unlike the `.env` values it also moves the **cookie file** — so an
SSO-gated integration can pin all of jtr's state without controlling
the working directory:

```bash
export JTR_CONFIG_DIR=/opt/myapp/state/jtr
jtr init --ticket "$URL" --auth sso --bare --json   # writes into that dir
jtr auth status --json                              # reads from that dir
```

`jtr init --dir <path>` is the cwd-shaped alternative: it initializes
`<path>/.jtr/` and still writes `.gitignore` / skills relative to
`<path>`. An explicit `--dir` wins over `JTR_CONFIG_DIR`.

### JSON errors

With `--json`, failures are also emitted as JSON to stdout (still with
a non-zero exit code) so third-party tools can parse stdout
unconditionally:

```json
{
  "error": "not_authenticated",
  "message": "Not authenticated — the WebSEAL gateway intercepted the request.",
  "fix": "jtr auth sso"
}
```

Stable `error` codes:

| Code | Exit | When |
|---|---|---|
| `not_authenticated` | 2 | No creds, expired session, gateway interception, rejected PAT |
| `unsupported_deployment` | 2 | Base URL points at a deployment jtr doesn't support |
| `not_found` | 1 | Jira 404 |
| `jira_error` | 1 | Other Jira 4xx/5xx |
| `not_configured` | 1 | No base URL is set |
| `already_initialized` | 1 | `init` found an existing config and no `--force` |
| `input_required` | 1 | `--json` needs a value it would otherwise prompt for |
| `confirmation_required` | 1 | A write or `reset` under `--json` with no `--yes` and no terminal |
| `invalid_input` | 1 | Empty comment/label, unknown edit field |
| `no_auth_method` | 1 | `jtr auth` with nothing saved and no `--method` |
| `sso_failed` / `verification_failed` | 1 | Login didn't complete |
| `no_transition_match` / `ambiguous_transition` | 1 | The status argument matched zero / several transitions |

The `fix` field is included when there's an obvious remediation;
`message` is always a human string suitable for surfacing in a UI.

Without `--json`, errors go to **stderr** as plain text and stdout stays
empty. Rich is also told not to wrap when stdout isn't a terminal, so
piped human output keeps paths and URLs on one line.

Field names match the `Ticket` / `Comment` / `User` dataclasses in
`src/jtr/models.py`. Timestamps are the full ISO strings Jira returns
(the table view truncates them; JSON does not).

## Storage

State lives in one of two places — a global user config dir by
default, or `./.jtr/` in the cwd if you've run `jtr init` there.

```
.env                 JTR_BASE_URL, JTR_PAT, JTR_PROJECT,
                     JTR_AUTH_METHOD                          (0600)
.jtr_session.json    SSO cookies, if you ran `jtr auth sso`   (0600)
.jtr_audit.jsonl     append-only write log                    (0600)
```

Resolution, in order: `$JTR_CONFIG_DIR` if set, else `./.jtr/` if it
exists in the cwd, else `~/.jtr/`. All three files live together in
whichever directory wins — including the cookie file, which is why
`JTR_CONFIG_DIR` is the one setting that can relocate an SSO session
(see [Driving jtr from another
tool](#driving-jtr-from-another-tool)). (Upgrading from
≤0.2.x: any existing config under the old platform-specific path
— e.g. `~/Library/Application Support/jtr/` on macOS — is moved to
`~/.jtr/` automatically the first time jtr runs.)

To opt into a project-local config, run `jtr init` in the directory
you want to scope to. That creates `./.jtr/` (mode 0700) with an
empty `.env` scaffold, and appends `.jtr/` to `./.gitignore` if one
exists (`--no-gitignore` to skip). From then on, every `jtr` call
from that directory uses `./.jtr/` — completely isolated from your
global state. Useful for per-project audit trails or switching
base URLs / PATs without touching the global config.

If you pass a Jira ticket URL, the base URL and project key are
parsed out of it so you don't have to type either:

```bash
jtr init https://tracker.example.com/jira/browse/PROJ-123
# → base_url    = https://tracker.example.com/jira
# → project     = PROJ
```

Add `--auth sso` (or `pat`) and init finishes the login too, saving the
method so plain `jtr auth` works from then on. `--force` re-runs init
over an existing `./.jtr/`, updating only the values you pass.

### Multiple project folders

Each project folder gets its own `./.jtr/` — independent base URL,
PAT, project key, and audit log. Run `jtr init` once per folder
with a sample ticket URL from that project:

```
~/work/foo/   →  jtr init <ticket-url-with-project=FOO>   →  scopes to FOO
~/work/bar/   →  jtr init <ticket-url-with-project=BAR>   →  scopes to BAR
~/elsewhere/  →  (no ./.jtr/)                              →  falls back to ~/.jtr/
```

`cd` into a folder and `jtr` picks up *that* folder's config —
no flags, no env vars. The global `~/.jtr/` config is still there
as a fallback for any directory without its own `./.jtr/`.

After init, the only remaining step is `jtr auth pat` (or
`jtr auth sso`).

`jtr config show` always prints the active `env_file` and
`audit_log` paths so you can see which mode is in use.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `200 text/html` with TFIM kickoff JS | Missing context path, or WebSEAL doesn't accept the credentials (need VPN / cookies / role). |
| `Auth failed (HTTP 401/403)` | SSO cookies expired (`jtr auth sso`), wrong base URL (`jtr config show`), or — if you use one — a revoked PAT (`jtr auth pat`). |
| SSO browser opens but `whoami` still fails | Cookies expired or weren't captured for the right host. Re-run `jtr auth sso --force` to force a fresh browser login. |
| `Not configured.` | First run — set up with `jtr init <ticket-url>` (or `jtr config base-url …`). |
| `HTTP 4xx: <Jira error>` on a write | Jira rejected the change (permission, validation, unknown field/value). The message is the server's; the attempt is in `.jtr_audit.jsonl` with `ok: false`. |
| `No transition matches '<name>'` | Workflow doesn't expose that transition from the current status. Run `jtr transition <KEY>` (no status arg) to list what's actually available. |
| `No installed browser found for SSO login` | jtr needs Edge, Chrome or Chromium for `jtr auth sso`. Install one, or point `JTR_BROWSER` at the executable if it's in a non-standard location. |
| SSO browser opens and closes immediately | Another copy of that browser is blocking the scratch profile, or policy forbids remote debugging. Try the other browser with `JTR_BROWSER_CHANNEL=chrome` (or `msedge`). |
| `UnicodeDecodeError` / `invalid start byte` during config load | A `.env` written by jtr ≤0.5.1 on a non-UTF-8 Windows codepage. Delete the comment line at the top of `.env` (`jtr config show` prints its path), or delete `.env` and re-run. |

## Layout

```
src/jtr/
  cli.py        Typer entry, all subcommands
  config.py     .env load/save + paths
  auth.py       PAT + SSO cookie storage; browser login flow
  browser.py    Minimal CDP driver for the installed Edge/Chrome
  client.py     JiraClient: read + write (comment / edit / assign / transition)
  models.py     Ticket, Comment, User, Transition dataclasses
  views.py      Rich renderers + JSON printers for tables / detail view
  safety.py     preview / confirm / audit wrapper used by every write
  audit.py      .jtr_audit.jsonl append-only log
worca-jira-source/
                worca-cc plugin: Jira source connector driving this CLI
```
