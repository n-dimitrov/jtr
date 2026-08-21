# jira-source (worca-cc plugin)

Pull tasks from Jira into worca-cc's New Pipeline, via the `jtr` CLI.
Read-only by default; write-back is a per-run choice in the New Pipeline
source panel (see **Write-back**).

Both deployments work — **Server / Data Center** (including behind an
SSO gateway) and **Cloud** (`*.atlassian.net`). They differ in the credentials
they accept and the way they page, and neither is ever guessed here: `jtr init`
reports which one the server address is, and everything else follows from that.

jtr owns everything Jira-specific — auth (browser SSO through a WebSEAL-style
gateway, a personal access token, or a Cloud account email + API token), the
instance config, and the REST dialect. This connector only shells out to
`jtr ... --json`, and drives jtr's setup from the plugin's own settings pane.

## Prerequisites

Only one: install **jtr 0.10.0 or newer** so that `jtr` runs in a terminal.
Everything else is done from the plugin's settings pane — there are no setup
commands to run by hand.

0.10.0 is the floor because Cloud lives there: `--deployment`, `jtr auth token`
and cursor paging are all new in it, and the deployment this connector branches
on is only reported by `init --json` from that release. (0.9.0 brought
`init --json`, `--bare` and `$JTR_CONFIG_DIR`, which the setup flow also needs.)
Connect checks the version once and says so plainly if it's too old.

## Install

    worca plugin link examples/plugins/jira-source     # dev, from this repo

or publish the directory to a plugin repo and `worca plugin add` / `install` it.

## Setup (all in the UI)

Open Plugins → jira-source → Settings, paste any ticket URL from your instance
(e.g. `https://tracker.example.com/jira/browse/PROJ-123` or
`https://acme.atlassian.net/browse/PROJ-123` — the server address and project
key are read out of it) and press **Connect**. Connect advances setup one step
per call and the pane polls it.

Authentication defaults to **auto**, which picks what the deployment can
actually take, so on Cloud there is nothing to choose. The three methods:

- **sso** (Server/DC) — Connect configures jtr, then opens your browser for the
  sign-in. Complete it there and the pane flips to "connected" on its own. (The
  login runs detached because it takes minutes, while a connector call is killed
  at 30s.) If no browser opens, Connect reports what the login said; the SSO
  browser dropdown usually fixes it.
- **pat** (Server/DC) — headless, no browser: paste the token and Connect
  stores and verifies it. Behind an SSO gateway a token alone cannot get
  through, so use sso there.
- **token** (Cloud) — the Atlassian account email plus an API token from
  <https://id.atlassian.com/manage-profile/security/api-tokens>. Atlassian does
  not accept browser cookies or PATs as REST credentials, so this is Cloud's
  only method; picking sso or pat there is refused immediately rather than
  failing later as a 401.

The deployment is read off the server address, which is a guess: a Cloud tenant
can sit on a vanity domain and a self-hosted instance on a Cloud-shaped one. If
it guesses wrong, pin it with the **Deployment** dropdown.

Re-press Connect whenever the session expires. Everything jtr writes (config,
cookies, audit log) is pinned by `$JTR_CONFIG_DIR` to a directory this plugin
owns under your home, so it never touches your projects and never depends on
which directory the worca server was started from.

## Profiles (several Jira instances)

This source is `multiProfile`: one install can hold several independent
configurations — work and OSS, two trackers, two projects on one tracker. Each
profile has its own settings, its own token, and its own jtr config directory
(`…/plugins/jira-source/data/jtr-home/<profile>/`), so they never share a
session — sharing one would mean the last instance you connected silently
answering for both.

A project is **bound** to one profile, and that binding is what a pipeline run
uses; the profile that fetched a ticket is also recorded on the run, so a result
is always reported back to the instance the ticket actually came from, even if
the project is re-bound later. A workspace with no binding of its own inherits
from its member projects when they agree, and asks when they don't.

## Config

