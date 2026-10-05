"""Find and install newer jtr releases from GitHub."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

REPO = "n-dimitrov/jtr"
SITE_URL = "https://n-dimitrov.github.io/jtr"
INSTALL_SH = f"curl -LsSf {SITE_URL}/install.sh | sh"
INSTALL_PS1 = f"irm {SITE_URL}/install.ps1 | iex"

_TAG_RE = re.compile(r"/releases/tag/v(\d+(?:\.\d+)*)$")


class UpdateError(RuntimeError):
    pass


def install_command() -> str:
    """The one-line installer for this OS — also the manual upgrade path."""
    return INSTALL_PS1 if sys.platform == "win32" else INSTALL_SH


def latest_version() -> str:
    """Latest released version, e.g. "1.2.0".

    Read off the redirect of /releases/latest rather than the REST API: the
    API allows 60 unauthenticated calls an hour per IP, which a whole office
    behind one proxy shares.
    """
    url = f"https://github.com/{REPO}/releases/latest"
    try:
        r = httpx.head(url, follow_redirects=False, timeout=15)
    except httpx.HTTPError as e:
        raise UpdateError(f"Couldn't reach github.com: {e}") from e
    m = _TAG_RE.search(r.headers.get("location", ""))
    if not m:
        raise UpdateError(f"Couldn't determine the latest release from {url}.")
    return m.group(1)


def parse_version(v: str) -> tuple[int, ...]:
    """Leading numeric components: "1.2.0" -> (1, 2, 0); junk -> ()."""
    m = re.match(r"\d+(?:\.\d+)*", v)
    return tuple(int(p) for p in m.group(0).split(".")) if m else ()


def is_newer(latest: str, installed: str) -> bool:
    return parse_version(latest) > parse_version(installed)


def wheel_url(version: str) -> str:
    return (
        f"https://github.com/{REPO}/releases/download/"
        f"v{version}/jtr-{version}-py3-none-any.whl"
    )


def _uv() -> str:
    uv = shutil.which("uv")
    if not uv:
        raise UpdateError("uv isn't on PATH, so jtr can't update itself.")
    return uv


def is_uv_tool_install() -> bool:
    """True when the running jtr lives in uv's tool directory.

    A dev checkout or a pip/pipx install must not be "updated" by dropping
    a second, uv-managed copy next to it.
    """
    try:
        out = subprocess.run(
            [_uv(), "tool", "dir"], capture_output=True, text=True, check=True
        ).stdout.strip()
        return Path(sys.prefix).resolve().is_relative_to(Path(out).resolve())
    except (UpdateError, OSError, subprocess.CalledProcessError):
        return False


def install(version: str) -> None:
    """Replace the installed jtr with `version`. uv's output goes to stderr."""
    proc = subprocess.run(
        [_uv(), "tool", "install", "--force", wheel_url(version)],
        stdout=sys.stderr,
    )
    if proc.returncode != 0:
        raise UpdateError(f"uv tool install failed (exit {proc.returncode}).")
