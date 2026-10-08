from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from jtr import skills
from jtr.cli import app

runner = CliRunner()


def run(*args, **kw):
    return runner.invoke(app, list(args), **kw)


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


def skill_md(scope="project", agent="claude"):
    return skills.scope_dir(scope, agent=agent) / "SKILL.md"


def test_install_then_current(project):
    assert skills.status("project").state == skills.MISSING
    assert skills.install("project") == skills.INSTALLED
    assert skill_md().read_bytes() == skills.bundled_files()["SKILL.md"]
    assert skills.status("project").state == skills.CURRENT
    assert skills.install("project") == skills.UNCHANGED


def test_global_scope_is_separate_from_project(project):
    skills.install("global")
    assert skill_md("global").is_file()
    assert skills.status("project").state == skills.MISSING


def test_copy_from_an_older_release_is_outdated_and_refreshed(project, monkeypatch):
    skills.install("project")
    # Pretend a newer jtr ships a different skill.
    newer = {"SKILL.md": b"# newer\n"}
    monkeypatch.setattr(skills, "bundled_files", lambda: newer)
    st = skills.status("project")
    assert st.state == skills.OUTDATED
    assert skills.install("project") == skills.UPDATED
    assert skill_md().read_bytes() == b"# newer\n"
    assert skills.status("project").state == skills.CURRENT


def test_edited_copy_is_kept_unless_forced(project):
    skills.install("project")
    skill_md().write_text("my own notes\n")
    assert skills.status("project").state == skills.MODIFIED
    assert skills.install("project") == skills.MODIFIED
    assert skill_md().read_text() == "my own notes\n"
    assert skills.install("project", force=True) == skills.UPDATED
    assert skills.status("project").state == skills.CURRENT


def test_manifestless_copy_from_a_known_old_release_is_outdated(project, monkeypatch):
    old = b"# an old skill\n"
    skill_md().parent.mkdir(parents=True)
    skill_md().write_bytes(old)
    assert skills.status("project").state == skills.MODIFIED
    monkeypatch.setattr(
        skills, "_LEGACY_DIGESTS", frozenset({skills.digest({"SKILL.md": old})})
    )
    assert skills.status("project").state == skills.OUTDATED
    assert skills.install("project") == skills.UPDATED


def test_line_endings_do_not_count_as_an_edit(project):
    skills.install("project")
    skill_md().write_bytes(skill_md().read_bytes().replace(b"\n", b"\r\n"))
    assert skills.status("project").state == skills.CURRENT


def test_files_dropped_by_a_newer_release_are_removed(project, monkeypatch):
    monkeypatch.setattr(
        skills, "bundled_files", lambda: {"SKILL.md": b"a\n", "extra.md": b"b\n"}
    )
    skills.install("project")
    (skill_md().parent / "mine.md").write_text("keep me\n")
    monkeypatch.setattr(skills, "bundled_files", lambda: {"SKILL.md": b"a2\n"})
    # The user's own file doesn't make the copy "edited", and survives.
    assert skills.install("project") == skills.UPDATED
    assert not (skill_md().parent / "extra.md").exists()
    assert (skill_md().parent / "mine.md").exists()


def test_refresh_existing_never_creates_a_copy(project, monkeypatch):
    assert skills.refresh_existing() == []
    skills.install("global")
    monkeypatch.setattr(skills, "bundled_files", lambda: {"SKILL.md": b"# newer\n"})
    rows = skills.refresh_existing()
    assert [(r["scope"], r["action"]) for r in rows] == [("global", skills.UPDATED)]
    assert not skill_md("project").exists()


# -- CLI ----------------------------------------------------------------


