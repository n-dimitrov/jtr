from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from jtr import skills
from jtr.cli import app

runner = CliRunner()


def run(*args):
    return runner.invoke(app, list(args))


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


def skill_md(scope="project"):
    return skills.scope_dir(scope) / "SKILL.md"


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
    states = {s["scope"]: s["state"] for s in json.loads(r.stdout)["skills"]}
    assert states == {"project": skills.CURRENT, "global": skills.MISSING}


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