| key | type | default | meaning |
|---|---|---|---|
| ticketUrl | text | — | Any ticket URL from your instance; the server address and project key are derived from it. |
| deployment | select | `auto` | `auto` detects Server/DC vs Cloud from the server address; `server` / `cloud` pin it. |
| authMethod | select | `auto` | `auto` picks the deployment's method. `sso` opens a browser; `pat` is a Server/DC token; `token` is Cloud's email + API token. |
| email | text | — | Atlassian account email, used only when authMethod is `token`. |
| pat | secret text | — | Server/DC personal access token or Cloud API token; unused with `sso`. Stored 0600 in the plugin's secrets file, never in the DB. |
| browser | select | `auto` | Which installed browser the SSO sign-in opens. |
| transitionOnComplete | text | — | Optional workflow transition/status name (e.g. `Done`) applied when a run that chose write-back completes successfully. Empty = comment only. |
| jtrPath | text | `jtr` | Command or absolute path to the jtr CLI. The connector child runs with `PATH`+`HOME` only — if the server's PATH lacks jtr (common for GUI-launched servers), set an absolute path like `/Users/you/.local/bin/jtr`. |

## Usage

- **Paste a ticket key or browse URL** into the task browser's search box and
  that exact ticket comes back (`key = PROJ-123`), whatever the JQL filter says.
  This is the fast path: ticket → pipeline.
- **Filter** is a dropdown of common queries — *None* (the default),
  *Assigned to me*, *Reported by me*, *Updated this week*. With *None* and
  nothing else supplied, nothing is listed: no filter is ever substituted on
  your behalf.
- Queries are **scoped to the configured project** (derived from the sample
  ticket URL), so a filter doesn't span every project on the instance. Two
  exceptions: a pasted ticket key is never scoped — that's how you pull a
  ticket from another project — and JQL that names a project itself is left
  alone rather than being given a second, contradictory constraint.
- **JQL** overrides the dropdown whenever it is non-empty, so a box you filled
  in is never ignored — that is also why there's no "Custom" entry in the
  dropdown: the box *is* custom mode. Its terms are sent as-is (with `--all`,
  so jtr's default project is not silently ANDed in). Editing either control
  re-runs the search immediately.
- Free text in the search box becomes `text ~ "..."` — on its own when no
  filter is active, otherwise ANDed onto whichever of the two won.
- Results are sorted newest-updated-first unless your JQL has its own
  `ORDER BY`, which is always respected. Without a sort Jira picks its own, and
  a text search can otherwise open on tickets from years ago.
- Results page 50 at a time, by whichever mechanism the deployment has:
  Server/DC counts offsets (`--start-at` / `next_start_at`), Cloud walks opaque
  tokens (`--cursor` / `next_page_token`) and reports no total. Sending the
  wrong one is an error in jtr, not a silently truncated result.
- Ticket description + comments become the pipeline prompt; Jira wiki markup
  is lightly converted (headings, `{code}`/`{noformat}` fences, links).

## Write-back

A per-run choice, made in the New Pipeline source panel: the **Write result
back** select defaults to *No*, and the choice is pinned on the run when the
ticket is fetched — changing it later never affects runs already started.
Pick *Yes* and the finished pipeline is posted onto its ticket as a comment —
the run's summary (diffstat, review counts, branch, key things to check)
converted from markdown to Jira wiki markup, plus a link to the pull request
when one exists. Failed and needs-human runs comment too, so the ticket
always says what happened.

**Transition on completion** (profile settings — a policy of the instance,
not of one run) goes one step further: give it a transition or status name
(e.g. `Done`) and a *successfully* completed run that chose write-back also
moves the ticket. Transition names are workflow-specific —
`jtr transition PROJ-123` lists the ones valid for a ticket. The comment is
always posted before the transition is attempted, so a wrong name never loses
the summary; the error shows up in the run's results view, where
**Report result** retries it.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `jtr not found at "jtr"` | Set `jtrPath` to the absolute path of the jtr binary. |
| Not authenticated | Press **Connect** — it re-runs the sign-in. Check VPN if it keeps failing. |
| "The browser login did not complete" | The message includes what the sign-in itself reported. If it found no browser, pick one explicitly in the SSO browser dropdown. |
| First call is slow | jtr is a Python CLI — ~1s startup per call is normal. |
| Wrong tickets / wrong instance | Update the sample ticket URL and press Connect; that re-runs `jtr init` against the new instance. |
| "Jira Cloud cannot authenticate with sso/pat" | Leave Authentication on `auto`, or set it to `token` and fill in the account email. |
| Cloud tickets 404, or a self-hosted instance is treated as Cloud | The deployment was detected from the hostname. Pin it with the **Deployment** dropdown and press Connect. |
