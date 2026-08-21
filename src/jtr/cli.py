from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import NoReturn

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__, audit, auth, config, safety, views
from . import dialect as dialect_mod
from .client import AmbiguousUser, JiraClient, JiraError, UserNotFound

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Local CLI for Jira / Track & Release tickets (Server/Data Center or Cloud).",
)
auth_app = typer.Typer(
    invoke_without_command=True,
    help=(
        "Authenticate. With no subcommand, uses the method saved by "
        "`jtr init` (or the last `jtr auth sso|pat|token`), so there's "
        "nothing to remember."
    ),
)
config_app = typer.Typer(no_args_is_help=True, help="Read/write jtr config.")
list_app = typer.Typer(no_args_is_help=True, help="Canned ticket lists.")
label_app = typer.Typer(no_args_is_help=True, help="Add/remove single labels (idempotent).")
app.add_typer(auth_app, name="auth")
app.add_typer(config_app, name="config")
app.add_typer(list_app, name="list")
app.add_typer(label_app, name="label")


def _make_console(*, stderr: bool = False) -> Console:
    """Wrap for humans only.

    Rich sizes non-TTY output to 80 columns, which splits long paths and
    URLs mid-token and makes piped output unscrapeable. When the stream
    isn't a terminal there is no width to respect, so don't invent one.
    """
    stream = sys.stderr if stderr else sys.stdout
    try:
        interactive = stream.isatty()
    except (AttributeError, ValueError):
        interactive = False
    return Console(stderr=stderr, soft_wrap=not interactive)


console = _make_console()
err = _make_console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"jtr {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    pass


def _ensure_creds() -> auth.Credentials:
    """Fail fast with an actionable error if base URL or creds are missing."""
    cfg = config.load()
    if cfg is None:
        raise auth.SessionExpired(
            "Not configured.\n"
            "Fix: jtr init <ticket-url>   (or jtr config base-url <url>)"
        )
    # load_credentials() knows which credential shape this deployment needs,
    # so it owns the "not authenticated" verdict as well as the fix hint.
    return auth.load_credentials()


def _current_dialect() -> dialect_mod.Dialect | None:
    """The active dialect, or None if unconfigured / misconfigured."""
    cfg = config.load()
    if cfg is None:
        return None
    try:
        return auth.dialect_for(cfg)
    except auth.UnsupportedDeployment:
        return None


def _fail(
    code: str,
    message: str,
    *,
    json_out: bool = False,
    fix: str | None = None,
    exit_code: int = 1,
) -> NoReturn:
    """Emit an error on the channel the caller can parse, then exit."""
    if json_out:
        views.print_json_error(code, message, fix=fix)
    else:
        err.print(f"[red]{message}[/]")
        if fix:
            err.print(f"[dim]Fix:[/] {fix}")
    raise typer.Exit(code=exit_code)


def _handle_expired(e: auth.SessionExpired, *, json_out: bool = False) -> None:
    if json_out:
        msg, _, fix = str(e).partition("\nFix:")
        code = (
            "unsupported_deployment"
            if isinstance(e, auth.UnsupportedDeployment)
            else "not_authenticated"
        )
        views.print_json_error(code, msg.strip(), fix=fix.strip() or None)
    else:
        err.print(f"[red]{e}[/]")
    raise typer.Exit(code=2)


def _handle_jira_error(e: JiraError, *, json_out: bool = False) -> None:
    if json_out:
        code = "not_found" if getattr(e, "status", None) == 404 else "jira_error"
        views.print_json_error(code, str(e))
    else:
        err.print(f"[red]{e}[/]")
    raise typer.Exit(code=1)


def _config_state() -> dict:
    """Machine-readable snapshot of the active config. PAT value not exposed."""
    cfg = config.load()
    d = _current_dialect()
    return {
        "mode": "project_local" if config.is_project_local() else "global",
        "config_dir": str(config.config_dir()),
        "env_file": str(config.env_path()),
        "session_file": str(config.session_path()),
        "audit_log": str(audit.path()),
        "base_url": cfg.base_url if cfg else None,
        "deployment": d.deployment if d else None,
        "api_version": d.api_version if d else None,
        "project": cfg.project if cfg and cfg.project else None,
        "pat_set": bool(cfg and cfg.pat),
        "email": cfg.email if cfg and cfg.email else None,
        "auth_method": cfg.auth_method if cfg else None,
    }


def _print_config_state_json(**extra) -> None:
    views.print_json({**_config_state(), **extra})


@config_app.command("base-url")
def config_base_url(
    url: str = typer.Argument(..., help="New base URL."),
    json_out: bool = typer.Option(False, "--json", help="Emit new state as JSON."),
):
    """Set or update the Jira base URL (include the context path)."""
    url = url.rstrip("/")
    cfg = config.load()
    try:
        d = auth.check_supported(
            url,
            deployment=cfg.deployment if cfg else None,
            api_version=cfg.api_version if cfg else None,
        )
    except auth.UnsupportedDeployment as e:
        _handle_expired(e, json_out=json_out)
        return
    config.set_value(config.KEY_BASE_URL, url)
    if json_out:
        _print_config_state_json()
        return
    console.print(f"[green]Saved[/] {config.KEY_BASE_URL} → {config.env_path()}")
    console.print(f"  [dim]deployment[/]  = {d.describe()}")


