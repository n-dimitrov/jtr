---
name: jtr
description: "Track & Release (Jira) operations via the local `jtr` CLI — list/search/view tickets, comment, edit fields, label, assign, transition. Use whenever the user mentions Jira, Track & Release, a ticket key like PROJ-NNNNN, asks about their work queue, or invokes /jtr. Reads run freely; writes need explicit per-turn user authorization."
trigger: /jtr
---

# /jtr

`jtr` is a local CLI wrapping the Track & Release Jira REST API.
It supports both **Jira Server / Data Center** and **Jira Cloud**
(`*.atlassian.net`); the deployment is detected from the base URL. A few
commands behave differently on each — see "Deployment differences" below.
Run `jtr config show` if you need to know which one this config targets.
Every write is **preview → confirm → POST/PUT → audit**, so the tool is
safe to operate but interactive for writes by default. Reads run
without prompting and fail fast if not configured. See `EXAMPLES.md`
in the project root for the user-maintained recipe book.

## When to invoke

Auto-activate when the user mentions any of:
- Jira, Track & Release, the Jira UI
- A ticket key (`PROJECT-NNNNN`, e.g. `PROJ-NNNNN`)
- "my tickets", "what's assigned to me", "open tickets"
- Workflow language: transition, in progress, done, blocked
- Labels, fix versions, JQL, assignee
- Explicit `/jtr ...`

Don't activate for: PR/code-review questions, GitHub issues unrelated
to T&R, generic project planning.

## Trust model — reads vs writes

`jtr` is a **write-capable tool against shared state** (other people see
the comments, transitions, edits). Apply the same care as `git push`:

| Action | Permission |
|---|---|
| `jtr list / search / view / whoami / projects / auth status / config show` | Run freely — read-only. |
| `jtr comment / edit / label add\|remove / assign / transition` | **Require explicit per-action user authorization.** Don't use `--yes` unless the user approved that exact change this turn. Default CLI behavior prompts y/N, which will hang in a non-TTY shell — so the flow is: show the command, get approval, then run with `--yes`. |
| `jtr init`, `jtr auth pat\|sso\|token\|logout`, `jtr config base-url\|project\|deployment` | Touches credentials/config — confirm before running. |
| `jtr reset` | **Destructive** — deletes all jtr-managed data and (in project-local mode) removes `./.jtr/`. Always confirm; never pass `--yes` unsolicited. |

