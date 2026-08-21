from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv, set_key
from platformdirs import user_config_dir

try:
    from importlib.resources import files
except ImportError:
    from importlib_resources import files  # type: ignore[import-not-found,no-redef]

KEY_BASE_URL = "JTR_BASE_URL"
# The secret, whatever its flavour: a Bearer PAT on Server/DC, an API token
# paired with JTR_EMAIL on Cloud. One slot, so `auth logout` and `auth
# status` don't need to know which deployment they're clearing.
KEY_PAT = "JTR_PAT"
# Atlassian account email — the username half of Cloud's Basic auth.
KEY_EMAIL = "JTR_EMAIL"
KEY_PROJECT = "JTR_PROJECT"
KEY_AUTH_METHOD = "JTR_AUTH_METHOD"
# server | cloud | auto (default). Only needed when the hostname lies:
# a Cloud tenant on a vanity domain, or DC on something that looks like one.
KEY_DEPLOYMENT = "JTR_DEPLOYMENT"
# Escape hatch, normally unset. See dialect.py for why this isn't a prompt.
KEY_API_VERSION = "JTR_API_VERSION"
# Which browser to drive for SSO. Lives in the *global* .env: it describes
# the machine, not the project, so it shouldn't be re-set per checkout.
KEY_BROWSER_CHANNEL = "JTR_BROWSER_CHANNEL"
# Absolute path to the config dir, overriding both ./.jtr/ and ~/.jtr/.
# Read from the real environment only — never from a .env file, since the
# .env we would read lives inside the directory this setting selects.
KEY_CONFIG_DIR = "JTR_CONFIG_DIR"

# sso/pat are Server/DC; token is Cloud (email + API token over Basic).
AUTH_METHODS = ("sso", "pat", "token")

_ENV_NAME = ".env"
_SESSION_NAME = ".jtr_session.json"
_AUDIT_NAME = ".jtr_audit.jsonl"
_BROWSER_PROFILE_NAME = "browser-profile"
_PROJECT_DIR = ".jtr"
_GITIGNORE_ENTRY = ".jtr/"


def _global_dir() -> Path:
    """Global config dir: ~/.jtr/. Migrates from the old platformdirs path once."""
    new = Path.home() / _PROJECT_DIR
    _migrate_legacy_global(new)
    return new


def _migrate_legacy_global(new: Path) -> None:
    old = Path(user_config_dir("jtr"))
    if old == new or new.exists() or not old.exists():
        return
    try:
        old.rename(new)
    except OSError:
        shutil.copytree(old, new)
        shutil.rmtree(old, ignore_errors=True)


def _install_bundled_skills(cwd: Path) -> list[str]:
    """Copy bundled skills from the package to ./.claude/skills/.

    Returns list of skill names that were installed (not skipped).
    """
    skills_target = cwd / ".claude" / "skills"
    installed = []

    # Get bundled_skills path from package
    try:
        bundled = files("jtr").joinpath("bundled_skills")
    except (TypeError, AttributeError, ModuleNotFoundError):
        # Fallback for development or edge cases
        return installed

    # Copy each skill directory
    for skill_name in ["jtr"]:
        try:
            skill_src = bundled.joinpath(skill_name)
            skill_dst = skills_target / skill_name

            # Skip if already exists
            if skill_dst.exists():
                continue

            # Only now create the tree — a skipped install must not leave an
            # empty .claude/skills/ behind in someone else's directory.
            skills_target.mkdir(parents=True, exist_ok=True)

            # Extract to temporary location first (works with both filesystem and zip)
            if hasattr(skill_src, "is_dir") and skill_src.is_dir():
                shutil.copytree(skill_src, skill_dst)
                installed.append(skill_name)
            else:
                # Handle zip/package case by reading files
                skill_dst.mkdir(parents=True, exist_ok=True)
                _copy_skill_from_package(skill_src, skill_dst)
                installed.append(skill_name)
        except Exception:
            # Silently skip on errors - skills are optional
            continue

    return installed


def _copy_skill_from_package(src, dst: Path) -> None:
    """Recursively copy skill files from package resources."""
    try:
        if hasattr(src, "iterdir"):
            for item in src.iterdir():
                if item.is_file():
                    (dst / item.name).write_bytes(item.read_bytes())
                elif item.is_dir():
                    subdir = dst / item.name
                    subdir.mkdir(exist_ok=True)
                    _copy_skill_from_package(item, subdir)
    except Exception:
        pass


def config_dir() -> Path:
    """Active config dir: $JTR_CONFIG_DIR, else `./.jtr/`, else the user dir.

    The env var exists so an embedder can pin every piece of jtr state —
    .env, cookies, audit log — without controlling the working directory.
    """
    override = os.environ.get(KEY_CONFIG_DIR, "").strip()
    if override:
        return Path(override).expanduser()
    local = Path.cwd() / _PROJECT_DIR
    if local.is_dir():
        return local
    return _global_dir()


def use_config_dir(path: Path) -> None:
    """Pin the active config dir for the rest of this process."""
    os.environ[KEY_CONFIG_DIR] = str(path)


def is_project_local() -> bool:
    return config_dir() != _global_dir()


class InitError(RuntimeError):
    pass