@config_app.command("deployment")
def config_deployment(
    value: str = typer.Argument(
        ...,
        help="server, cloud, or auto (detect from the base URL).",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit new state as JSON."),
):
    """Pin which Jira dialect to speak, overriding hostname detection.

    Only needed when the hostname misleads: a Cloud tenant behind a vanity
    domain, or a Server/DC instance on something that looks like Cloud.
    """
    raw = value.strip().lower()
    try:
        normalized = dialect_mod.normalize_deployment(raw)
    except dialect_mod.DialectError as e:
        _fail("invalid_input", str(e), json_out=json_out)
    cfg = config.load()
    if cfg:
        try:
            auth.check_supported(
                cfg.base_url,
                deployment=normalized,
                api_version=cfg.api_version,
            )
        except auth.UnsupportedDeployment as e:
            _handle_expired(e, json_out=json_out)
            return
    config.set_value(config.KEY_DEPLOYMENT, normalized or "")
    if json_out:
        _print_config_state_json()
        return
    if normalized:
        console.print(f"[green]Saved[/] {config.KEY_DEPLOYMENT} = {normalized}")
    else:
        console.print(
            f"[yellow]Cleared[/] {config.KEY_DEPLOYMENT} — detecting from the base URL"
        )


@config_app.command("project")
def config_project(
    key: str = typer.Argument(
        ..., help="Default project key (e.g. PROJ). Empty string clears."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit new state as JSON."),
):
    """Set the default Jira project key used to scope `list`/`search`."""
    config.set_value(config.KEY_PROJECT, key.strip())
    if json_out:
        _print_config_state_json()
        return
    if key.strip():
        console.print(f"[green]Saved[/] {config.KEY_PROJECT} = {key.strip()}")
    else:
        console.print(f"[yellow]Cleared[/] {config.KEY_PROJECT}")


@config_app.command("show")
def config_show(
    json_out: bool = typer.Option(False, "--json", help="Emit JSON to stdout."),
):
    """Show .env contents (PAT masked)."""
    if json_out:
        _print_config_state_json()
        return
    state = _config_state()
    t = Table(show_header=False, box=None)
    t.add_row("mode", state["mode"])
    t.add_row("env_file", state["env_file"])
    t.add_row("base_url", state["base_url"] or "(missing)")
    t.add_row(
        "deployment",
        f"{state['deployment']} (REST v{state['api_version']})"
        if state["deployment"]
        else "(unknown)",
    )
    t.add_row("token", "set" if state["pat_set"] else "(missing)")
    if state["deployment"] == dialect_mod.CLOUD:
        t.add_row("email", state["email"] or "(missing)")
    t.add_row("project", state["project"] or "(unscoped)")
    t.add_row("auth_method", state["auth_method"] or "(unset)")
    t.add_row("audit_log", state["audit_log"])
    console.print(t)


def _valid_method(
    method: str | None, d: dialect_mod.Dialect | None = None
) -> str | None:
    """Validate an auth method, against the deployment when we know it.

    `sso`/`pat` are Server/DC and `token` is Cloud, so accepting a method
    that can't work on this deployment only defers the failure to a
    confusing 401.
    """
    if method is None:
        return None
    m = method.strip().lower()
    allowed = auth.methods_for(d) if d else config.AUTH_METHODS
    if m not in allowed:
        hint = f" on {d.deployment}" if d else ""
        raise typer.BadParameter(
            f"Unknown or unsupported auth method {method!r}{hint}. Use "
            + " or ".join(allowed)
            + "."
        )
    return m


def _require_base_url(*, json_out: bool = False) -> str:
    cfg = config.load()
    if cfg is None:
        _fail(
            "not_configured",
            "Not configured — no Jira base URL is set.",
            json_out=json_out,
            fix="jtr init <ticket-url>   (or jtr config base-url <url>)",
        )
    return cfg.base_url


@auth_app.command("pat")
def auth_pat(
    pat: str | None = typer.Option(
        None, "--pat", help="Token value. If omitted, prompted (hidden)."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON to stdout."),
):
    """Store a Personal Access Token in .env and verify it works."""
    result = _do_pat(_require_base_url(json_out=json_out), pat, json_out=json_out)
    if json_out:
        _print_config_state_json(**result)


def _do_pat(base: str, pat: str | None, *, json_out: bool = False) -> dict:
    """Prompt for / store / verify a PAT. Exits non-zero on failure."""
    if pat is None:
        if json_out:
            _fail(
                "input_required",
                "No token supplied and --json cannot prompt for one.",
                json_out=True,
                fix="jtr auth pat --pat <token> --json",
            )
        pat = typer.prompt("Personal Access Token", hide_input=True).strip()
    if not pat:
        _fail("input_required", "Empty token.", json_out=json_out)

    if not json_out:
        console.print("Verifying token...")
    try:
        creds = auth.set_pat(base, pat)
        me = auth.verify(creds)
    except auth.GatewayIntercepted as e:
        # Jira never saw the token — keep it; the problem is the SSO gateway.
        _fail(
            "not_authenticated",
            f"Token stored but not verified: {_first_line(e)}",
            json_out=json_out,
            fix="jtr auth sso",
        )
    except auth.SessionExpired as e:
        # Bad token — wipe so we don't keep a dud around.
        config.set_value(config.KEY_PAT, "")
        _fail(
            "not_authenticated",
            f"Token rejected: {_first_line(e)}",
            json_out=json_out,
            fix="jtr auth pat",
        )
    except Exception as e:
        _fail(
            "verification_failed",
            f"Verification failed: {e}",
            json_out=json_out,
        )

    name = me.get("displayName") or me.get("name") or "?"
    auth.remember_method("pat")
    if not json_out:
        console.print(f"[green]PAT stored in .env and verified as[/] [bold]{name}[/]")
    return {"authenticated": True, "auth_method": "pat", "user": name}


_TOKEN_URL = "https://id.atlassian.com/manage-profile/security/api-tokens"


@auth_app.command("token")
def auth_token(
    email: str | None = typer.Option(
        None, "--email", help="Atlassian account email. Prompted if omitted."
    ),
    token: str | None = typer.Option(
        None, "--token", help="API token value. If omitted, prompted (hidden)."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON to stdout."),
):
    """Store Jira Cloud credentials (account email + API token) and verify.

    Create the token at https://id.atlassian.com/manage-profile/security/api-tokens.
    """
    result = _do_token(
        _require_base_url(json_out=json_out), email, token, json_out=json_out
    )
    if json_out:
        _print_config_state_json(**result)


def _do_token(
    base: str,
    email: str | None,
    token: str | None,
    *,
    json_out: bool = False,
) -> dict:
    """Prompt for / store / verify Cloud Basic-auth credentials."""
    if email is None or token is None:
        if json_out:
            _fail(
                "input_required",
                "Cloud auth needs an email and token, and --json cannot prompt.",
                json_out=True,
                fix="jtr auth token --email <you@example.com> --token <token> --json",
            )
        if not json_out and token is None:
            console.print(f"[dim]Create an API token at[/] {_TOKEN_URL}")
        if email is None:
            email = typer.prompt("Atlassian account email").strip()
        if token is None:
            token = typer.prompt("API token", hide_input=True).strip()
    email = (email or "").strip()
    token = (token or "").strip()
    if not email or not token:
        _fail(
            "input_required",
            "Both an account email and an API token are required.",
            json_out=json_out,
            fix="jtr auth token --email <you@example.com> --token <token>",
        )

    if not json_out:
        console.print("Verifying API token...")
    try:
        creds = auth.set_api_token(base, email, token)
        me = auth.verify(creds)
    except auth.SessionExpired as e:
        # Wipe the secret but keep the email — a rejected token is far more
        # often a bad token than a mistyped address, and retyping it is noise.
        config.set_value(config.KEY_PAT, "")
        _fail(
            "not_authenticated",
            f"API token rejected: {_first_line(e)}",
            json_out=json_out,
            fix=f"Check the email, and create a fresh token at {_TOKEN_URL}",
        )
    except Exception as e:
        _fail(
            "verification_failed",
            f"Verification failed: {e}",
            json_out=json_out,
        )

    name = me.get("displayName") or email
    auth.remember_method("token")
    if not json_out:
        console.print(
            f"[green]API token stored in .env and verified as[/] [bold]{name}[/]"
        )
    return {"authenticated": True, "auth_method": "token", "user": name}


def _first_line(e: Exception) -> str:
    """Errors carry a `\\nFix:` tail for humans; JSON callers get `fix` instead."""
    return str(e).partition("\nFix:")[0].strip()


@auth_app.command("sso")
def auth_sso(
    timeout: int = typer.Option(300, help="Seconds to wait for SSO completion."),
    force: bool = typer.Option(
        False, "--force", help="Skip the reuse check; always launch a browser."
    ),
    browser: str | None = typer.Option(
        None, "--browser", help="Browser channel: msedge, chrome or chromium."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON to stdout."),
):
    """Browser-driven SSO; saves cookies to the active config dir."""
    result = _do_sso(
        _require_base_url(json_out=json_out),
        timeout=timeout,
        force=force,
        channel=browser,
        json_out=json_out,
    )
    if json_out:
        _print_config_state_json(**result)


def _do_sso(
    base: str,
    *,
    timeout: int,
    force: bool,
    channel: str | None = None,
    json_out: bool = False,
) -> dict:
    if not json_out:
        console.print(f"Checking SSO session for [bold]{base}[/]...")
    try:
        state = auth.sso_login(base, timeout_s=timeout, force=force, channel=channel)
    except auth.UnsupportedDeployment as e:
        # Not a failed login — SSO cannot work here at all, so "retry with
        # --force" would be actively misleading advice.
        _handle_expired(e, json_out=json_out)
        raise
    except Exception as e:
        _fail(
            "sso_failed",
            f"SSO failed: {_first_line(e)}",
            json_out=json_out,
            fix="jtr auth sso --force",
        )
    auth.remember_method("sso")
    if not json_out:
        console.print(
            f"[green]Ready.[/] {len(state.cookies)} cookies → {config.session_path()}"
        )
    return {
        "authenticated": True,
        "auth_method": "sso",
        "cookies": len(state.cookies),
    }


@auth_app.callback()
def auth_default(
    ctx: typer.Context,
    method: str | None = typer.Option(
        None, "--method", help="Set the saved method (sso|pat|token) and use it now."
    ),
    timeout: int = typer.Option(300, help="Seconds to wait for SSO completion."),
    force: bool = typer.Option(
        False, "--force", help="SSO only: always launch a browser."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON to stdout."),
):
    """Authenticate using the method saved by `jtr init`.

    `jtr auth` with no subcommand re-runs whichever method this config was
    set up with, so there's nothing to remember. `jtr auth sso` / `jtr auth
    pat` still work and update the saved method.
    """
    if ctx.invoked_subcommand is not None:
        return
    base = _require_base_url(json_out=json_out)
    d = _current_dialect()
    chosen = _valid_method(method, d) or auth.resolve_method(config.load())
    # A Cloud config with nothing saved has exactly one possible answer, so
    # asking the user to pick one would be theatre.
    if chosen is None and d and d.is_cloud:
        chosen = "token"
    if chosen is None:
        _fail(
            "no_auth_method",
            "No auth method configured.",
            json_out=json_out,
            fix="jtr auth sso   (or jtr auth pat) — the choice is remembered",
        )
    if method:
        auth.remember_method(chosen)
    if not json_out:
        console.print(f"[dim]Auth method:[/] {chosen}")
    if chosen == "sso":
        result = _do_sso(base, timeout=timeout, force=force, json_out=json_out)
    elif chosen == "token":
        result = _do_token(base, None, None, json_out=json_out)
    else:
        result = _do_pat(base, None, json_out=json_out)
    if json_out:
        _print_config_state_json(**result)


@auth_app.command("status")
def auth_status(
    json_out: bool = typer.Option(False, "--json", help="Emit JSON to stdout."),
):
    """Show what credentials are configured."""
    s = auth.status()
    if json_out:
        views.print_json(s)
        return
    t = Table(show_header=False, box=None)
    for k, v in s.items():
        t.add_row(k, str(v))
    console.print(t)


@auth_app.command("logout")
def auth_logout(
    cookies_only: bool = typer.Option(
        False, "--cookies", help="Only delete the cookie file (keep PAT)."
    ),
    pat_only: bool = typer.Option(
        False, "--pat", help="Only clear the PAT (keep cookies)."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON to stdout."),
):
    """Clear PAT from .env and delete the cookie file.

    With no flag, clears both. Use `--cookies` to refresh SSO without
    losing your PAT, or `--pat` to drop the token while keeping cookies.
    Clearing cookies also discards the saved browser profile, so the next
    `jtr auth sso` asks the identity provider to log in again.
    """
    if cookies_only and pat_only:
        cookies_only = pat_only = False  # both set == clear all
    pat = not cookies_only
    cookies = not pat_only
    removed = auth.clear(pat=pat, cookies=cookies)
    if json_out:
        views.print_json({"cleared": removed})
        return
    bits = [k for k, v in removed.items() if v]
    if bits:
        console.print(f"[green]Cleared:[/] {', '.join(bits)}")
    else:
        console.print("[yellow]Nothing to clear.[/]")


_TICKET_URL_RE = re.compile(
    r"^(?P<base>https?://[^/]+(?:/[^?#]*?)?)"
    r"/browse/(?P<project>[A-Za-z][A-Za-z0-9_]*)-\d+/?"
    r"(?:[?#].*)?$"
)


def _parse_ticket_url(url: str) -> tuple[str, str]:
    """Return (base_url, project_key) parsed from a Jira /browse/ URL.

    Raises typer.BadParameter on anything that doesn't match.
    """
    m = _TICKET_URL_RE.match(url.strip())
    if not m:
        raise typer.BadParameter(
            "Expected a Jira ticket URL like "
            "https://<host>/<context>/browse/<KEY>-<N>."
        )
    return m.group("base").rstrip("/"), m.group("project").upper()


def _parse_jira_url(url: str) -> tuple[str, str]:
    """Split a ticket URL into (base, project); accept a bare base URL too.

    A ticket URL gives us both values. A plain `https://host/jira` gives
    only the base, and the project key comes from `--project` or the
    prompt — better than rejecting a perfectly usable URL.
    """
    url = url.strip()
    m = _TICKET_URL_RE.match(url)
    if m:
        return m.group("base").rstrip("/"), m.group("project").upper()
    if re.match(r"^https?://[^/\s]+", url):
        return url.rstrip("/"), ""
    raise typer.BadParameter(
        "Expected a Jira ticket URL like "
        "https://<host>/<context>/browse/<KEY>-<N>, or a base URL like "
        "https://<host>/<context>."
    )


@app.command("init")
def cmd_init(
    ticket_url: str | None = typer.Argument(
        None,
        help="Jira ticket URL (or base URL); base URL and project derived from it.",
    ),
    ticket: str | None = typer.Option(
        None, "--ticket", help="Same as the positional URL, named."
    ),
    base_url_opt: str | None = typer.Option(
        None, "--base-url", help="Jira base URL, including the context path."
    ),
    project_opt: str | None = typer.Option(
        None, "--project", help="Default project key (e.g. PROJ)."
    ),
    auth_method: str | None = typer.Option(
        None,
        "--auth",
        help="How to authenticate: sso or pat (Server/DC), token (Cloud).",
    ),
    pat: str | None = typer.Option(
        None, "--pat", help="PAT value; implies --auth pat. Prompted if omitted."
    ),
    email: str | None = typer.Option(
        None, "--email", help="Cloud only: Atlassian account email for the API token."
    ),
    token: str | None = typer.Option(
        None, "--token", help="Cloud API token; implies --auth token."
    ),
    deployment: str | None = typer.Option(
        None,
        "--deployment",
        help="server | cloud | auto. Overrides detection from the base URL.",
    ),
    browser: str | None = typer.Option(
        None,
        "--browser",
        help="Browser for SSO (msedge|chrome|chromium). Saved globally.",
    ),
    timeout: int = typer.Option(300, "--timeout", help="Seconds to wait for SSO."),
    no_auth: bool = typer.Option(
        False, "--no-auth", help="Only scaffold; don't authenticate."
    ),
    force: bool = typer.Option(
        False, "--force", help="Update an existing ./.jtr/ instead of failing."
    ),
    no_gitignore: bool = typer.Option(
        False, "--no-gitignore", help="Don't append `.jtr/` to ./.gitignore."
    ),
    no_skills: bool = typer.Option(
        False, "--no-skills", help="Don't install the bundled Claude Code skill."
    ),
    bare: bool = typer.Option(
        False, "--bare", help="Config only: implies --no-gitignore --no-skills."
    ),
    dir_opt: str | None = typer.Option(
        None,
        "--dir",
        help="Directory to initialize instead of the cwd. Config lands in <dir>/.jtr/.",
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit the resulting config as JSON. Never prompts."
    ),
):
    """Set up jtr in the cwd: project-local config, settings, and auth.

    Everything can come from one ticket URL, from explicit flags, or from
    a prompt — and `--auth` finishes the job, so a full setup is one
    command:

        jtr init --ticket <url> --auth sso

    With `--json` the resulting config is printed as a single object and
    nothing is ever prompted for, so a third-party tool can drive setup
    and read back what it got in one round trip.
    """
    no_gitignore = no_gitignore or bare
    no_skills = no_skills or bare
    source = ticket or ticket_url
    parsed_base, parsed_project = _parse_jira_url(source) if source else ("", "")

    root = Path(dir_opt).expanduser().resolve() if dir_opt else Path.cwd()
    # $JTR_CONFIG_DIR names the config dir outright; --dir names the project
    # root that contains it. An explicit --dir wins.
    env_dir = os.environ.get(config.KEY_CONFIG_DIR, "").strip()
    explicit_target = (
        Path(env_dir).expanduser().resolve() if env_dir and not dir_opt else None
    )
    target_dir = explicit_target or (root / ".jtr")

    # Pin the config dir before reading anything, so `--dir` and
    # $JTR_CONFIG_DIR steer the .env we inherit from as well as the one we
    # write to.
    config.use_config_dir(target_dir)

    # Existing values are only inherited when this folder is already a jtr
    # project; a fresh init must not silently adopt the global base URL.
    existing = config.load() if target_dir.is_dir() else None

    base_url = (
        (base_url_opt.rstrip("/") if base_url_opt else "")
        or parsed_base
        or (existing.base_url if existing else "")
    )
    project = (
        (project_opt.strip().upper() if project_opt else "")
        or parsed_project
        or (existing.project if existing and existing.project else "")
    )
    email = (email or "").strip() or (existing.email if existing else "") or ""

    # --json is a promise that stdout is parseable; a prompt would break it.
    interactive = sys.stdin.isatty() and not json_out
    if not base_url:
        if not interactive:
            _fail(
                "not_configured",
                "No base URL. Pass a ticket URL, --ticket <url>, or --base-url <url>.",
                json_out=json_out,
                fix="jtr init <ticket-url>",
            )
        base_url = _parse_jira_url(
            typer.prompt("Jira ticket or base URL")
        )[0]

    # Resolve the dialect before anything asks about auth: which credentials
    # even make sense is a function of the deployment.
    deployment_setting = deployment if deployment is not None else (
        existing.deployment if existing else None
    )
    try:
        deployment_setting = dialect_mod.normalize_deployment(deployment_setting)
        d = auth.check_supported(
            base_url,
            deployment=deployment_setting,
            api_version=existing.api_version if existing else None,
        )
    except dialect_mod.DialectError as e:
        _fail("invalid_input", str(e), json_out=json_out)
    except auth.UnsupportedDeployment as e:
        _fail(
            "unsupported_deployment",
            _first_line(e),
            json_out=json_out,
            fix=str(e).partition("\nFix:")[2].strip() or None,
            exit_code=2,
        )

    default_method = "token" if d.is_cloud else "pat"
    method = _valid_method(auth_method, d) or (
        default_method if (pat or token) else ""
    )

    if not project and interactive:
        project = typer.prompt(
            "Default project key (blank for none)", default="", show_default=False
        ).strip().upper()
    if not method and not no_auth:
        saved = auth.resolve_method(existing)
        allowed = auth.methods_for(d)
        if saved not in allowed:
            saved = None
        if len(allowed) == 1:
            # Cloud has exactly one workable method; nothing to ask.
            method = allowed[0]
        elif interactive:
            method = _valid_method(
                typer.prompt(
                    f"Auth method ({'/'.join(allowed)})", default=saved or allowed[0]
                ),
                d,
            )
        else:
            method = saved or ""

    try:
        target, touched, installed_skills = config.init_project(
            root,
            update_gitignore=not no_gitignore,
            base_url=base_url,
            project=project,
            auth_method=method,
            deployment=deployment_setting or "",
            email=email,
            force=force,
            install_skills=not no_skills,
            target=explicit_target,
        )
    except config.InitError as e:
        _fail(
            "already_initialized",
            f"{e} Already initialized?",
            json_out=json_out,
            fix="jtr init --force   (updates the existing config in place)",
        )

    if browser:
        config.set_value(config.KEY_BROWSER_CHANNEL, browser, scope="global")

    scaffold = {
        "gitignore_updated": touched,
        "skills_installed": installed_skills,
    }

    if not json_out:
        console.print(f"[green]Initialized[/] project-local jtr config at {target}/")
        console.print(f"  [dim]base_url[/]    = {base_url}")
        console.print(
            f"  [dim]deployment[/]  = {d.describe()}"
            + ("" if deployment_setting else "  [dim](detected)[/]")
        )
        console.print(f"  [dim]project[/]     = {project or '(unscoped)'}")
        console.print(f"  [dim]auth_method[/] = {method or '(unset)'}")
        if browser:
            console.print(f"  [dim]browser[/]     = {browser}  [dim](global)[/]")
        if touched:
            console.print("[green]Added[/] `.jtr/` to ./.gitignore")
        if installed_skills:
            skills_str = ", ".join(f"/{s}" for s in installed_skills)
            noun = "skill" if len(installed_skills) == 1 else "skills"
            console.print(f"[green]Installed[/] Claude Code {noun}: {skills_str}")

    if no_auth or not method:
        if json_out:
            _print_config_state_json(**scaffold, authenticated=False)
            return
        hint = "jtr auth token" if d.is_cloud else "jtr auth sso | pat"
        console.print(f"[dim]Next:[/] jtr auth   [dim](or {hint})[/]")
        return
    # The scaffold is valid even if auth fails, so this is deliberately not
    # rolled back — `jtr auth` retries just this step.
    if not json_out:
        console.print()
    if method == "sso":
        result = _do_sso(
            base_url, timeout=timeout, force=False, channel=browser, json_out=json_out
        )
    elif method == "token":
        result = _do_token(
            base_url, email or None, token, json_out=json_out
        )
    else:
        result = _do_pat(base_url, pat, json_out=json_out)
    if json_out:
        _print_config_state_json(**scaffold, **result)


@app.command("reset")
def cmd_reset(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON to stdout."),
):
    """Delete all jtr-managed data in the active config dir.

    Clears .env, cookies, and audit log. In project-local mode, also
    removes the ./.jtr/ folder so subsequent calls use global config.
    """
    target = config.config_dir()
    local = config.is_project_local()
    files = sorted(p for p in target.glob("*") if p.is_file()) if target.exists() else []
    if not files and not (local and target.exists()):
        if json_out:
            views.print_json(
                {"config_dir": str(target), "removed": [], "folder_removed": False}
            )
            return
        console.print("[yellow]Nothing to reset.[/]")
        return
    if not json_out:
        console.print(f"[bold red]Will delete:[/] {target}/")
        for f in files:
            console.print(f"  - {f.name}")
        if local:
            console.print(f"  - {target.name}/  [dim](the folder itself)[/]")
    if not yes:
        if json_out:
            _fail(
                "confirmation_required",
                "Refusing to delete config without confirmation under --json.",
                json_out=True,
                fix="jtr reset --yes --json",
            )
        typer.confirm("Proceed?", abort=True)
    for f in files:
        f.unlink()
    if local:
        target.rmdir()
    if json_out:
        views.print_json({
            "config_dir": str(target),
            "removed": [f.name for f in files],
            "folder_removed": local,
        })
        return
    if local:
        console.print(f"[green]Removed[/] {target}/ — back to global config.")
    else:
        console.print(f"[green]Cleared[/] {target}/")


@app.command("whoami")
def whoami(
    json_out: bool = typer.Option(False, "--json", help="Emit JSON to stdout."),
):
    """Print the currently authenticated user."""
    try:
        _ensure_creds()
        with JiraClient.from_session() as jc:
            me = jc.myself()
    except auth.SessionExpired as e:
        _handle_expired(e, json_out=json_out)
        return
    except JiraError as e:
        _handle_jira_error(e, json_out=json_out)
        return
    payload = {
        "name": me.get("name", ""),
        "display_name": me.get("displayName", ""),
        # Cloud sends only accountId; Server/DC sends only key.
        "key": me.get("key") or me.get("accountId", ""),
        "account_id": me.get("accountId", ""),
        "email": me.get("emailAddress", ""),
    }
    if json_out:
        views.print_json(payload)
        return
    console.print(
        f"[bold]{payload['display_name'] or payload['name'] or '?'}[/]"
        f"  <{payload['email']}>  ({payload['key']})"
    )


def _resolve_project(project: str | None, all_projects: bool) -> str | None:
    """`--project X` wins, then `--all` unscopes, else JTR_PROJECT."""
    if project:
        return project.strip() or None
    if all_projects:
        return None
    cfg = config.load()
    return cfg.project if cfg and cfg.project else None


def _scope_jql(jql: str, project: str | None) -> str:
    if not project:
        return jql
    if "project" in jql.lower():
        return jql
    return f"({jql}) AND project = {project}" if jql.strip() else f"project = {project}"


def _run_search(
    jql: str,
    limit: int,
    json_out: bool = False,
    start_at: int = 0,
    cursor: str | None = None,
) -> None:
    d = _current_dialect()
    # Offsets don't exist on Cloud's search endpoint, so silently ignoring
    # --start-at would hand back page 1 while the caller believes it paged.
    if d and d.is_cloud and start_at:
        _fail(
            "unsupported_option",
            "--start-at doesn't work on Jira Cloud: its search endpoint pages "
            "by cursor, not by offset.",
            json_out=json_out,
            fix="Use --cursor <token> with the next_page_token from the last page.",
        )
    if d and not d.is_cloud and cursor:
        _fail(
            "unsupported_option",
            "--cursor is Jira Cloud only; Server/DC pages by offset.",
            json_out=json_out,
            fix="Use --start-at <n>.",
        )
    try:
        _ensure_creds()
        with JiraClient.from_session() as jc:
            page = jc.search(jql, limit=limit, start_at=start_at, cursor=cursor)
    except auth.SessionExpired as e:
        _handle_expired(e, json_out=json_out)
        return
    except JiraError as e:
        _handle_jira_error(e, json_out=json_out)
        return
    if json_out:
        views.print_ticket_table_json(page, jql)
        return
    console.print(f"[dim]JQL:[/] {jql}")
    views.render_ticket_table(console, page)


@list_app.command("mine")
def list_mine(
    project: str | None = typer.Option(None, "--project", help="Override scope."),
    all_projects: bool = typer.Option(False, "--all", help="Ignore JTR_PROJECT."),
    limit: int = typer.Option(50, "--limit"),
    start_at: int = typer.Option(
        0, "--start-at", help="Server/DC: index of the first result (paging)."
    ),
    cursor: str | None = typer.Option(
        None, "--cursor", help="Cloud: next_page_token from the previous page."
    ),
    include_done: bool = typer.Option(
        False, "--all-statuses", help="Include Done/Closed tickets."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON to stdout."),
):
    """Tickets assigned to me (open by default; scoped by JTR_PROJECT)."""
    base = "assignee = currentUser()"
    if not include_done:
        base += " AND statusCategory != Done"
    jql = _scope_jql(base, _resolve_project(project, all_projects))
    jql += " ORDER BY updated DESC"
    _run_search(jql, limit, json_out=json_out, start_at=start_at, cursor=cursor)


@app.command("search")
def search(
    jql: str = typer.Argument(..., help="JQL query."),
    project: str | None = typer.Option(None, "--project", help="Override scope."),
    all_projects: bool = typer.Option(False, "--all", help="Ignore JTR_PROJECT."),
    limit: int = typer.Option(50, "--limit"),
    start_at: int = typer.Option(
        0, "--start-at", help="Server/DC: index of the first result (paging)."
    ),
    cursor: str | None = typer.Option(
        None, "--cursor", help="Cloud: next_page_token from the previous page."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON to stdout."),
):
    """Run a JQL search. Auto-scoped to JTR_PROJECT unless JQL says project."""
    final = _scope_jql(jql, _resolve_project(project, all_projects))
    _run_search(final, limit, json_out=json_out, start_at=start_at, cursor=cursor)


@app.command("projects")
def cmd_projects(
    json_out: bool = typer.Option(False, "--json", help="Emit JSON to stdout."),
):
    """List the projects you can see — enough to build a project picker."""
    try:
        _ensure_creds()
        with JiraClient.from_session() as jc:
            projects = jc.projects()
    except auth.SessionExpired as e:
        _handle_expired(e, json_out=json_out)
        return
    except JiraError as e:
        _handle_jira_error(e, json_out=json_out)
        return
    projects.sort(key=lambda p: p.key)
    if json_out:
        views.print_projects_json(projects)
        return
    views.render_project_table(console, projects)


@app.command("view")
def view(
    key: str = typer.Argument(..., help="Ticket key (e.g. PROJ-123)."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON to stdout."),
):
    """Show a ticket: header, fields, comments."""
    try:
        _ensure_creds()
        with JiraClient.from_session() as jc:
            ticket = jc.get_issue(key)
            comments = jc.get_comments(key)
    except auth.SessionExpired as e:
        _handle_expired(e, json_out=json_out)
        return
    except JiraError as e:
        _handle_jira_error(e, json_out=json_out)
        return
    if json_out:
        views.print_ticket_detail_json(ticket, comments)
        return
    views.render_ticket_detail(console, ticket, comments)


# -- Writes (M2) -----------------------------------------------------------

def _write_console(json_out: bool) -> Console:
    """Under --json the preview and prompt go to stderr, keeping stdout clean."""
    return err if json_out else console


def _applied(row: dict) -> dict:
    """The audit row is already the right shape for a `--json` write result."""
    return {**row, "changed": True}


def _unchanged(action: str, key: str, **fields) -> dict:
    return {"action": action, "key": key, "ok": True, "changed": False, **fields}


def _run_write(
    action: str,
    key: str,
    fn,
    *,
    json_out: bool = False,
    assume_yes: bool = False,
    confirm_required: bool = True,
) -> None:
    """Wrap a write closure with shared error handling.

    The closure must do its own preview + safety.confirm_apply call, and
    returns the payload to emit under `--json`.
    """
    if json_out and confirm_required and not assume_yes and not sys.stdin.isatty():
        _fail(
            "confirmation_required",
            "A write needs confirmation, and --json has no terminal to ask on.",
            json_out=True,
            fix=f"jtr {action.split(':')[0]} ... --yes --json",
        )
    payload: dict | None = None
    try:
        _ensure_creds()
        with JiraClient.from_session() as jc:
            payload = fn(jc)
    except auth.SessionExpired as e:
        _handle_expired(e, json_out=json_out)
    except safety.WriteCancelled:
        # Already announced; treat as a clean no-op exit.
        if json_out:
            views.print_json(
                {"action": action, "key": key, "ok": False, "cancelled": True}
            )
        raise typer.Exit(code=0) from None
    except JiraError as e:
        _handle_jira_error(e, json_out=json_out)
    if json_out and payload is not None:
        views.print_json(payload)


@app.command("comment")
def cmd_comment(
    key: str = typer.Argument(..., help="Ticket key."),
    text: str = typer.Argument(..., help="Comment body."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    json_out: bool = typer.Option(
        False, "--json", help="Emit the audit row as JSON to stdout."
    ),
):
    """Add a comment to a ticket (preview → confirm → post)."""
    body = text.strip()
    if not body:
        _fail("invalid_input", "Empty comment.", json_out=json_out)

    def go(jc: JiraClient) -> dict:
        preview = Panel(
            Text(body), title="Comment to add", border_style="green", expand=True
        )
        row = safety.confirm_apply(
            _write_console(json_out),
            action="comment",
            key=key,
            preview=preview,
            apply_fn=lambda: jc.add_comment(key, body).id,
            after={"body": body},
            assume_yes=yes,
            quiet=json_out and yes,
        )
        if not json_out:
            console.print(f"[green]Comment added on[/] {key}")
        return _applied(row)

    _run_write("comment", key, go, json_out=json_out, assume_yes=yes)


# Field name → (current-value extractor from Ticket, payload builder from raw str)
def _summary_extract(t):
    return t.summary


def _description_extract(t):
    return t.description


def _priority_extract(t):
    return t.priority


def _labels_extract(t):
    return ", ".join(t.labels)


def _fixversions_extract(t):
    return ", ".join(t.fix_versions)


def _csv_list(v: str) -> list[str]:
    return [s.strip() for s in v.split(",") if s.strip()]


EDITABLE_FIELDS: dict[str, tuple] = {
    # alias            extractor              payload builder
    "summary":      (_summary_extract,      lambda v: {"summary": v}),
    "description":  (_description_extract,  lambda v: {"description": v}),
    "priority":     (_priority_extract,     lambda v: {"priority": {"name": v}}),
    "labels":       (_labels_extract,       lambda v: {"labels": _csv_list(v)}),
    "fixVersions":  (_fixversions_extract,  lambda v: {
        "fixVersions": [{"name": s} for s in _csv_list(v)]
    }),
}


@app.command("edit")
def cmd_edit(
    key: str = typer.Argument(..., help="Ticket key."),
    field: str = typer.Argument(
        ...,
        help=f"Field to edit. One of: {', '.join(EDITABLE_FIELDS)}.",
    ),
    value: str = typer.Argument(..., help="New value. CSV for labels/fixVersions."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    json_out: bool = typer.Option(
        False, "--json", help="Emit the audit row as JSON to stdout."
    ),
):
    """Edit a single field on a ticket (preview → confirm → PUT)."""
    if field not in EDITABLE_FIELDS:
        _fail(
            "invalid_input",
            f"Unknown field '{field}'.",
            json_out=json_out,
            fix=f"Supported fields: {', '.join(EDITABLE_FIELDS)}.",
        )

    extract, build = EDITABLE_FIELDS[field]
    payload = build(value)

    def go(jc: JiraClient) -> dict:
        current = jc.get_issue(key)
        old = extract(current)
        if old == value:
            if not json_out:
                console.print(
                    f"[yellow]No change[/] — {field} is already [bold]{value}[/]."
                )
            return _unchanged(f"edit:{field}", key, before={field: old})
        preview = safety.render_field_diff(field, old, value)
        row = safety.confirm_apply(
            _write_console(json_out),
            action=f"edit:{field}",
            key=key,
            preview=preview,
            apply_fn=lambda: jc.edit_issue(key, payload),
            before={field: old},
            after={field: value},
            assume_yes=yes,
            quiet=json_out and yes,
        )
        if not json_out:
            console.print(f"[green]Updated[/] {key}: {field} = {value}")
        return _applied(row)

    _run_write(f"edit:{field}", key, go, json_out=json_out, assume_yes=yes)


@app.command("assign")
def cmd_assign(
    key: str = typer.Argument(..., help="Ticket key."),
    user: str = typer.Argument(
        "",
        help=(
            "Server/DC: username. Cloud: email, display name, or accountId. "
            "Empty or use --unassign to clear."
        ),
    ),
    unassign: bool = typer.Option(False, "--unassign", help="Clear the assignee."),
    account_id: bool = typer.Option(
        False,
        "--account-id",
        help="Cloud: treat the argument as an accountId; skip the user lookup.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    json_out: bool = typer.Option(
        False, "--json", help="Emit the audit row as JSON to stdout."
    ),
):
    """Set or clear the assignee.

    On Cloud the API identifies people by opaque accountId, so an email or
    display name is looked up first; `--account-id` skips that when you
    already hold the id.
    """
    requested: str | None = None if unassign or not user.strip() else user.strip()

    def go(jc: JiraClient) -> dict:
        current = jc.get_issue(key)
        old = current.assignee.short() if current.assignee else ""
        new = requested or ""
        if requested is None or account_id:
            target = requested
        else:
            try:
                target = jc.resolve_assignee(requested)
            except AmbiguousUser as e:
                _fail(
                    "ambiguous_user",
                    str(e),
                    json_out=json_out,
                    fix="Use the exact email, or pass the accountId with --account-id.",
                )
            except UserNotFound as e:
                _fail(
                    "user_not_found",
                    str(e),
                    json_out=json_out,
                    fix="Check the spelling, or pass an accountId with --account-id.",
                )
        # Compare on whatever field this deployment identifies people by —
        # on Cloud the assignee has no `name`, so comparing names would make
        # every assignment look like a change.
        if jc.dialect.user_identifier(current.assignee) == (target or ""):
            if not json_out:
                console.print(
                    f"[yellow]No change[/] — assignee is already "
                    f"[bold]{old or '(unassigned)'}[/]."
                )
            return _unchanged("assign", key, before={"assignee": old or None})
        preview = safety.render_field_diff(
            "Assignee", old or "(unassigned)", new or "(unassigned)"
        )
        row = safety.confirm_apply(
            _write_console(json_out),
            action="assign",
            key=key,
            preview=preview,
            apply_fn=lambda: jc.assign(key, target),
            before={"assignee": old or None},
            after={"assignee": new or None},
            assume_yes=yes,
            quiet=json_out and yes,
        )
        if not json_out:
            console.print(
                f"[green]Assignee updated[/] on {key}: {new or '(unassigned)'}"
            )
        return _applied(row)

    _run_write("assign", key, go, json_out=json_out, assume_yes=yes)


def _labels_diff_preview(old: list[str], new: list[str]):
    return safety.render_field_diff(
        "Labels",
        ", ".join(old) or "(none)",
        ", ".join(new) or "(none)",
    )


@label_app.command("add")
def cmd_label_add(
    key: str = typer.Argument(..., help="Ticket key."),
    name: str = typer.Argument(..., help="Label to add."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    json_out: bool = typer.Option(
        False, "--json", help="Emit the audit row as JSON to stdout."
    ),
):
    """Add a label if it isn't already present."""
    label = name.strip()
    if not label:
        _fail("invalid_input", "Empty label.", json_out=json_out)

    def go(jc: JiraClient) -> dict:
        current = jc.get_issue(key)
        if label in current.labels:
            if not json_out:
                console.print(
                    f"[yellow]No change[/] — {key} already has label "
                    f"[bold]{label}[/]."
                )
            return _unchanged("label:add", key, before={"labels": current.labels})
        new_labels = [*current.labels, label]
        row = safety.confirm_apply(
            _write_console(json_out),
            action="label:add",
            key=key,
            preview=_labels_diff_preview(current.labels, new_labels),
            apply_fn=lambda: jc.update_labels(key, add=[label]),
            before={"labels": current.labels},
            after={"labels": new_labels},
            assume_yes=yes,
            quiet=json_out and yes,
        )
        if not json_out:
            console.print(f"[green]Added label[/] {label} → {key}")
        return _applied(row)

    _run_write("label:add", key, go, json_out=json_out, assume_yes=yes)


@label_app.command("remove")
def cmd_label_remove(
    key: str = typer.Argument(..., help="Ticket key."),
    name: str = typer.Argument(..., help="Label to remove."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    json_out: bool = typer.Option(
        False, "--json", help="Emit the audit row as JSON to stdout."
    ),
):
    """Remove a label if it's present."""
    label = name.strip()
    if not label:
        _fail("invalid_input", "Empty label.", json_out=json_out)

    def go(jc: JiraClient) -> dict:
        current = jc.get_issue(key)
        if label not in current.labels:
            if not json_out:
                console.print(
                    f"[yellow]No change[/] — {key} doesn't have label "
                    f"[bold]{label}[/]."
                )
            return _unchanged("label:remove", key, before={"labels": current.labels})
        new_labels = [x for x in current.labels if x != label]
        row = safety.confirm_apply(
            _write_console(json_out),
            action="label:remove",
            key=key,
            preview=_labels_diff_preview(current.labels, new_labels),
            apply_fn=lambda: jc.update_labels(key, remove=[label]),
            before={"labels": current.labels},
            after={"labels": new_labels},
            assume_yes=yes,
            quiet=json_out and yes,
        )
        if not json_out:
            console.print(f"[green]Removed label[/] {label} from {key}")
        return _applied(row)

    _run_write("label:remove", key, go, json_out=json_out, assume_yes=yes)


def _match_transition(transitions, target: str):
    """Case-insensitive match on transition name, then on to_status."""
    t = target.strip().lower()
    if not t:
        return None, []
    exact = [
        x for x in transitions
        if x.name.lower() == t or x.to_status.lower() == t
    ]
    if exact:
        return (exact[0] if len(exact) == 1 else None), exact
    partial = [
        x for x in transitions
        if t in x.name.lower() or t in x.to_status.lower()
    ]
    if len(partial) == 1:
        return partial[0], partial
    return None, partial


@app.command("transition")
def cmd_transition(
    key: str = typer.Argument(..., help="Ticket key."),
    status: str = typer.Argument(
        "",
        help="Target status or transition name. Omit to list available transitions.",
    ),
    comment: str | None = typer.Option(
        None, "--comment", "-m", help="Optional comment to add with the transition."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    json_out: bool = typer.Option(
        False, "--json", help="Emit the audit row (or the transition list) as JSON."
    ),
):
    """Move a ticket through a workflow transition.

    Without a status argument, prints the available transitions.
    """

    def go(jc: JiraClient) -> dict | None:
        transitions = jc.get_transitions(key)
        if not transitions:
            if json_out:
                return {"key": key, "transitions": []}
            console.print(f"[yellow]No transitions available on[/] {key}.")
            return None

        if not status.strip():
            if json_out:
                return {
                    "key": key,
                    "transitions": [
                        {"id": tr.id, "name": tr.name, "to_status": tr.to_status}
                        for tr in transitions
                    ],
                }
            t = Table(title=f"Transitions for {key}", show_header=True)
            t.add_column("Name", style="bold")
            t.add_column("→ Status")
            for tr in transitions:
                t.add_row(tr.name, tr.to_status)
            console.print(t)
            return None

        match, candidates = _match_transition(transitions, status)
        if match is None:
            names = ", ".join(f"'{x.name}'" for x in (candidates or transitions))
            if not candidates:
                _fail(
                    "no_transition_match",
                    f"No transition matches '{status}'.",
                    json_out=json_out,
                    fix=f"Available: {names}.",
                )
            _fail(
                "ambiguous_transition",
                f"Ambiguous: '{status}' matches {names}.",
                json_out=json_out,
                fix="Be more specific.",
            )

        current = jc.get_issue(key)
        old = current.status
        preview_text = Text()
        preview_text.append("Status: ", style="bold")
        preview_text.append(old or "—", style="red strike" if old else "dim")
        preview_text.append("  →  ", style="dim")
        preview_text.append(match.to_status or match.name, style="green")
        preview_text.append(f"\n(via transition '{match.name}', id={match.id})",
                            style="dim")
        if comment:
            preview_text.append("\n\nComment: ", style="bold")
            preview_text.append(comment)

        row = safety.confirm_apply(
            _write_console(json_out),
            action="transition",
            key=key,
            preview=preview_text,
            apply_fn=lambda: jc.do_transition(key, match.id, comment=comment),
            before={"status": old},
            after={
                "status": match.to_status or match.name,
                "transition_id": match.id,
                "comment": comment,
            },
            assume_yes=yes,
            quiet=json_out and yes,
        )
        if not json_out:
            console.print(
                f"[green]Transitioned[/] {key} → {match.to_status or match.name}"
            )
        return _applied(row)

    _run_write(
        "transition",
        key,
        go,
        json_out=json_out,
        assume_yes=yes,
        confirm_required=bool(status.strip()),
    )


def main() -> int:
    try:
        app()
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
