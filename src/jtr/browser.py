"""Minimal Chrome DevTools Protocol driver for the SSO login flow.

jtr only needs three things from a browser: show the user a login page,
tell us what happened, and hand back the cookies. That doesn't justify a
browser-automation framework — and on a corporate network it actively hurt,
because Playwright insisted on downloading its own Chromium through a
TLS-intercepting proxy that blocks it.

So we drive the Edge or Chrome that is already installed. No download, no
extra runtime, nothing for a proxy to intercept.

The browser runs against a throwaway profile directory and an ephemeral
debugging port, both discarded when the login finishes. While it is open,
any local process could in principle attach to that port; the profile
holds no user data and the window is only as long as the login takes.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import websocket

# Talk to the debugging port directly. Corporate machines set HTTP(S)_PROXY
# globally, and routing a 127.0.0.1 call through a proxy fails in confusing
# ways, so this opener has proxies explicitly disabled.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# Chrome refuses a websocket upgrade carrying an unexpected Origin header
# (111+). We suppress the header client-side and allow it server-side, since
# which of the two applies varies by browser build.
_LAUNCH_FLAGS = [
    "--remote-debugging-port=0",
    "--remote-allow-origins=*",
    "--no-first-run",
    "--no-default-browser-check",
]

# Fields Network.setCookies accepts. Anything else (CDP's `size`, `session`,
# `priority`, ...) is rejected, and our saved cookies carry those.
_COOKIE_PARAM_KEYS = ("name", "value", "domain", "path", "secure", "httpOnly", "sameSite")


class BrowserError(RuntimeError):
    pass


# --- finding an installed browser -------------------------------------


def _mac_candidates() -> dict[str, list[str]]:
    home = Path.home()
    out: dict[str, list[str]] = {}
    for channel, app in (
        ("msedge", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ("chrome", "Google Chrome.app/Contents/MacOS/Google Chrome"),
        ("chromium", "Chromium.app/Contents/MacOS/Chromium"),
    ):
        out[channel] = [f"/Applications/{app}", str(home / "Applications" / app)]
    return out


def _windows_candidates() -> dict[str, list[str]]:
    roots = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    out: dict[str, list[str]] = {}
    for channel, rel in (
        ("msedge", r"Microsoft\Edge\Application\msedge.exe"),
        ("chrome", r"Google\Chrome\Application\chrome.exe"),
    ):
        out[channel] = [str(Path(r) / rel) for r in roots if r]
    return out


_LINUX_COMMANDS = {
    "msedge": ["microsoft-edge", "microsoft-edge-stable"],
    "chrome": ["google-chrome", "google-chrome-stable"],
    "chromium": ["chromium", "chromium-browser"],
}


def _candidates() -> dict[str, list[str]]:
    system = platform.system()
    if system == "Darwin":
        return _mac_candidates()
    if system == "Windows":
        return _windows_candidates()
    return {
        channel: [p for cmd in cmds if (p := shutil.which(cmd))]
        for channel, cmds in _LINUX_COMMANDS.items()
    }


def find_browser(channel: str | None = None) -> tuple[str, str]:
    """Locate an installed Chromium-family browser.

    Returns (channel, executable path). `JTR_BROWSER` overrides everything
    with an explicit path; `JTR_BROWSER_CHANNEL` (or `channel`) narrows the
    search to one of msedge/chrome/chromium.
    """
    explicit = os.environ.get("JTR_BROWSER")
    if explicit:
        if not Path(explicit).exists():
            raise BrowserError(
                f"JTR_BROWSER points at {explicit}, which doesn't exist."
            )
        return "custom", explicit

    channel = channel or os.environ.get("JTR_BROWSER_CHANNEL") or None
    found = _candidates()
    if channel:
        if channel not in found:
            raise BrowserError(
                f"Unknown browser channel {channel!r}. "
                "Use 'msedge', 'chrome' or 'chromium', or set JTR_BROWSER to "
                "the full path of a browser executable."
            )
        for path in found[channel]:
            if Path(path).exists():
                return channel, path
        raise BrowserError(
            f"No {channel} found. Looked in:\n"
            + "\n".join(f"  {p}" for p in found[channel])
            + "\nInstall it, pick another channel, or set JTR_BROWSER to a "
            "browser executable."
        )

    for name in ("msedge", "chrome", "chromium"):
        for path in found.get(name, []):
            if Path(path).exists():
                return name, path

    searched = [p for paths in found.values() for p in paths]
    raise BrowserError(
        "No installed browser found for SSO login. jtr needs Microsoft Edge, "
        "Google Chrome or Chromium.\nLooked in:\n"
        + "\n".join(f"  {p}" for p in searched)
        + "\nSet JTR_BROWSER to a browser executable if yours lives elsewhere."
    )


# --- CDP transport ----------------------------------------------------


def _http_json(url: str, timeout: float = 5.0):
    with _opener.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class _Connection:
    """One websocket to one CDP target, with synchronous request/response."""

    def __init__(self, ws_url: str):
        self._ws = websocket.create_connection(
            ws_url, suppress_origin=True, timeout=30, max_size=None
        )
        self._next_id = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        self._next_id += 1
        msg_id = self._next_id
        payload = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params
        try:
            self._ws.send(json.dumps(payload))
        except Exception as e:
            raise BrowserError(f"Lost the browser connection: {e}") from e

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserError(f"{method} timed out after {timeout:g}s")
            self._ws.settimeout(remaining)
            try:
                raw = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as e:
                raise BrowserError(
                    f"Lost the browser connection during {method} "
                    "(was the window closed?)"
                ) from e
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            # Everything without our id is an event or a reply we no longer
            # care about; CDP guarantees ids come back exactly once.
            if data.get("id") != msg_id:
                continue
            if "error" in data:
                raise BrowserError(f"{method}: {data['error'].get('message', data['error'])}")
            return data.get("result", {})

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._ws.close()


def _exception_text(details: dict) -> str:
    exc = details.get("exception") or {}
    return exc.get("description") or exc.get("value") or details.get("text") or "JS error"


def _cookie_param(cookie: dict) -> dict | None:
    """Narrow a saved cookie to what Network.setCookies will accept."""
    if not cookie.get("name") or not cookie.get("domain"):
        return None
    param = {k: cookie[k] for k in _COOKIE_PARAM_KEYS if k in cookie}
    param.setdefault("value", "")
    if param.get("sameSite") not in ("Strict", "Lax", "None"):
        param.pop("sameSite", None)
    # Chrome *silently* discards SameSite=None without Secure. Losing the
    # attribute is survivable; losing the whole session cookie is not.
    if param.get("sameSite") == "None" and not param.get("secure"):
        param.pop("sameSite")
    expires = cookie.get("expires")
    # -1 (Playwright) and 0 both mean "session cookie"; CDP wants the field
    # left out entirely for those.
    if isinstance(expires, (int, float)) and expires > 0:
        param["expires"] = expires
    return param


# --- the browser ------------------------------------------------------


class Browser:
    """A launched browser with one page attached over CDP."""

    def __init__(
        self, proc, profile_dir: Path, debug_url: str, target_id: str, conn,
        keep_profile: bool = False,
    ):
        self._proc = proc
        self._profile_dir = profile_dir
        self._debug_url = debug_url
        self._target_id = target_id
        self._conn = conn
        self._keep_profile = keep_profile

    # -- lifecycle --

    @classmethod
    def launch(
        cls, channel: str | None = None, profile_dir: Path | None = None
    ) -> tuple[Browser, str]:
        """Start a browser and attach to its first page. Returns (browser, channel).

        A `profile_dir` is kept between runs, so the identity provider can
        remember the login and skip the form next time. Without one, a
        throwaway profile is used and deleted on close.
        """
        channel, exe = find_browser(channel)
        keep_profile = profile_dir is not None
        if profile_dir is None:
            profile_dir = Path(tempfile.mkdtemp(prefix="jtr-sso-"))
        else:
            profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            # A crashed run leaves this behind pointing at a dead port; we
            # would then poll it and attach to nothing.
            (profile_dir / "DevToolsActivePort").unlink(missing_ok=True)
        try:
            proc = subprocess.Popen(
                [exe, *_LAUNCH_FLAGS, f"--user-data-dir={profile_dir}", "about:blank"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            if not keep_profile:
                shutil.rmtree(profile_dir, ignore_errors=True)
            raise BrowserError(f"Could not start {exe}: {e}") from e

        try:
            port = cls._wait_for_port(proc, profile_dir)
            debug_url = f"http://127.0.0.1:{port}"
            target_id, ws_url = cls._wait_for_page(debug_url)
            conn = _Connection(ws_url)
        except Exception:
            _terminate(proc)
            if not keep_profile:
                shutil.rmtree(profile_dir, ignore_errors=True)
            raise

        browser = cls(proc, profile_dir, debug_url, target_id, conn, keep_profile)
        for domain in ("Page", "Runtime", "Network"):
            browser._conn.call(f"{domain}.enable")
        return browser, channel

    @staticmethod
    def _wait_for_port(proc, profile_dir: Path, timeout: float = 30.0) -> int:
        """Read the port the browser chose out of its DevToolsActivePort file."""
        port_file = profile_dir / "DevToolsActivePort"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise BrowserError(
                    f"The browser exited immediately (code {proc.returncode}) "
                    "instead of opening for login."
                )
            if port_file.exists():
                lines = port_file.read_text(encoding="utf-8").splitlines()
                if lines and lines[0].strip().isdigit():
                    return int(lines[0].strip())
            time.sleep(0.1)
        raise BrowserError(
            f"The browser did not expose a debugging port within {timeout:g}s."
        )

    @staticmethod
    def _wait_for_page(debug_url: str, timeout: float = 15.0) -> tuple[str, str]:
        deadline = time.monotonic() + timeout
        last = "no page targets"
        while time.monotonic() < deadline:
            try:
                for t in _http_json(f"{debug_url}/json/list"):
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        return t["id"], t["webSocketDebuggerUrl"]
            except Exception as e:
                last = str(e)
            time.sleep(0.2)
        raise BrowserError(f"Could not attach to a browser tab: {last}")

    def close(self) -> None:
        self._conn.close()
        _terminate(self._proc)
        if not self._keep_profile:
            shutil.rmtree(self._profile_dir, ignore_errors=True)

    def __enter__(self) -> Browser:
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # -- page control --

    def navigate(self, url: str) -> None:
        self._conn.call("Page.navigate", {"url": url})

    def current_url(self) -> str:
        """Read the tab's URL over HTTP rather than JS.

        During an SSO redirect chain there may be no usable JS execution
        context, but /json/list always reports where the tab actually is.
        """
        try:
            for t in _http_json(f"{self._debug_url}/json/list"):
                if t.get("id") == self._target_id:
                    return t.get("url", "")
        except Exception:
            pass
        return ""

    def evaluate(self, expression: str, timeout: float = 30.0):
        result = self._conn.call(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            raise BrowserError(_exception_text(result["exceptionDetails"]))
        return result.get("result", {}).get("value")

    # -- cookies --

    def cookies(self) -> list[dict]:
        try:
            result = self._conn.call("Storage.getCookies")
        except BrowserError:
            result = self._conn.call("Network.getAllCookies")
        return result.get("cookies", [])

    def set_cookies(self, cookies: list[dict]) -> None:
        params = [p for c in cookies if (p := _cookie_param(c))]
        if params:
            self._conn.call("Network.setCookies", {"cookies": params})


def _terminate(proc) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
