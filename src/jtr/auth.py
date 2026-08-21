from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import config
from .browser import Browser
from .dialect import Dialect, DialectError


class SessionExpired(Exception):
    pass


class GatewayIntercepted(SessionExpired):
    """The WebSEAL/SSO gateway answered instead of Jira — cookies are stale.

    Distinct from a plain SessionExpired so callers can tell "your credential
    was rejected" from "the request never reached Jira" (a PAT is still fine).
    """


class UnsupportedDeployment(SessionExpired):
    """The requested combination of deployment, API version and auth can't work.

    Both Cloud and Server/DC are supported, so this is no longer "wrong kind
    of Jira" — it's things like REST v3 against Server/DC (which has no v3)
    or `auth sso` against Cloud (which doesn't accept cookies). Subclasses
    SessionExpired so every command's existing handler catches it; the CLI
    special-cases it for the JSON error code.
    """


# Markers of a WebSEAL login/redirect page served with HTTP 200 + text/html.
_GATEWAY_MARKERS = ("tmpurl", "/tfim/", "WebSEAL")


def gateway_page(body: str) -> bool:
    return any(m in body for m in _GATEWAY_MARKERS)


def dialect_for(cfg: config.Config) -> Dialect:
    """The dialect implied by a config, or a clear error saying why not."""
    try:
        return Dialect.resolve(
            cfg.base_url,
            deployment=cfg.deployment,
            api_version=cfg.api_version,
        )
    except DialectError as e:
        raise UnsupportedDeployment(str(e)) from e


def check_supported(
    base_url: str,
    *,
    deployment: str | None = None,
    api_version: str | None = None,
) -> Dialect:
    """Validate a base URL / override combination before we store or use it.

    Historically this rejected Cloud outright. Cloud is supported now, so
    what's left is catching combinations that cannot work — caught here so
    they surface at `init` rather than as a 404 on the first real command.
    """
    try:
        return Dialect.resolve(
            base_url, deployment=deployment, api_version=api_version
        )
    except DialectError as e:
        raise UnsupportedDeployment(str(e)) from e


@dataclass
class CookieState:
    base_url: str
    cookies: list[dict] = field(default_factory=list)
    captured_at: str = ""


@dataclass
class Credentials:
    base_url: str
    pat: str | None = None
    cookies: list[dict] = field(default_factory=list)
    # Cloud's Basic auth needs the account email as the username half.
    email: str | None = None
    dialect: Dialect | None = None

    def __post_init__(self) -> None:
        if self.dialect is None:
            self.dialect = Dialect.resolve(self.base_url)

    @property
    def has_pat(self) -> bool:
        return bool(self.pat)

    @property
    def has_cookies(self) -> bool:
        return bool(self.cookies)

    @property
    def usable(self) -> bool:
        """Do we hold something that could actually authenticate a request?

        On Cloud a token without an email is only half a Basic credential,
        and cookies are not a credential at all — so "has a PAT set" isn't
        the same question as "can we make a call".
        """
        assert self.dialect is not None
        if self.dialect.is_cloud:
            return bool(self.pat and self.email)
        return self.has_pat or self.has_cookies


def _write_cookies(state: CookieState) -> None:
    p = config.session_path()
    p.write_text(json.dumps(asdict(state), indent=2))
    os.chmod(p, 0o600)


def _read_cookies() -> CookieState | None:
    p = config.session_path()
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    return CookieState(**data)


def load_credentials() -> Credentials:
    """Build credentials from .env (PAT) plus the cookie file (SSO)."""
    cfg = config.load()
    if cfg is None:
        raise SessionExpired(
            f"No {config.KEY_BASE_URL} configured. Edit {config.env_path()} "
            "or run any command to be prompted."
        )
    dialect = dialect_for(cfg)
    cookies = _read_cookies()
    # Cookies only mean anything to a gateway-fronted Server/DC; carrying
    # them to Cloud would be noise on every request.
    cookie_list = (cookies.cookies if cookies else []) if dialect.supports_sso else []
    creds = Credentials(
        base_url=cfg.base_url,
        pat=cfg.pat,
        cookies=cookie_list,
        email=cfg.email,
        dialect=dialect,
    )
    if not creds.usable:
        if dialect.is_cloud:
            missing = "email and API token" if not cfg.pat else "account email"
            raise SessionExpired(
                f"Incomplete Cloud credentials — missing {missing}.\n"
                "Fix: jtr auth token"
            )
        raise SessionExpired(
            f"No credentials. Set {config.KEY_PAT} in {config.env_path()} "
            "or run `jtr auth sso`."
        )
    return creds


