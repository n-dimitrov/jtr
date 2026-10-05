from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from jtr import cli, skills
from jtr import update as update_mod
from jtr.cli import app

runner = CliRunner()


def run(*args):
    return runner.invoke(app, list(args))


def fake_head(location):
    def head(url, **kwargs):
        headers = {"location": location} if location else {}
        return httpx.Response(302 if location else 404, headers=headers)

    return head


def test_latest_version_reads_the_release_redirect(monkeypatch):
    monkeypatch.setattr(
        httpx, "head",
        fake_head("https://github.com/n-dimitrov/jtr/releases/tag/v1.4.2"),
    )
    assert update_mod.latest_version() == "1.4.2"


def test_latest_version_without_a_release_is_an_error(monkeypatch):
    # With no release, GitHub redirects to the bare /releases page.
    monkeypatch.setattr(
        httpx, "head", fake_head("https://github.com/n-dimitrov/jtr/releases")
    )
    with pytest.raises(update_mod.UpdateError):
        update_mod.latest_version()


def test_latest_version_wraps_network_errors(monkeypatch):
    def boom(url, **kwargs):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "head", boom)
    with pytest.raises(update_mod.UpdateError):
        update_mod.latest_version()


@pytest.mark.parametrize(
    "latest, installed, newer",
    [
        ("1.2.0", "1.1.0", True),
        ("1.10.0", "1.9.3", True),
        ("1.1.0", "1.1.0", False),
        ("1.1.0", "1.2.0", False),
        ("1.1.0", "0.0.0+dev", True),
    ],
)
def test_is_newer(latest, installed, newer):
    assert update_mod.is_newer(latest, installed) is newer


def test_wheel_url():
    assert update_mod.wheel_url("1.2.0") == (
        "https://github.com/n-dimitrov/jtr/releases/download/v1.2.0/"
        "jtr-1.2.0-py3-none-any.whl"
    )


# -- CLI ----------------------------------------------------------------


@pytest.fixture
def versions(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    def set_versions(installed, latest):
        monkeypatch.setattr(cli, "__version__", installed)
        monkeypatch.setattr(update_mod, "latest_version", lambda: latest)

    return set_versions


def test_update_check_reports_a_newer_release(versions):
    versions("1.1.0", "1.2.0")
    r = run("update", "--check", "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout) == {
        "installed": "1.1.0", "latest": "1.2.0", "update_available": True,
    }


def test_update_when_current_still_refreshes_skills(versions, monkeypatch):
    versions("1.2.0", "1.2.0")
    skills.install("global")
    monkeypatch.setattr(skills, "bundled_files", lambda: {"SKILL.md": b"# newer\n"})
    r = run("update", "--json")
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["updated"] is False
    assert [(s["scope"], s["action"]) for s in out["skills"]] == [
        ("global", skills.UPDATED)
    ]


def test_update_installs_then_refreshes_skills_with_the_new_jtr(versions, monkeypatch):
    versions("1.1.0", "1.2.0")
    calls = []
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(update_mod, "is_uv_tool_install", lambda: True)
    monkeypatch.setattr(update_mod, "install", lambda v: calls.append(("install", v)))
    monkeypatch.setattr(
        cli, "_refresh_skills_with_new_jtr",
        lambda *, json_out: calls.append(("skills",)) or [],
    )
    r = run("update", "--json")
    assert r.exit_code == 0, r.output
    assert calls == [("install", "1.2.0"), ("skills",)]
    assert json.loads(r.stdout)["updated"] is True


def test_update_refuses_to_replace_a_non_uv_install(versions, monkeypatch):
    versions("1.1.0", "1.2.0")
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(update_mod, "is_uv_tool_install", lambda: False)
    monkeypatch.setattr(update_mod, "install", lambda v: pytest.fail("must not install"))
    r = run("update", "--json")
    assert r.exit_code == 1
    assert json.loads(r.stdout)["error"] == "manual_update_required"


def test_update_on_windows_points_at_the_installer(versions, monkeypatch):
    versions("1.1.0", "1.2.0")
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(update_mod, "install", lambda v: pytest.fail("must not install"))
    r = run("update", "--json")
    assert r.exit_code == 1
    out = json.loads(r.stdout)
    assert out["error"] == "manual_update_required"
    assert "install.ps1" in out["fix"]


def test_update_check_failure_is_reported(monkeypatch):
    def boom():
        raise update_mod.UpdateError("Couldn't reach github.com: no route")

    monkeypatch.setattr(update_mod, "latest_version", boom)
    r = run("update", "--json")
    assert r.exit_code == 1
    assert json.loads(r.stdout)["error"] == "update_check_failed"
