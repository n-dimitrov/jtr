# Installing jtr

`jtr` is a Python CLI installed as a [`uv`](https://docs.astral.sh/uv/)
tool. The quickest way in is the one-line installer, also shown on the
[landing page](https://n-dimitrov.github.io/jtr/).

## Requirements

- **`uv`** — the installer downloads it automatically if missing.
- **Python 3.11+** — you don't install this; `uv` fetches a compatible
  Python when needed.
- **Microsoft Edge, Google Chrome or Chromium** — used by `jtr auth sso`
  for the browser login. jtr drives whichever is already installed;
  nothing is downloaded.
- **A Jira instance** — Server / Data Center (8.14+ for a PAT via
  `jtr auth pat`; any version for `jtr auth sso`) or Jira Cloud. See
  "Supported Jira deployments" in the README.

---

## One-line install

### macOS / Linux

```bash
curl -LsSf https://n-dimitrov.github.io/jtr/install.sh | sh
```

### Windows (PowerShell)

```powershell
irm https://n-dimitrov.github.io/jtr/install.ps1 | iex
```

The installer installs `uv` if needed, finds the latest release on
GitHub and installs its wheel with `uv tool install`. To install a
specific release instead, set `JTR_VERSION` first:

```bash
curl -LsSf https://n-dimitrov.github.io/jtr/install.sh | JTR_VERSION=1.2.0 sh
```

```powershell
$env:JTR_VERSION = '1.2.0'; irm https://n-dimitrov.github.io/jtr/install.ps1 | iex
```

> `jtr auth sso` on Windows uses the Edge that ships with it — no
> download and nothing extra to install. If you'd rather it used Chrome,
> set `JTR_BROWSER_CHANNEL=chrome`.

## Upgrade / uninstall

```bash
jtr update            # install the latest release and refresh the /jtr skill
jtr update --check    # only report whether a newer release exists
uv tool uninstall jtr
```

On **Windows** a running program can't replace itself, so `jtr update`
points you back at the installer one-liner; re-running it is the
upgrade. Re-running the installer upgrades on every OS.

Upgrading also refreshes copies of the `/jtr` skill that an older jtr
installed — this project's (`./.claude/skills/jtr`) and the global one
(`~/.claude/skills/jtr`) for Claude Code, and likewise for any other
agent you installed it for (`jtr skill install --agent ...`). A copy you
have edited is left alone. `jtr skill status` shows what is installed
where.

---

## Install from a downloaded release (offline / restricted network)

Use this when your shell can't reach GitHub, or you want to install
exactly the files you downloaded in a browser.

1. Download the **Source code (zip)** from the jtr
   [Releases](https://github.com/n-dimitrov/jtr/releases) page.
2. Extract it and run the bundled installer — it installs from the
   extracted folder instead of downloading:

```bash
# macOS / Linux
unzip jtr-1.2.0.zip          # adjust to the filename you got
cd jtr-1.2.0
./install.sh
```

```powershell
# Windows
Expand-Archive .\jtr-1.2.0.zip -DestinationPath .   # adjust filename
cd .\jtr-1.2.0\
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

A release's `jtr-<version>-py3-none-any.whl` works the same way: put it
next to `install.sh` / `install.ps1`, or pass its path as the argument
(`./install.sh <wheel>`, `.\install.ps1 -Path <wheel>`).

To upgrade an install made this way, download the newer zip and re-run
the installer.

### Without the installer

Install `uv` yourself, then install `jtr` from the extracted source
folder (the one containing `pyproject.toml`):

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh   # skip if you have uv
cd jtr-1.2.0
uv tool install --force .
```

```powershell
# Windows
winget install --id astral-sh.uv     # skip if you have uv; then open a NEW terminal
cd .\jtr-1.2.0\
uv tool install --force .
```

---

## First-run setup (all platforms)

```bash
jtr init --ticket https://tracker.example.com/jira/browse/PROJ-123 --auth sso
```

`jtr init` parses the base URL and project key from the ticket URL and
writes them into `./.jtr/.env`; `--auth sso` runs the browser login
straight away and remembers the method, so later refreshes are just
`jtr auth`. Use `--auth pat` instead to authenticate with a Personal
Access Token (prompted without echo). Run `jtr init` with no arguments
to be prompted for each value. Verify with:

```bash
jtr whoami
```

See [README.md](README.md) for usage and [EXAMPLES.md](EXAMPLES.md) for
copy-paste recipes.

---

## Developing on jtr itself

```bash
git clone https://github.com/n-dimitrov/jtr.git
cd jtr
uv sync
uv run jtr whoami
```
