"""Install and refresh the bundled `/jtr` agent skill.

The skill ships inside the package and is copied to an agent's skills
directory: `./.claude/skills/jtr` for one project, `~/.claude/skills/jtr`
for all of them (Claude Code, the default). The same SKILL.md format is
read by Codex, Gemini CLI, Copilot, Cursor and OpenCode, each from its own
pair of directories — see AGENTS. A copy made by an older jtr goes stale when jtr itself is
upgraded, so each install leaves a small manifest behind recording what was
written. That is what lets a later run tell an untouched old copy (safe to
replace) from one the user has edited (not ours to overwrite).
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from . import __version__

try:
    from importlib.resources import files
except ImportError:
    from importlib_resources import files  # type: ignore[import-not-found,no-redef]

SKILL_NAME = "jtr"
_MANIFEST = ".jtr-skill.json"

MISSING = "missing"
CURRENT = "current"
OUTDATED = "outdated"
MODIFIED = "modified"

INSTALLED = "installed"
UPDATED = "updated"
UNCHANGED = "unchanged"

# Digests of every SKILL.md shipped before installs wrote a manifest
# (jtr <= 1.1.0). A manifest-less copy matching one of these is a pristine
# old install; anything else without a manifest has been edited by hand.
# Frozen: releases from 1.2.0 on are recognised by their manifest instead.
_LEGACY_DIGESTS: frozenset[str] = frozenset({
    "2842d4021d29ef8ebafff0ea09cefe940a87aadeeeddfc8a7c96c5c2a1e24f51",
    "445c8be445f2ed46224970c2b81c4b47b4c79b7af45ad2d6482a2ee1c1b6e644",
    "488f94c8f6584e54ef46e248260b6a450a600d07acc6a2c9deeaac7430728bec",
    "48961190c5a7ee4f3a7a2bda24e00af5be3ceadb065dced218b44b13b452d657",
    "765158a71c7310b6bafefb89d9e7744f3be903f7b8f6fa9c1b374e966addc6df",
    "a5e605f14a7719ac9609b7b72ef7d05cb56f73bc2a3d2d8a8d13d7c979c1d844",
    "bebb9331e34c7036bb52aca5edf46270c2d4579a37d4069b5e189348229a1145",
    "bf6e9b2a398e547330d4d727824f99aa88bed1e002bec3ddf09ae5814e67e4b8",
    "dc29c5fe00e1e0ff705d6cade3f3ca837c00ee67efb6d5147a16f2e3d032d5c9",
    "f13b795d618f2e1599a492238c6f6c03b0f48191f2a6688b161d7f8312f899e4",
})


@dataclass(frozen=True)
class Agent:
    key: str
    label: str
    global_root: Callable[[], Path]  # where `skills/` lives for every project
    local: str  # project-relative skills dir, posix


def _home_dir(env: str, default: str) -> Callable[[], Path]:
    def root() -> Path:
        override = os.environ.get(env, "").strip() if env else ""
        return Path(override).expanduser() if override else Path.home() / default
    return root


def _xdg_config(sub: str) -> Callable[[], Path]:
    def root() -> Path:
        override = os.environ.get("XDG_CONFIG_HOME", "").strip()
        base = Path(override).expanduser() if override else Path.home() / ".config"
        return base / sub
    return root


DEFAULT_AGENT = "claude"

# Ordered: the menu in `jtr init` and `skill status` list them in this order.
AGENTS: dict[str, Agent] = {
    a.key: a
    for a in (
        # Claude Code honours $CLAUDE_CONFIG_DIR as a replacement for ~/.claude.
        Agent("claude", "Claude Code", _home_dir("CLAUDE_CONFIG_DIR", ".claude"), ".claude/skills"),
        Agent("codex", "Codex", _home_dir("", ".agents"), ".agents/skills"),
        Agent("gemini", "Gemini CLI", _home_dir("", ".gemini"), ".gemini/skills"),
        Agent("copilot", "GitHub Copilot", _home_dir("", ".copilot"), ".github/skills"),
        Agent("cursor", "Cursor", _home_dir("", ".cursor"), ".cursor/skills"),
        Agent("opencode", "OpenCode", _xdg_config("opencode"), ".opencode/skills"),
    )
}
_ALIASES = {"agents": "codex"}


def agent_names() -> list[str]:
    return list(AGENTS)


def resolve_agent(name: str | None) -> str:
    """Canonical agent key for `name` (case-insensitive, aliases allowed)."""
    key = (name or DEFAULT_AGENT).strip().lower()
    key = _ALIASES.get(key, key)
    if key not in AGENTS:
        raise ValueError(
            f"Unknown agent {name!r}. Choose one of: {', '.join(AGENTS)}."
        )
    return key


@dataclass
class SkillStatus:
    agent: str  # key into AGENTS
    scope: str  # "project" | "global"
    path: str
    state: str  # MISSING | CURRENT | OUTDATED | MODIFIED
    version: str | None  # jtr version that wrote the copy, when known


def project_dir(cwd: Path | None = None, agent: str = DEFAULT_AGENT) -> Path:
    return (cwd or Path.cwd()) / AGENTS[resolve_agent(agent)].local / SKILL_NAME


def global_dir(agent: str = DEFAULT_AGENT) -> Path:
    return AGENTS[resolve_agent(agent)].global_root() / "skills" / SKILL_NAME


def scope_dir(scope: str, cwd: Path | None = None, agent: str = DEFAULT_AGENT) -> Path:
    return global_dir(agent) if scope == "global" else project_dir(cwd, agent)


def _walk(node, prefix: str = "") -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for item in node.iterdir():
        rel = f"{prefix}{item.name}"
        if item.is_dir():
            if item.name != "__pycache__":
                out.update(_walk(item, rel + "/"))
        elif item.name != _MANIFEST:
            out[rel] = item.read_bytes()
    return out


def bundled_files() -> dict[str, bytes]:
    """The skill as shipped: {relative posix path: content}."""
    return _walk(files("jtr").joinpath("bundled_skills", SKILL_NAME))


def digest(contents: dict[str, bytes]) -> str:
    # Line endings are normalised so a checkout or editor that rewrites them
    # doesn't make an untouched copy look edited.
    h = hashlib.sha256()
    for rel in sorted(contents):
        h.update(rel.encode())
        h.update(b"\0")
        h.update(contents[rel].replace(b"\r\n", b"\n"))
        h.update(b"\0")
    return h.hexdigest()


def _read_manifest(dst: Path) -> dict:
    try:
        data = json.loads((dst / _MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def status(
    scope: str, cwd: Path | None = None, agent: str = DEFAULT_AGENT
) -> SkillStatus:
    agent = resolve_agent(agent)
    dst = scope_dir(scope, cwd, agent)
    if not (dst / "SKILL.md").is_file():
        return SkillStatus(agent, scope, str(dst), MISSING, None)
    manifest = _read_manifest(dst)
    present = _walk(dst)
    bundled = bundled_files()

    # Only files jtr put there are compared; anything else in the folder
    # is the user's and doesn't make the copy "edited".
    def have(names) -> str:
        return digest({rel: present[rel] for rel in names if rel in present})

    if have(bundled) == digest(bundled):
        return SkillStatus(agent, scope, str(dst), CURRENT, __version__)
    if manifest:
        pristine = have(manifest.get("files", [])) == manifest.get("digest")
    else:
        pristine = have(["SKILL.md"]) in _LEGACY_DIGESTS
    state = OUTDATED if pristine else MODIFIED
    return SkillStatus(agent, scope, str(dst), state, manifest.get("version"))


def install(
    scope: str,
    cwd: Path | None = None,
    *,
    agent: str = DEFAULT_AGENT,
    force: bool = False,
) -> str:
    """Install or refresh the skill in `scope` for `agent`.

    Returns INSTALLED, UPDATED, UNCHANGED, or MODIFIED — the last meaning the
    copy has local edits and was left alone because `force` wasn't set.
    """
    before = status(scope, cwd, agent)
    if before.state == MODIFIED and not force:
        return MODIFIED
    dst = Path(before.path)
    bundled = bundled_files()
    if before.state != CURRENT:
        # Drop files an earlier version shipped and this one doesn't; anything
        # else in the folder isn't ours and stays.
        for rel in _read_manifest(dst).get("files", []):
            if rel not in bundled:
                (dst / rel).unlink(missing_ok=True)
        for rel, content in bundled.items():
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    manifest = {
        "version": __version__,
        "digest": digest(bundled),
        "files": sorted(bundled),
    }
    if _read_manifest(dst) != manifest:
        (dst / _MANIFEST).write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
    if before.state == CURRENT:
        return UNCHANGED
    return INSTALLED if before.state == MISSING else UPDATED


def refresh_existing(cwd: Path | None = None) -> list[dict]:
    """Bring every copy that already exists up to date; never create one.

    Every agent's project and global locations are checked. Returns one
    row per existing copy: its status fields plus `action`.
    """
    rows = []
    for agent in AGENTS:
        for scope in ("project", "global"):
            if status(scope, cwd, agent).state == MISSING:
                continue
            action = install(scope, cwd, agent=agent)
            rows.append({**asdict(status(scope, cwd, agent)), "action": action})
    return rows


def all_statuses(cwd: Path | None = None) -> list[SkillStatus]:
    """Status of every agent's project and global copy, in AGENTS order."""
    return [
        status(scope, cwd, agent)
        for agent in AGENTS
        for scope in ("project", "global")
    ]
