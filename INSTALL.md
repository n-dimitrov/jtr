# Installing jtr

`jtr` is a Python CLI installed as a [`uv`](https://docs.astral.sh/uv/)
tool. The repo is private, so you install from a release you download in
your browser — no GitHub CLI, no tokens.

**The flow is the same on every OS:** download the release, extract it,
run the bundled installer.

## Requirements

- **`uv`** — the installer downloads it automatically if missing.
- **Python 3.11+** — you don't install this; `uv` fetches a compatible
  Python when needed.
- **Microsoft Edge, Google Chrome or Chromium** — used by `jtr auth sso`
  for the browser login. jtr drives whichever is already installed;
  nothing is downloaded.
- **A Jira Server / Data Center instance** — 8.14+ if you want to use a
  PAT (`jtr auth pat`); any version for `jtr auth sso`. **Jira Cloud
  (`*.atlassian.net`) is not supported** — see the "Supported Jira
  deployments" section in the README.

---

## macOS / Linux

1. Download the **Source code (zip)** from the jtr **Releases** page.
2. Extract it and run the bundled `install.sh`:

```bash
unzip jtr-0.9.0.zip          # adjust to the filename you got
cd jtr-0.9.0
./install.sh
```

`install.sh` installs `uv` if needed, then installs `jtr` from the
extracted source.

## Windows

1. Download the **Source code (zip)** from the jtr **Releases** page.
2. Extract it and run the bundled `install.ps1`:

```powershell
Expand-Archive .\jtr-0.9.0.zip -DestinationPath .   # adjust filename
cd .\jtr-0.9.0\
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

> `jtr auth sso` uses the Edge that ships with Windows — no download and
> nothing extra to install. If you'd rather it used Chrome, set
> `JTR_BROWSER_CHANNEL=chrome`.

## Upgrade / uninstall (all platforms)

- **Upgrade:** download the newer zip and re-run the installer.
- **Uninstall:** `uv tool uninstall jtr`

---

## Manual install (without the bundled installer)

Install `uv` yourself, then install `jtr` from the extracted source
folder (the one containing `pyproject.toml`).

### macOS / Linux

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # skip if you have uv
cd jtr-0.9.0
uv tool install --force .
```

### Windows

```powershell
winget install --id astral-sh.uv     # skip if you have uv; then open a NEW terminal
cd .\jtr-0.9.0\
uv tool install --force .
```

Installing from a source zip pins you to that release with no
auto-upgrade — re-run the command against a newer download to update.

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
git clone git@github.com:BD-AI-SDLC/jtr.git
cd jtr
uv sync
uv run jtr whoami
```