def test_cli_skill_install_and_status(project):
    r = run("skill", "install", "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["action"] == skills.INSTALLED

    r = run("skill", "status", "--json")
    rows = json.loads(r.stdout)["skills"]
    states = {(s["agent"], s["scope"]): s["state"] for s in rows}
    assert states[("claude", "project")] == skills.CURRENT
    assert states[("claude", "global")] == skills.MISSING
    assert {s["agent"] for s in rows} == set(skills.agent_names())
    # The table only lists other agents where a copy exists.
    r = run("skill", "status")
    assert "claude" in r.stdout and "cursor" not in r.stdout


def test_cli_skill_install_refuses_to_overwrite_edits(project):
    run("skill", "install", "--global")
    skill_md("global").write_text("edited\n")
    r = run("skill", "install", "--global", "--json")
    assert r.exit_code == 1
    assert json.loads(r.stdout)["error"] == "skill_modified"
    assert run("skill", "install", "--global", "--force").exit_code == 0
    assert skills.status("global").state == skills.CURRENT


def test_init_refreshes_an_outdated_project_skill(project, monkeypatch):
    skills.install("project")
    monkeypatch.setattr(skills, "bundled_files", lambda: {"SKILL.md": b"# newer\n"})
    r = run(
        "init", "--base-url", "https://tracker.example.com/jira",
        "--no-auth", "--force", "--dir", str(project), "--json",
    )
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["skills_installed"] == ["jtr"]
    assert skill_md().read_bytes() == b"# newer\n"


# -- other agents -------------------------------------------------------


@pytest.mark.parametrize(
    "agent, local, home",
    [
        ("claude", ".claude/skills", None),  # home via CLAUDE_CONFIG_DIR
        ("codex", ".agents/skills", ".agents/skills"),
        ("agents", ".agents/skills", ".agents/skills"),  # alias of codex
        ("gemini", ".gemini/skills", ".gemini/skills"),
        ("copilot", ".github/skills", ".copilot/skills"),
        ("cursor", ".cursor/skills", ".cursor/skills"),
        ("opencode", ".opencode/skills", ".config/opencode/skills"),
    ],
)
def test_agent_dirs(project, isolated_config, agent, local, home):
    assert skills.project_dir(project, agent) == project / local / "jtr"
    if home:
        assert skills.global_dir(agent) == isolated_config / "home" / home / "jtr"


def test_opencode_global_dir_honours_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert skills.global_dir("opencode") == tmp_path / "xdg" / "opencode" / "skills" / "jtr"


def test_unknown_agent_is_rejected(project):
    with pytest.raises(ValueError, match="Unknown agent 'emacs'"):
        skills.resolve_agent("emacs")
    r = run("skill", "install", "--agent", "emacs", "--json")
    assert r.exit_code == 2
    assert json.loads(r.stdout)["error"] == "invalid_input"


def test_cli_install_for_another_agent(project):
    r = run("skill", "install", "--agent", "Cursor", "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["agent"] == "cursor"
    assert skill_md(agent="cursor").is_file()
    assert not skill_md().exists()
    # The fix hint for an edited copy reproduces the agent and scope flags.
    run("skill", "install", "--agent", "codex", "--global")
    skill_md("global", "codex").write_text("edited\n")
    r = run("skill", "install", "--agent", "codex", "--global", "--json")
    assert r.exit_code == 1
    assert json.loads(r.stdout)["fix"].startswith(
        "jtr skill install --agent codex --global --force"
    )


def test_refresh_existing_covers_every_agent(project, monkeypatch):
    skills.install("project", agent="gemini")
    skills.install("global", agent="opencode")
    monkeypatch.setattr(skills, "bundled_files", lambda: {"SKILL.md": b"# newer\n"})
    rows = skills.refresh_existing()
    assert [(r["agent"], r["scope"], r["action"]) for r in rows] == [
        ("gemini", "project", skills.UPDATED),
        ("opencode", "global", skills.UPDATED),
    ]
    assert not skill_md().exists()


def _init_args(project, *extra):
    return (
        "init", "--base-url", "https://tracker.example.com/jira",
        "--no-auth", "--force", "--dir", str(project), *extra,
    )


def test_init_agent_flag_picks_the_skill_dir(project):
    r = run(*_init_args(project, "--agent", "copilot", "--json"))
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["skills_installed"] == ["jtr"]
    assert out["skill_agent"] == "copilot"
    assert (project / ".github" / "skills" / "jtr" / "SKILL.md").is_file()
    assert not skill_md().exists()


def test_init_json_never_prompts_and_defaults_to_claude(project):
    r = run(*_init_args(project, "--json"))
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["skill_agent"] == "claude"
    assert skill_md().is_file()


def test_init_no_skills_reports_no_agent(project):
    r = run(*_init_args(project, "--no-skills", "--agent", "cursor", "--json"))
    assert json.loads(r.stdout)["skill_agent"] is None
    assert not skill_md(agent="cursor").exists()


def test_init_menu_lets_the_user_choose(project, monkeypatch):
    monkeypatch.setattr("jtr.cli._stdin_is_tty", lambda: True)
    # project key, then an out-of-range pick, then Cursor (5th in the menu).
    r = run(*_init_args(project), input="\n9\n5\n")
    assert r.exit_code == 0, r.output
    assert "Which agent should get the /jtr skill?" in r.output
    assert "Enter a number from 1 to" in r.output
    assert skill_md(agent="cursor").is_file()
    assert not skill_md().exists()


def test_init_menu_default_is_claude(project, monkeypatch):
    monkeypatch.setattr("jtr.cli._stdin_is_tty", lambda: True)
    r = run(*_init_args(project), input="\n\n")
    assert r.exit_code == 0, r.output
    assert skill_md().is_file()


def test_init_agent_flag_skips_the_menu(project, monkeypatch):
    monkeypatch.setattr("jtr.cli._stdin_is_tty", lambda: True)
    r = run(*_init_args(project, "--agent", "gemini"), input="\n")
    assert r.exit_code == 0, r.output
    assert "Which agent" not in r.output
    assert skill_md(agent="gemini").is_file()


def test_skill_install_menu_in_a_terminal(project, monkeypatch):
    monkeypatch.setattr("jtr.cli._stdin_is_tty", lambda: True)
    r = run("skill", "install", "--global", input="6\n")
    assert r.exit_code == 0, r.output
    assert "Which agent should get the /jtr skill?" in r.output
    assert skill_md("global", "opencode").is_file()
    assert not skill_md("global").exists()
    # --agent, --json, or a pipe: no menu.
    for args, stdin in ((("--agent", "cursor"), "\n"), (("--json",), "\n")):
        r = run("skill", "install", *args, input=stdin)
        assert r.exit_code == 0, r.output
        assert "Which agent" not in r.output
    monkeypatch.setattr("jtr.cli._stdin_is_tty", lambda: False)
    r = run("skill", "install")
    assert "Which agent" not in r.output and skill_md().is_file()
