from __future__ import annotations

import json

from typer.testing import CliRunner

from jtr import config
from jtr.cli import app
from jtr.dialect import CLOUD, SERVER

runner = CliRunner()

CLOUD_URL = "https://acme.atlassian.net"
SERVER_URL = "https://tracker.example.com/jira"


def run(*args):
    return runner.invoke(app, list(args))


def configure(base_url, **values):
    config.set_value(config.KEY_BASE_URL, base_url)
    for key, value in values.items():
        config.set_value(key, value)


# -- init ---------------------------------------------------------------


def test_init_detects_cloud_and_reports_it():
    r = run("init", "--base-url", CLOUD_URL, "--no-auth", "--bare", "--force", "--json")
    assert r.exit_code == 0, r.output
    state = json.loads(r.stdout)
    assert state["deployment"] == CLOUD
    assert state["api_version"] == "2"


def test_init_detects_server_by_default():
    r = run("init", "--base-url", SERVER_URL, "--no-auth", "--bare", "--force", "--json")
    state = json.loads(r.stdout)
    assert state["deployment"] == SERVER


def test_init_deployment_override_is_stored():
    r = run(
        "init",
        "--base-url", "https://jira.acme.com",
        "--deployment", "cloud",
        "--no-auth", "--bare", "--force", "--json",
    )
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["deployment"] == CLOUD
    assert config.load().deployment == "cloud"


def test_init_rejects_unknown_deployment():
    r = run(
        "init", "--base-url", SERVER_URL, "--deployment", "nonsense",
        "--no-auth", "--bare", "--force", "--json",
    )
    assert r.exit_code != 0
    assert json.loads(r.stdout)["error"] == "invalid_input"


def test_init_json_will_not_prompt_for_cloud_credentials():
    """--json promises parseable stdout, so it must fail rather than prompt."""
    r = run("init", "--base-url", CLOUD_URL, "--auth", "token", "--bare", "--force", "--json")
    assert r.exit_code != 0
    assert json.loads(r.stdout)["error"] == "input_required"


def test_init_rejects_sso_on_cloud():
    r = run("init", "--base-url", CLOUD_URL, "--auth", "sso", "--bare", "--force", "--json")
    assert r.exit_code != 0
    assert "sso" in r.output.lower()


# -- config -------------------------------------------------------------


def test_config_deployment_roundtrip():
    configure(SERVER_URL)
    assert run("config", "deployment", "cloud").exit_code == 0
    assert config.load().deployment == "cloud"
    # `auto` clears the pin and goes back to detecting.
    assert run("config", "deployment", "auto").exit_code == 0
    assert not config.load().deployment


def test_config_deployment_rejects_v3_on_server():
    configure(SERVER_URL, **{config.KEY_API_VERSION: "3"})
    r = run("config", "deployment", "server", "--json")
    assert r.exit_code != 0
    assert json.loads(r.stdout)["error"] == "unsupported_deployment"


def test_config_show_json_includes_deployment():
    configure(CLOUD_URL, **{config.KEY_EMAIL: "me@acme.com"})
    state = json.loads(run("config", "show", "--json").stdout)
    assert state["deployment"] == CLOUD
    assert state["email"] == "me@acme.com"


def test_config_base_url_accepts_cloud():
    configure(SERVER_URL)
    r = run("config", "base-url", CLOUD_URL, "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["deployment"] == CLOUD


# -- paging guards ------------------------------------------------------


def test_start_at_is_refused_on_cloud():
    """Silently ignoring it would hand back page 1 while looking like paging."""
    configure(CLOUD_URL, **{config.KEY_PAT: "t", config.KEY_EMAIL: "me@acme.com"})
    r = run("search", "project = X", "--start-at", "50", "--json")
    assert r.exit_code != 0
    payload = json.loads(r.stdout)
    assert payload["error"] == "unsupported_option"
    assert "--cursor" in payload["fix"]


def test_cursor_is_refused_on_server():
    configure(SERVER_URL, **{config.KEY_PAT: "t"})
    r = run("search", "project = X", "--cursor", "tok", "--json")
    assert r.exit_code != 0
    assert json.loads(r.stdout)["error"] == "unsupported_option"


def test_start_at_zero_is_fine_on_cloud(monkeypatch):
    """The default must not trip the guard."""
    configure(CLOUD_URL, **{config.KEY_PAT: "t", config.KEY_EMAIL: "me@acme.com"})
    calls = {}

    class FakeClient:
        dialect = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def search(self, jql, **kwargs):
            calls.update(kwargs)
            from jtr.models import SearchPage

            return SearchPage(tickets=[], total=None)

    monkeypatch.setattr("jtr.cli.JiraClient.from_session", lambda: FakeClient())
    r = run("search", "project = X", "--json")
    assert r.exit_code == 0, r.output
    assert calls["cursor"] is None
    assert json.loads(r.stdout)["total"] is None