def _build_client(creds: Credentials) -> httpx.Client:
    """Build an httpx client carrying every credential we have.

    The WebSEAL gateway demands session cookies just to reach Jira on
    /rest/* paths from off-corp networks; a bare PAT (Bearer) doesn't get
    past it. We therefore always send cookies if we have them, and add the
    Authorization header on top. The header's scheme is the dialect's call:
    Bearer for Server/DC, Basic for Cloud.
    """
    dialect = creds.dialect or Dialect.resolve(creds.base_url)
    headers = {"Accept": "application/json"}
    headers.update(dialect.auth_headers(token=creds.pat, email=creds.email))
    cookies = None
    if creds.has_cookies:
        jar = httpx.Cookies()
        for ck in creds.cookies:
            jar.set(
                name=ck["name"],
                value=ck["value"],
                domain=ck.get("domain", "").lstrip("."),
                path=ck.get("path", "/"),
            )
        cookies = jar
    return httpx.Client(
        base_url=creds.base_url,
        cookies=cookies,
        timeout=30.0,
        follow_redirects=False,
        headers=headers,
    )


def client() -> httpx.Client:
    return _build_client(load_credentials())


def session() -> tuple[httpx.Client, Dialect]:
    """An authenticated client plus the dialect to drive it with."""
    creds = load_credentials()
    assert creds.dialect is not None
    return _build_client(creds), creds.dialect


def status() -> dict:
    cfg = config.load()
    cookies = _read_cookies()
    dialect = None
    if cfg:
        try:
            dialect = dialect_for(cfg)
        except UnsupportedDeployment:
            dialect = None
    return {
        "env_file": str(config.env_path()),
        "base_url": cfg.base_url if cfg else None,
        "deployment": dialect.deployment if dialect else None,
        "api_version": dialect.api_version if dialect else None,
        "auth_method": (cfg.auth_method if cfg else None) or "(unset)",
        "email": (cfg.email if cfg else None) or None,
        "pat": "set" if cfg and cfg.pat else "missing",
        "cookies": len(cookies.cookies) if cookies else 0,
        "cookies_file": str(config.session_path()),
        "cookies_captured_at": cookies.captured_at if cookies else None,
    }


def clear(*, pat: bool = True, cookies: bool = True) -> dict:
    """Remove PAT from .env and/or delete the cookie file."""
    removed = {"pat": False, "cookies": False}
    if pat:
        cfg = config.load()
        if cfg and cfg.pat:
            config.set_value(config.KEY_PAT, "")
            removed["pat"] = True
        # The email is half of a Cloud credential; leaving it behind would
        # make `auth status` claim a login that no longer exists.
        if cfg and cfg.email:
            config.set_value(config.KEY_EMAIL, "")
            removed["email"] = True
    if cookies:
        p = Path(config.session_path())
        if p.exists():
            p.unlink()
            removed["cookies"] = True
        # The browser profile holds the IdP's own session. Leaving it behind
        # would mean `auth logout` doesn't actually log you out — the next
        # `auth sso` would sail past the login form as the same user.
        profile = config.browser_profile_path()
        if profile.exists():
            shutil.rmtree(profile, ignore_errors=True)
            removed["browser_profile"] = True
    return removed


def verify(creds: Credentials) -> dict:
    """Call the deployment's `myself` endpoint and return the identity dict."""
    dialect = creds.dialect or Dialect.resolve(creds.base_url)
    with _build_client(creds) as c:
        r = c.get(dialect.myself_path)
    if r.status_code in (401, 403):
        raise SessionExpired(f"Auth failed: HTTP {r.status_code}")
    if 300 <= r.status_code < 400:
        raise SessionExpired(f"Got redirect HTTP {r.status_code}; auth likely failed")
    ct = (r.headers.get("content-type") or "").lower()
    if r.status_code != 200 or "application/json" not in ct:
        # WebSEAL answers with its own HTML login page (HTTP 200!) when the SSO
        # cookies are gone, so the request never reached Jira at all.
        if gateway_page(r.text or ""):
            raise GatewayIntercepted(
                "Not authenticated — the WebSEAL SSO gateway answered instead "
                "of Jira, so the credential was never checked.\n"
                "Fix: jtr auth sso   (or connect to VPN if you only use a PAT)."
            )
        raise RuntimeError(
            f"Unexpected response: status={r.status_code} ct={ct.split(';')[0]} "
            f"body={r.text[:200]!r}"
        )
    return r.json()