After any write, surface the audit-log line (or at least the
`ok:true/false` + key) so the user sees what landed. The audit log
path differs by mode — discover it with `jtr config show --json | jq -r .audit_log` (or the table view's `audit_log` row).

## Command quick-reference

### Reading (all accept `--json`)
```bash
jtr whoami [--json]                              # verify auth
jtr view <KEY> [--json]                          # full ticket detail
jtr list mine [--all-statuses] [--all] [--json]  # assigned to me; --all = cross-project
jtr list mine --project KEY --limit N [--start-at N|--cursor TOK] [--json]
jtr search "<JQL>" [--all] [--project KEY] [--limit N] [--start-at N|--cursor TOK] [--json]
jtr projects [--json]                            # projects the user can see
jtr auth status [--json]                         # cookie count + captured_at
jtr config show [--json]                         # env paths + values
```

### Setup / reset (touches config — confirm first)
```bash
jtr init [<ticket-url>] [--ticket <url>] [--base-url <url>] [--project KEY]
         [--auth sso|pat|token] [--pat <v>] [--email <addr>] [--token <v>]
         [--deployment server|cloud|auto]
         [--browser CHANNEL] [--timeout N]
         [--no-auth] [--force] [--no-gitignore]
         [--no-skills] [--bare] [--dir <path>]
         [--json]                                # create ./.jtr/ and authenticate
jtr auth [--method sso|pat|token] [--json]       # login with the saved method
jtr auth token [--email <addr>] [--token <v>]    # Cloud: email + API token
jtr config deployment server|cloud|auto          # override hostname detection
jtr reset [--yes] [--json]                       # delete all jtr-managed data (destructive)
```

### Deployment differences

| | Server / DC | Cloud |
|---|---|---|
| Auth command | `jtr auth pat` / `jtr auth sso` | `jtr auth token` (email + API token) |
| Search paging | `--start-at N`, `total` is a number | `--cursor TOK`, `total` is `null` |
| `jtr assign <KEY> <who>` | username | email, display name, or accountId |

`jtr auth sso` is Server/DC only and refuses on Cloud — don't suggest it
there. On Cloud, don't report a result count you weren't given: `--json`
returns `"total": null`, and `has_more` + `next_page_token` are what say
whether more pages exist. For `assign` on Cloud, prefer the user's email;
if the CLI reports `ambiguous_user`, show the candidates and ask rather
than picking one.

### Writing (need user OK each time)
```bash
jtr comment <KEY> "<text>" --yes
jtr edit <KEY> <field> <value> --yes             # full replace
jtr label add|remove <KEY> <name> --yes          # idempotent single-label
jtr assign <KEY> <user> --yes   |  jtr assign <KEY> --unassign --yes
jtr transition <KEY>                             # NO --yes — lists what's available
jtr transition <KEY> "<status>" [-m "msg"] --yes
```

All of these accept `--json`, which returns the audit row that was
appended — prefer it over re-reading `.jtr_audit.jsonl`.

### Config setters (return new state with `--json`)
```bash
jtr config base-url <url> [--json]
jtr config project <key> [--json]                # "" clears the scope
```

### Editable fields (`jtr edit`)
| field | value syntax |
|---|---|
| `summary`, `description`, `priority` | string |
| `labels`, `fixVersions` | comma-separated, **replaces the whole list** |

For incremental label changes, prefer `jtr label add/remove` — they're
idempotent (no API call if already in the desired state) and don't
require knowing the rest of the list.

## JQL cheatsheet

| Need | Example |
|---|---|
| Text | `summary ~ 'release'`, `text ~ 'kafka rebalance'` |
| Status | `statusCategory = 'In Progress'`, `status in ('In Review','Blocked')` |
| People | `assignee = currentUser()`, `assignee in (jdoe, asmith)`, `reporter = currentUser()` |
| Time | `updated >= -7d`, `created >= startOfWeek()`, `resolved >= startOfMonth()` |
| Releases | `fixVersion = '2026.06'`, `fixVersion in unreleasedVersions()` |
| Labels | `labels = AvailableResources`, `labels in (release, blocker)`, `labels is EMPTY` |
| Order | append `ORDER BY updated DESC` |

### JQL gotchas (Server/DC instances)
- **Labels are case-sensitive.** `availableresources` ≠ `AvailableResources`. When in doubt, `jtr view` a known ticket and copy the exact spelling.
- If `JTR_PROJECT` is set, list/search auto-AND with `project = <KEY>` unless the JQL already mentions `project`, `--all` is passed, or `--project OTHER` overrides. The actual JQL sent prints above each result table — read it to debug "matched nothing".
- Single quotes around string values are safest; double quotes inside the outer shell-quoted JQL clash.

## Transitions

- `jtr transition <KEY>` with no status arg lists the transitions available **from the current state of that specific ticket** — workflow state determines what's exposed.
- Match is case-insensitive on transition name OR target status; partial OK if unambiguous. Ambiguous prints candidates and exits non-zero.
- `-m "msg"` adds a comment with the transition in a single audited action.

## Auth & config

### Resolution
- `./.jtr/` exists in cwd → **project-local** mode (files inside it)
- Otherwise → **global** mode, files in `~/.jtr/`

(In ≤0.2.x the global location was `user_config_dir("jtr")`; 0.3.0+
auto-migrates that to `~/.jtr/` on first run.)

### Files in the active config dir
- `.env` — `JTR_BASE_URL`, `JTR_PAT`, `JTR_PROJECT` (0600)
- `.jtr_session.json` — SSO cookies if `jtr auth sso` ran (0600)
- `.jtr_audit.jsonl` — append-only write log (0600)

### First-run setup (no more interactive prompts as of 0.4.0)
Reads now fail-fast with `Not configured. Fix: jtr init <ticket-url>` —
they do **not** prompt. The setup flow is one-way and explicit:
```bash
jtr init --ticket https://tracker.example.com/jira/browse/PROJ-123 \
         --auth sso                              # config + login in one command
jtr auth                                         # later: re-login, method remembered
```
`--ticket <url>` parses the base URL (incl. context path) and project
key out of the ticket URL — no manual typing. `--base-url` / `--project`
set either directly; `--force` updates an existing `./.jtr/` in place.

`--auth {sso|pat}` saves the method in `JTR_AUTH_METHOD` and runs it, so
plain `jtr auth` works afterwards. In a non-TTY shell init does **not**
prompt — pass every value as a flag, and use `--pat "$PAT"` (or
`--no-auth`) rather than letting it ask for a token.

### Inspect / diagnose
```bash
jtr config show          # mode, env paths, base_url, PAT presence, project
jtr config show --json   # same data, machine-readable
jtr auth status [--json] # cookie count + captured_at + paths
```

### Selective auth clear
```bash
jtr auth logout              # both PAT + cookies
jtr auth logout --cookies    # cookies only (keep PAT — useful for SSO refresh)
jtr auth logout --pat        # PAT only (keep cookies)
```

### Auth failure signatures (now translated to clear messages)
- `Not authenticated — the WebSEAL gateway intercepted the request.` → cookies missing/expired. Fix: `jtr auth sso` (or VPN).
- `Not configured.` → no base URL set. Fix: `jtr init <ticket-url>`.
- `Auth failed (HTTP 401/403)` → PAT revoked or wrong base URL.
- The "Got non-JSON" raw dump is gone as of 0.2.0; if you see it, the user is on an older version — recommend `uv tool upgrade jtr`.

## Audit log queries

The audit log is the source of truth for "what did I/we change?".
The path differs by mode — resolve it dynamically:

```bash
LOG=$(jtr config show --json | jq -r .audit_log)

jq 'select(.key == "PROJ-123")' "$LOG"
jq 'select(.ok == false)' "$LOG"                              # failures only
jq 'select(.action == "transition")' "$LOG"                   # by action
grep "$(date -u +%Y-%m-%d)" "$LOG"                            # today
```

Cancellations don't log; successes and failures both do.

## JSON output for scripting

When the user is wiring jtr into a third-party tool, prefer `--json`
on every supported command. Output goes to **stdout** in all cases —
including errors — so a tool can do `json.loads(stdout)` unconditionally
and check exit code separately.

Every command accepts `--json`, including `init`, the `auth` commands
and every write.

Success shapes:
| Command | Shape |
|---|---|
| `list mine` / `search` | `{"jql", "count", "start_at", "max_results", "total", "next_start_at", "next_page_token", "has_more", "tickets": [...]}` — on Cloud `total` and `next_start_at` are `null`; page with `next_page_token` |
| `view` | `{"ticket": {...}, "comments": [...]}` |
| `whoami` | `{"name", "display_name", "key", "account_id", "email"}` (`name` is empty on Cloud, `account_id` empty on Server/DC) |
| `projects` | `{"count", "projects": [{"key", "name", "id", "lead"}]}` |
| `auth status` | `{env_file, base_url, deployment, api_version, auth_method, email, pat, cookies, cookies_file, cookies_captured_at}` |
| `init` / `auth` / `config show / base-url / project / deployment` | `{"mode", "config_dir", "env_file", "session_file", "audit_log", "base_url", "deployment", "api_version", "project", "pat_set", "email", "auth_method"}` (setters return the *new* state; `init`/`auth` add `gitignore_updated`, `skills_installed`, `authenticated`) |
| writes (`comment`/`edit`/`label`/`assign`/`transition`) | the audit row: `{ts, action, key, ok, before, after, result, changed}` |
| `transition <KEY>` (no status) | `{"key", "transitions": [{"id", "name", "to_status"}]}` |

Error shape (any `--json` command on failure):
```json
{ "error": "<code>", "message": "<human>", "fix": "<optional command>" }
```
Stable codes: `not_authenticated` and `unsupported_deployment` (exit 2);
`not_found`, `jira_error`, `not_configured`, `already_initialized`,
`input_required`, `confirmation_required`, `invalid_input`,
`no_auth_method`, `sso_failed`, `verification_failed`,
`no_transition_match`, `ambiguous_transition`, `unsupported_option`
(a paging flag used on the wrong deployment), `ambiguous_user` and
`user_not_found` (Cloud assignee lookup) — all exit 1. The PAT/token
*value* is never returned by any endpoint — only `pat_set: true|false`.

Under `--json` jtr never prompts: supply every value up front (a missing
one is `input_required`, not a hang) and pass `--yes` on writes (without
it, and with no terminal, you get `confirmation_required`).

Programmatic setup pattern — one call, one round trip:
```bash
jtr init --ticket "$URL" --auth pat --pat "$PAT" --bare --json
# → the full config state, incl. base_url, project, authenticated
```
`--bare` skips the `.gitignore` edit and the skill install; use it when
initializing a directory that isn't the user's own project. To pin every
file (including the SSO cookie) somewhere specific, export
`JTR_CONFIG_DIR=/abs/path` — it beats both `./.jtr/` and `~/.jtr/`.

Paging: `--limit` sizes a page. On Server/DC `--start-at` offsets it and
`next_start_at` is `null` on the last page. On Cloud there are no offsets:
pass `next_page_token` back as `--cursor`, and check `has_more` — `total`
is always `null` there, so never quote a result count on Cloud.

## Output style

When reporting to the user:
- For lists/searches: show the JQL that was run + the count + key/summary/status of each row. Don't dump the full table verbatim unless asked.
- For `view`: prioritize summary, status, assignee, priority, fix versions, labels, last 2-3 comments. The user can ask for full detail.
- For writes: report `ok:true/false`, key, the field/action, and the new value. Don't paraphrase the audit line — quote it.

## Anti-patterns

- Don't run a write with `--yes` because "the user probably wants it" — get the explicit OK.
- Don't grep `.env` or `.jtr_session.json` — they contain credentials. Use `jtr config show [--json]` and `jtr auth status [--json]` instead. `config show --json` returns `pat_set` (boolean), never the PAT value.
- Don't construct PATs, base URLs, or project keys from memory — read them via `jtr config show`.
- Don't try to update a label list by re-sending the full `jtr edit labels` value unless the user explicitly wants the full replace — use `jtr label add/remove` for incremental changes.
- Don't infer transitions from previous tickets — always `jtr transition <KEY>` first to see what's actually available on this one.