def init_project(
    cwd: Path,
    *,
    update_gitignore: bool,
    base_url: str = "",
    project: str = "",
    auth_method: str = "",
    deployment: str = "",
    email: str = "",
    force: bool = False,
    install_skills: bool = True,
    target: Path | None = None,
) -> tuple[Path, bool, list[str]]:
    """Create `cwd/.jtr/` and scaffold a .env inside it.

    `target` overrides the config-dir location outright (used for
    $JTR_CONFIG_DIR); `cwd` still decides where .gitignore and the
    bundled skills go.

    Returns (project_dir, gitignore_touched, installed_skills). Raises
    InitError if the folder already exists and `force` is not set; with
    `force`, the existing config is updated in place instead.

    Only non-empty settings are written, so a re-run that supplies just one
    value leaves the others alone rather than blanking them.

    Also copies bundled Claude Code skills to `cwd/.claude/skills/`, unless
    `install_skills` is False.
    """
    target = target or (cwd / _PROJECT_DIR)
    if target.exists():
        if not force:
            raise InitError(f"{target} already exists.")
    else:
        target.mkdir(mode=0o700, parents=True, exist_ok=False)
    # Everything from here on (.env, cookies, audit log) must land in the
    # directory we just created, even when it isn't under the cwd.
    use_config_dir(target)
    ensure_env_file()
    for key, value in (
        (KEY_BASE_URL, base_url),
        (KEY_PROJECT, project),
        (KEY_AUTH_METHOD, auth_method),
        (KEY_DEPLOYMENT, deployment),
        (KEY_EMAIL, email),
    ):
        if value:
            set_value(key, value)

    # Copy bundled skills to ./.claude/skills/
    installed_skills = _install_bundled_skills(cwd) if install_skills else []

    touched = False
    if update_gitignore:
        gi = cwd / ".gitignore"
        if gi.exists():
            existing = gi.read_text(encoding="utf-8").splitlines()
            if _GITIGNORE_ENTRY not in existing and ".jtr" not in existing:
                with gi.open("a", encoding="utf-8") as fh:
                    if existing and existing[-1] != "":
                        fh.write("\n")
                    fh.write(_GITIGNORE_ENTRY + "\n")
                touched = True
    return target, touched, installed_skills


def env_path() -> Path:
    return config_dir() / _ENV_NAME


def session_path() -> Path:
    return config_dir() / _SESSION_NAME


def audit_path() -> Path:
    return config_dir() / _AUDIT_NAME


def browser_profile_path() -> Path:
    """Browser profile kept between SSO logins, so the IdP can remember you.

    Deliberately global rather than project-local: it holds one person's
    SSO identity, which is the same whichever project directory they run
    from. Cleared by `jtr auth logout`.
    """
    return _global_dir() / _BROWSER_PROFILE_NAME


def _load() -> None:
    p = env_path()
    if p.exists():
        load_dotenv(p, override=False, encoding="utf-8")


def ensure_env_file() -> None:
    """Create an empty .env scaffold in the active config dir if missing."""
    p = env_path()
    if p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "# jtr settings - values populated on first run\n"
        f"{KEY_BASE_URL}=\n"
        f"{KEY_PAT}=\n"
        "# Cloud only: the account email paired with the API token above.\n"
        f"{KEY_EMAIL}=\n"
        f"{KEY_PROJECT}=\n"
        f"{KEY_AUTH_METHOD}=\n"
        "# server | cloud | auto (blank = auto-detect from the base URL).\n"
        f"{KEY_DEPLOYMENT}=\n",
        encoding="utf-8",
    )
    os.chmod(p, 0o600)


def get(key: str) -> str | None:
    _load()
    v = os.environ.get(key)
    return v.strip() if v else None


def set_value(key: str, value: str, *, scope: str = "active") -> None:
    """Write a setting. `scope="global"` targets ~/.jtr/.env explicitly."""
    p = _global_dir() / _ENV_NAME if scope == "global" else env_path()
    if p == env_path():
        ensure_env_file()
    else:
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        p.touch(mode=0o600, exist_ok=True)
    set_key(str(p), key, value, quote_mode="never")
    os.environ[key] = value
    os.chmod(p, 0o600)


def browser_channel() -> str | None:
    """Browser channel for SSO: real env var, then active .env, then global.

    Read explicitly rather than by loading the global .env into the process,
    so a project-local config never inherits a global base URL or PAT.
    """
    v = get(KEY_BROWSER_CHANNEL)
    if v:
        return v
    p = _global_dir() / _ENV_NAME
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            k, sep, val = line.partition("=")
            if sep and k.strip() == KEY_BROWSER_CHANNEL and val.strip():
                return val.strip()
    return None


@dataclass
class Config:
    base_url: str
    pat: str | None
    project: str | None
    auth_method: str | None = None
    email: str | None = None
    # Raw, unvalidated overrides — dialect.Dialect.resolve() is what decides
    # whether they make sense together, so it can report one clear error.
    deployment: str | None = None
    api_version: str | None = None


def load() -> Config | None:
    base = get(KEY_BASE_URL)
    if not base:
        return None
    method = (get(KEY_AUTH_METHOD) or "").lower()
    return Config(
        base_url=base.rstrip("/"),
        pat=get(KEY_PAT),
        project=get(KEY_PROJECT),
        auth_method=method if method in AUTH_METHODS else None,
        email=get(KEY_EMAIL),
        deployment=get(KEY_DEPLOYMENT),
        api_version=get(KEY_API_VERSION),
    )