def remember_method(method: str) -> None:
    """Record how this user authenticates, so `jtr auth` needs no argument."""
    if method in config.AUTH_METHODS:
        config.set_value(config.KEY_AUTH_METHOD, method)


def methods_for(dialect: Dialect) -> tuple[str, ...]:
    """Which auth methods make sense for this deployment."""
    return ("sso", "pat") if dialect.supports_sso else ("token",)


def resolve_method(cfg: config.Config | None) -> str | None:
    """The saved auth method, or an inference from what's already stored.

    Someone who set up before this setting existed still has cookies or a
    PAT on disk; that's a good enough answer to save them a re-run of init.
    A saved method that doesn't fit the deployment (an `sso` config whose
    base URL later moved to Cloud) is discarded rather than replayed.
    """
    dialect = None
    if cfg:
        try:
            dialect = dialect_for(cfg)
        except UnsupportedDeployment:
            dialect = None
    allowed = methods_for(dialect) if dialect else config.AUTH_METHODS
    if cfg and cfg.auth_method and cfg.auth_method in allowed:
        return cfg.auth_method
    if dialect and dialect.is_cloud:
        return "token" if cfg and cfg.pat and cfg.email else None
    cookies = _read_cookies()
    if cookies and cookies.cookies:
        return "sso"
    if cfg and cfg.pat:
        return "pat"
    return None


def set_pat(base_url: str, pat: str) -> Credentials:
    """Persist a Server/DC PAT into .env; verify happens at call site."""
    dialect = check_supported(base_url)
    config.set_value(config.KEY_BASE_URL, base_url.rstrip("/"))
    config.set_value(config.KEY_PAT, pat)
    existing = _read_cookies()
    return Credentials(
        base_url=base_url.rstrip("/"),
        pat=pat,
        cookies=existing.cookies if existing and dialect.supports_sso else [],
        dialect=dialect,
    )


def set_api_token(base_url: str, email: str, token: str) -> Credentials:
    """Persist Cloud Basic-auth credentials into .env.

    Stored in the same secret slot as a PAT so `auth logout` / `auth status`
    stay deployment-agnostic; the email is what makes it a Basic credential.
    """
    dialect = check_supported(base_url)
    config.set_value(config.KEY_BASE_URL, base_url.rstrip("/"))
    config.set_value(config.KEY_EMAIL, email)
    config.set_value(config.KEY_PAT, token)
    return Credentials(
        base_url=base_url.rstrip("/"),
        pat=token,
        email=email,
        dialect=dialect,
    )


def _first_line(e: Exception) -> str:
    """First line of an exception message — some are long and multi-line."""
    text = str(e).strip()
    return text.splitlines()[0] if text else e.__class__.__name__


_PROBE_JS = """(async (url) => {
    try {
        const r = await fetch(url, {
            credentials: 'include',
            headers: {'Accept': 'application/json'}
        });
        const ct = r.headers.get('content-type') || '';
        if (r.status === 200 && ct.includes('application/json')) {
            return {status: r.status, ct, json: await r.json()};
        }
        const text = await r.text();
        return {status: r.status, ct, snippet: text.slice(0, 200)};
    } catch (e) {
        return {status: 0, ct: '', snippet: String(e)};
    }
})"""


def _identity_probe_js(me_url: str) -> str:
    """JS that asks Jira who we are, from inside the logged-in page.

    Runs as a same-origin fetch with the page's cookies so it reports the
    session the browser actually has, not one we guessed at.
    """
    return _PROBE_JS + "(" + json.dumps(me_url) + ")"


