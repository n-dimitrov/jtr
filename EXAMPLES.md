# jtr — examples

Copy-paste recipes. Run `jtr <cmd> --help` for full option lists.

## First-time setup

```bash
jtr init --ticket https://tracker.example.com/jira/browse/PROJ-123 --auth sso
jtr whoami               # smoke test
```

`--auth sso` opens your installed Edge/Chrome for the login and saves
the method, so refreshing the session later is just `jtr auth`. Other
ways to feed init the same settings:

```bash
jtr init                                       # prompts for everything
jtr init --base-url https://tracker.example.com/jira --project PROJ --auth pat
jtr init --ticket <url> --auth sso --browser msedge   # pick the browser
jtr init --project OTHER --force               # change one value, in place
```

Hacking on jtr itself? Clone, `uv sync`, then prefix every command
with `uv run` (e.g. `uv run jtr whoami`).

## Reading tickets

```bash
jtr whoami
jtr view PROJ-123
jtr list mine                                  # open, in JTR_PROJECT
jtr list mine --all-statuses                   # include Done/Closed
jtr list mine --project OTHER
jtr list mine --all --limit 200                # every project, raise cap
jtr projects                                   # projects you can see
```

Results come back one page at a time — `--limit` sizes the page,
`--start-at` moves the window:

```bash
jtr search "project = PROJ" --limit 50               # 1-50 of N
jtr search "project = PROJ" --limit 50 --start-at 50 # the next 50
```

On **Jira Cloud** there are no offsets: the search endpoint pages by
cursor and doesn't report a total, so use `--cursor` with the
`next_page_token` from the previous page (`jtr` prints it for you):

```bash
jtr search "project = PROJ" --limit 50                 # first page
jtr search "project = PROJ" --limit 50 --cursor <token>  # the next page
```

## Searching with JQL

```bash
# Simple text search
jtr search "summary ~ 'release'"
jtr search "text ~ 'kafka rebalance'"

# By status
jtr search "statusCategory = 'In Progress'"
jtr search "status in ('In Review', 'Blocked')"

# By people
jtr search "assignee = currentUser() AND statusCategory != Done"
jtr search "reporter = currentUser() ORDER BY created DESC"
jtr search "assignee in (jdoe, asmith)"

# By time
jtr search "updated >= -7d"
jtr search "created >= startOfWeek()"
jtr search "resolved >= startOfMonth() AND assignee = currentUser()"

# By release / version
jtr search "fixVersion = '2026.06'"
jtr search "fixVersion in unreleasedVersions()"

# By labels (exact match, case-sensitive — `Release` ≠ `release`)
jtr search 'labels = NeedsTriage'
jtr search 'labels in (release, blocker)'                # any of
jtr search 'labels = release AND labels = blocker'       # both
jtr search 'labels is EMPTY'

# Combine: my open tickets with a specific label
jtr search 'labels = NeedsTriage AND assignee = currentUser() AND statusCategory != Done'

# Combine: label + status (any assignee in the scoped project)
jtr search "labels = 'NeedsTriage' AND statusCategory = 'In Progress'"

# Cross-project (ignore JTR_PROJECT scope)
jtr search 'labels = NeedsTriage' --all
```

The actual JQL sent (after auto-scoping) is printed above the table —
use that to debug "why did this match nothing".

## Adding comments

```bash
jtr comment PROJ-123 "Picked this up, investigating."
jtr comment PROJ-123 "Done — see PR #842." --yes     # skip confirm
```

## Editing fields

`jtr edit <KEY> <field> <value>` does a full replace and shows
the diff before applying.

```bash
jtr edit PROJ-123 summary "Fix retry logic in payment webhook"
jtr edit PROJ-123 priority High
jtr edit PROJ-123 description "Updated scope: now includes API gateway."

# Labels — comma-separated, replaces the entire list
jtr edit PROJ-123 labels "NeedsTriage,backend,release"
jtr edit PROJ-123 labels ""                          # clear all

# Or use the single-label primitives (idempotent — no-op if already in/out)
jtr label add PROJ-123 release
jtr label remove PROJ-123 release
jtr label add PROJ-123 release --yes                 # scripted

# Fix versions — same shape as labels
jtr edit PROJ-123 fixVersions "2026.06,2026.07"
```

To **add** one label without losing the others, run `jtr view`
first and re-send the full list with the new entry appended.

## Assigning

```bash
jtr assign PROJ-123 jdoe              # set
jtr assign PROJ-123 --unassign        # clear
jtr assign PROJ-123 jdoe --yes        # scripted, no prompt
```

On Server/DC the username is Jira's `name` field (visible as the
trailing part of the "Assignee" row in `jtr view`).

On **Jira Cloud** people are identified by an opaque `accountId`, so pass
an email or display name and `jtr` looks it up:

```bash
jtr assign PROJ-123 jdoe@acme.com                    # looked up for you
jtr assign PROJ-123 "557058:1a2b..." --account-id    # skip the lookup
```

A name matching several accounts is reported as `ambiguous_user` rather
than guessed at — use the exact email, or pass the accountId.

## Transitions

```bash
jtr transition PROJ-123                       # list what's available
jtr transition PROJ-123 "In Progress"
jtr transition PROJ-123 progress              # partial match if unambiguous
jtr transition PROJ-123 "Done" -m "Fixed in PR #842."   # transition + comment
```

Match is case-insensitive on the transition name **or** the target
status. Ambiguous matches print the candidates and exit non-zero.

## Audit log

Every successful or failed write appends one JSON line to the audit
log. Cancellations don't. The file lives in the active config dir —
`./.jtr/.jtr_audit.jsonl` in project-local mode, or `~/.jtr/.jtr_audit.jsonl`
otherwise. `jtr config show` reports the active path.

```bash
LOG=$(jtr config show | awk '/audit_log/ {print $2}')

# All writes I made today
grep "$(date -u +%Y-%m-%d)" "$LOG"

# Pretty-print the last 5 writes (needs jq)
tail -5 "$LOG" | jq .

# Just the failures
jq 'select(.ok == false)' "$LOG"

# What did I touch on PROJ-123
jq 'select(.key == "PROJ-123")' "$LOG"
```

## Auth maintenance

```bash
jtr auth status              # what's configured
jtr auth sso                 # refresh expired cookies (primary credential)
jtr auth pat                 # rotate the optional PAT
jtr auth logout              # wipe PAT + cookies
jtr config show              # paths, scope, audit log location
```

## Driving jtr from another tool

Every command takes `--json`, and under it jtr never prompts — so supply
everything up front and pass `--yes` on writes.

```bash
# Set up and verify in one round trip; --bare skips the .gitignore
# edit and the /jtr skill install.
jtr init --ticket "$URL" --auth pat --pat "$TOKEN" --bare --json \
  | jq '{base_url, project, authenticated}'

# Same, against Jira Cloud — Basic auth needs both halves up front.
jtr init --base-url https://acme.atlassian.net \
  --auth token --email "$EMAIL" --token "$API_TOKEN" --bare --json \
  | jq '{base_url, deployment, authenticated}'

# Keep every file — .env, cookies, audit log — out of the cwd.
export JTR_CONFIG_DIR=/opt/myapp/state/jtr
jtr auth status --json | jq -e '.cookies > 0'

# Writes return the audit row they appended.
jtr comment PROJ-123 "Pipeline finished." --yes --json | jq '{ok, changed}'

# Page through a large result set (Server/DC — offsets).
start=0
while [ "$start" != "null" ]; do
  page=$(jtr search "project = PROJ" --limit 50 --start-at "$start" --json)
  echo "$page" | jq -r '.tickets[].key'
  start=$(echo "$page" | jq '.next_start_at')
done

# Same on Cloud — cursors. `has_more` works on both, so a loop written
# against it doesn't need to know which deployment it's talking to.
cursor=""
while :; do
  page=$(jtr search "project = PROJ" --limit 50 \
    ${cursor:+--cursor "$cursor"} --json)
  echo "$page" | jq -r '.tickets[].key'
  [ "$(echo "$page" | jq -r '.has_more')" = "true" ] || break
  cursor=$(echo "$page" | jq -r '.next_page_token')
done
```

Failures are JSON on stdout too, with a non-zero exit — so parse stdout
unconditionally and check the exit code separately:

```bash
jtr view NOPE-1 --json | jq -r '.error // .ticket.key'   # → not_found
```