def sso_login(
    base_url: str,
    timeout_s: int = 300,
    force: bool = False,
    channel: str | None = None,
) -> CookieState:
    """Browser-driven SSO. Captures cookies to the cookie file."""
    base_url = base_url.rstrip("/")
    dialect = check_supported(base_url)
    if not dialect.supports_sso:
        # Fail before opening a browser: Atlassian doesn't accept browser
        # cookies as REST credentials, so a successful login here would
        # still authenticate nothing.
        raise UnsupportedDeployment(
            "Jira Cloud doesn't accept browser session cookies as REST "
            "credentials, so `jtr auth sso` cannot work against it.\n"
            "Fix: jtr auth token   (Atlassian account email + API token from "
            "https://id.atlassian.com/manage-profile/security/api-tokens)"
        )
    me_url = f"{base_url}{dialect.myself_path}"
    jira_host = urlparse(base_url).netloc
    dashboard_url = f"{base_url}/secure/Dashboard.jspa"
    probe_js = _identity_probe_js(me_url)
    existing = _read_cookies()

    if existing and existing.cookies and not force:
        try:
            who = verify(Credentials(base_url=base_url, cookies=existing.cookies))
            ident = who.get("displayName") or who.get("name") or who.get("key")
            print(f"Saved cookies still valid ({ident}); no browser needed.")
            return existing
        except SessionExpired as e:
            print(
                f"Saved cookies no longer work ({_first_line(e)}); "
                "opening browser to refresh."
            )
        except Exception as e:
            # The reuse check is only a shortcut — never let it be fatal.
            # Say what went wrong first: a VPN/DNS/base-URL problem won't
            # be fixed by a browser either, and this is the only place the
            # cause is visible.
            print(
                f"Could not verify saved cookies ({_first_line(e)}); "
                "opening browser to refresh."
            )

    browser, channel = Browser.launch(
        channel=channel or config.browser_channel(),
        profile_dir=config.browser_profile_path(),
    )
    print(f"Opened {channel} for login.")
    try:
        if existing and existing.cookies:
            # Seed the profile with what we already have; a session the IdP
            # still recognises can skip straight past the login form.
            try:
                browser.set_cookies(existing.cookies)
            except Exception as e:
                print(f"Ignoring unusable saved cookies ({_first_line(e)}).")
        browser.navigate(dashboard_url)

        print(f"Waiting up to {timeout_s}s for SSO to complete...")
        deadline = time.monotonic() + timeout_s
        identity: dict | None = None
        cookies: list[dict] = []
        last_signal = ""
        landed_on_jira = False
        while time.monotonic() < deadline:
            page_url = browser.current_url()

            # The definitive test, and the only one that matters: do the
            # browser's cookies authenticate a real jtr request? Asking the
            # page to fetch /myself is a proxy for this and a fragile one —
            # once the tab lands on a raw REST response or an error document
            # it can keep failing while the cookies are already good.
            cookies = browser.cookies()
            if cookies:
                try:
                    identity = verify(Credentials(base_url=base_url, cookies=cookies))
                    break
                except SessionExpired:
                    pass
                except Exception as e:
                    signal = f"cookie check failed: {_first_line(e)}"
                    if signal != last_signal:
                        print(f"  ... {signal}")
                        last_signal = signal

            # Load the dashboard once the SSO chain returns to Jira. Strictly
            # once: WebSEAL serves its login form from this same host, and
            # re-navigating on a timer would yank the page out from under
            # someone still typing their password.
            if (
                page_url
                and urlparse(page_url).netloc == jira_host
                and not landed_on_jira
            ):
                landed_on_jira = True
                try:
                    browser.navigate(dashboard_url)
                    print("  ... SSO returned; loaded dashboard, checking session")
                except Exception as e:
                    print(f"  ... dashboard load failed: {e}")

            try:
                page_probe = browser.evaluate(probe_js)
                if not isinstance(page_probe, dict):
                    raise RuntimeError(f"unexpected probe result: {page_probe!r}")
                ct = (page_probe.get("ct") or "").lower()
                signal = (
                    f"status={page_probe['status']} ct={ct.split(';')[0]} "
                    f"url={page_url}"
                )
                # status 0 means the in-page fetch threw; the reason is the
                # only useful thing in that case, so don't discard it.
                if not page_probe.get("status") and page_probe.get("snippet"):
                    signal += f" ({page_probe['snippet']})"
                if signal != last_signal:
                    print(f"  ... not yet: {signal}")
                    last_signal = signal
            except Exception as e:
                signal = f"err={type(e).__name__}: {e}"
                if signal != last_signal:
                    print(f"  ... not yet: {signal}")
                    last_signal = signal
            time.sleep(2)

        if identity is None:
            # Report the last probe result: when the cause is a wrong base URL
            # or a dead network, "did you complete the login?" is a red herring.
            detail = f" Last seen: {last_signal}." if last_signal else ""
            raise RuntimeError(
                f"Timed out after {timeout_s}s waiting for SSO. "
                f"Did you complete the login?{detail}"
            )

        print(
            f"  SSO confirmed as "
            f"{identity.get('displayName') or identity.get('name')}"
        )
        cookies = browser.cookies()
    finally:
        browser.close()

    state = CookieState(
        base_url=base_url,
        cookies=cookies,
        captured_at=datetime.now(UTC).isoformat(),
    )
    _write_cookies(state)
    return state
