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


# -- create -------------------------------------------------------------

ISSUE_TYPES = [
    {"id": "3", "name": "Task", "subtask": False},
    {"id": "1", "name": "Bug", "subtask": False},
    {"id": "5", "name": "Sub-task", "subtask": True},
]


def stub_jira(monkeypatch, *, url=SERVER_URL, issue_types=ISSUE_TYPES, create=None):
    """Route the CLI's client at a fake Jira; returns the POSTed create bodies."""
    import httpx

    from jtr.client import JiraClient
    from jtr.dialect import Dialect

    created: list[dict] = []

    def handler(request):
        path = request.url.path
        if request.method == "POST" and path.endswith("/issue"):
            created.append(json.loads(request.content))
            return create or httpx.Response(201, json={"key": "PROJ-7"})
        if "/project/" in path:
            payload = {"key": "PROJ", "issueTypes": issue_types}
        elif path.endswith("/user/search"):
            payload = [{"accountId": "557058:abc", "emailAddress": "jd@acme.com"}]
        else:
            payload = {"key": "PROJ-1", "fields": {"project": {"key": "PROJ"}}}
        return httpx.Response(200, json=payload)

    def from_session():
        http = httpx.Client(base_url=url, transport=httpx.MockTransport(handler))
        return JiraClient(http, Dialect.resolve(url))

    monkeypatch.setattr("jtr.cli.JiraClient.from_session", from_session)
    return created


def test_create_defaults_to_task_in_the_configured_project(monkeypatch):
    configure(SERVER_URL, **{config.KEY_PAT: "t", config.KEY_PROJECT: "PROJ"})
    created = stub_jira(monkeypatch)
    r = run("create", "Fix the thing", "-d", "Details", "--labels", "a, b",
            "--priority", "High", "--assignee", "jdoe", "--yes", "--json")
    assert r.exit_code == 0, r.output
    assert created == [{"fields": {
        "project": {"key": "PROJ"},
        "summary": "Fix the thing",
        "issuetype": {"id": "3"},
        "description": "Details",
        "labels": ["a", "b"],
        "priority": {"name": "High"},
        "assignee": {"name": "jdoe"},
    }}]
    payload = json.loads(r.stdout)
    assert payload["action"] == "create"
    assert payload["key"] == "PROJ"
    assert payload["created"] == payload["result"] == "PROJ-7"
    assert payload["url"] == f"{SERVER_URL}/browse/PROJ-7"


def test_create_with_parent_makes_a_subtask_in_the_parents_project(monkeypatch):
    """No project configured: the parent says where the sub-task goes."""
    configure(SERVER_URL, **{config.KEY_PAT: "t"})
    created = stub_jira(monkeypatch)
    r = run("create", "Child", "--parent", "PROJ-1", "--yes", "--json")
    assert r.exit_code == 0, r.output
    assert created[0]["fields"] == {
        "project": {"key": "PROJ"},
        "summary": "Child",
        "issuetype": {"id": "5"},
        "parent": {"key": "PROJ-1"},
    }
    assert json.loads(r.stdout)["key"] == "PROJ-1"


def test_create_type_is_matched_case_insensitively(monkeypatch):
    configure(SERVER_URL, **{config.KEY_PAT: "t"})
    created = stub_jira(monkeypatch)
    r = run("create", "S", "--project", "PROJ", "--type", "bug", "--yes", "--json")
    assert r.exit_code == 0, r.output
    assert created[0]["fields"]["issuetype"] == {"id": "1"}


def test_create_refuses_a_subtask_type_without_a_parent(monkeypatch):
    configure(SERVER_URL, **{config.KEY_PAT: "t", config.KEY_PROJECT: "PROJ"})
    created = stub_jira(monkeypatch)
    r = run("create", "S", "--type", "Sub-task", "--yes", "--json")
    assert r.exit_code != 0
    payload = json.loads(r.stdout)
    assert payload["error"] == "unknown_issue_type"
    assert "'Task', 'Bug'" in payload["fix"]
    assert created == []


def test_create_will_not_guess_between_several_subtask_types(monkeypatch):
    configure(SERVER_URL, **{config.KEY_PAT: "t"})
    types = [
        {"id": "8", "name": "Dev Sub-task", "subtask": True},
        {"id": "9", "name": "QA Sub-task", "subtask": True},
    ]
    created = stub_jira(monkeypatch, issue_types=types)
    r = run("create", "S", "--parent", "PROJ-1", "--yes", "--json")
    assert r.exit_code != 0
    assert json.loads(r.stdout)["error"] == "unknown_issue_type"
    assert created == []


def test_create_needs_a_project(monkeypatch):
    configure(SERVER_URL, **{config.KEY_PAT: "t"})
    created = stub_jira(monkeypatch)
    r = run("create", "S", "--yes", "--json")
    assert r.exit_code != 0
    assert json.loads(r.stdout)["error"] == "invalid_input"
    assert created == []


def test_create_field_values_are_sent_as_json_when_they_parse(monkeypatch):
    configure(SERVER_URL, **{config.KEY_PAT: "t", config.KEY_PROJECT: "PROJ"})
    created = stub_jira(monkeypatch)
    r = run("create", "S", "-f", 'customfield_1={"value": "Blue"}',
            "-f", "customfield_2=plain text", "-f", "customfield_3=5", "--yes", "--json")
    assert r.exit_code == 0, r.output
    fields = created[0]["fields"]
    assert fields["customfield_1"] == {"value": "Blue"}
    assert fields["customfield_2"] == "plain text"
    assert fields["customfield_3"] == 5


def test_create_rejects_a_malformed_field(monkeypatch):
    configure(SERVER_URL, **{config.KEY_PAT: "t", config.KEY_PROJECT: "PROJ"})
    created = stub_jira(monkeypatch)
    r = run("create", "S", "-f", "novalue", "--yes", "--json")
    assert r.exit_code != 0
    assert json.loads(r.stdout)["error"] == "invalid_input"
    assert created == []


def test_create_points_at_field_when_jira_demands_more(monkeypatch):
    import httpx

    configure(SERVER_URL, **{config.KEY_PAT: "t", config.KEY_PROJECT: "PROJ"})
    stub_jira(monkeypatch, create=httpx.Response(
        400, json={"errors": {"customfield_1": "Team is required."}}
    ))
    r = run("create", "S", "--yes", "--json")
    assert r.exit_code != 0
    payload = json.loads(r.stdout)
    assert payload["error"] == "jira_error"
    assert "customfield_1: Team is required." in payload["message"]
    assert "--field" in payload["fix"]


def test_create_on_cloud_resolves_the_assignee_to_an_account_id(monkeypatch):
    configure(CLOUD_URL, **{
        config.KEY_PAT: "t", config.KEY_EMAIL: "me@acme.com", config.KEY_PROJECT: "PROJ",
    })
    created = stub_jira(monkeypatch, url=CLOUD_URL)
    r = run("create", "S", "--assignee", "jd@acme.com", "--yes", "--json")
    assert r.exit_code == 0, r.output
    assert created[0]["fields"]["assignee"] == {"accountId": "557058:abc"}


def test_create_asks_before_posting_and_no_means_no(monkeypatch):
    configure(SERVER_URL, **{config.KEY_PAT: "t", config.KEY_PROJECT: "PROJ"})
    created = stub_jira(monkeypatch)
    r = runner.invoke(app, ["create", "S"], input="n\n")
    assert r.exit_code == 0
    assert "Summary: S" in r.output
    assert created == []


def test_create_is_audited(monkeypatch, isolated_config):
    configure(SERVER_URL, **{config.KEY_PAT: "t", config.KEY_PROJECT: "PROJ"})
    stub_jira(monkeypatch)
    assert run("create", "S", "--yes").exit_code == 0
    row = json.loads(config.audit_path().read_text().splitlines()[-1])
    assert (row["action"], row["key"], row["ok"], row["result"]) == (
        "create", "PROJ", True, "PROJ-7",
    )


def test_issuetypes_lists_them_as_json(monkeypatch):
    configure(SERVER_URL, **{config.KEY_PAT: "t"})
    stub_jira(monkeypatch)
    r = run("issuetypes", "PROJ", "--json")
    assert r.exit_code == 0, r.output
    payload = json.loads(r.stdout)
    assert payload["project"] == "PROJ"
    assert payload["count"] == 3
    assert payload["issue_types"][2] == {
        "id": "5", "name": "Sub-task", "subtask": True, "description": "",
    }


# -- transition ---------------------------------------------------------

TRANSITIONS = [
    {"id": "11", "name": "Start Progress", "to": {"name": "In Progress"}},
    {
        "id": "21",
        "name": "Close Issue",
        "to": {"name": "Closed"},
        "fields": {
            "resolution": {
                "required": True,
                "allowedValues": [{"name": "Fixed"}, {"name": "Won't Fix"}],
            },
            "customfield_9": {"required": False},
        },
    },
]


def stub_workflow(monkeypatch, *, status="Open", transitions=TRANSITIONS, post=None):
    """A fake Jira with one ticket, P-1; returns the POSTed transition bodies."""
    import httpx

    from jtr.client import JiraClient
    from jtr.dialect import Dialect

    posted: list[dict] = []
    seen = {}

    def handler(request):
        if request.method == "POST":
            posted.append(json.loads(request.content))
            return post or httpx.Response(204)
        if request.url.path.endswith("/transitions"):
            seen["expand"] = request.url.params.get("expand")
            return httpx.Response(200, json={"transitions": transitions})
        return httpx.Response(
            200, json={"key": "P-1", "fields": {"status": {"name": status}}}
        )

    def from_session():
        http = httpx.Client(base_url=SERVER_URL, transport=httpx.MockTransport(handler))
        return JiraClient(http, Dialect.resolve(SERVER_URL))

    monkeypatch.setattr("jtr.cli.JiraClient.from_session", from_session)
    configure(SERVER_URL, **{config.KEY_PAT: "t"})
    posted.append(seen)
    return posted


def test_transition_posts_the_matching_transition(monkeypatch):
    seen, *posted = stub_workflow(monkeypatch)
    r = run("transition", "P-1", "in progress", "--yes", "--json")
    assert r.exit_code == 0, r.output
    payload = json.loads(r.stdout)
    assert payload["before"] == {"status": "Open"}
    assert payload["after"]["status"] == "In Progress"
    assert payload["after"]["fields"] is None


def test_transition_sends_only_the_transition_by_default(monkeypatch):
    posted = stub_workflow(monkeypatch)
    assert run("transition", "P-1", "progress", "-m", "on it", "--yes").exit_code == 0
    assert posted[1:] == [{
        "transition": {"id": "11"},
        "update": {"comment": [{"add": {"body": "on it"}}]},
    }]


def test_close_sends_resolution_and_extra_fields(monkeypatch):
    posted = stub_workflow(monkeypatch)
    r = run("transition", "P-1", "Closed", "--resolution", "Fixed",
            "-f", 'customfield_9={"value": "x"}', "--yes", "--json")
    assert r.exit_code == 0, r.output
    assert posted[1:] == [{
        "transition": {"id": "21"},
        "fields": {"resolution": {"name": "Fixed"}, "customfield_9": {"value": "x"}},
    }]
    assert json.loads(r.stdout)["after"]["fields"]["resolution"] == {"name": "Fixed"}


def test_close_without_a_required_resolution_says_how_to_give_one(monkeypatch):
    import httpx

    stub_workflow(monkeypatch, post=httpx.Response(
        400, json={"errors": {"resolution": "Resolution is required."}}
    ))
    r = run("transition", "P-1", "Closed", "--yes", "--json")
    assert r.exit_code != 0
    payload = json.loads(r.stdout)
    assert payload["error"] == "jira_error"
    assert "--resolution" in payload["fix"]
    assert "Fixed, Won't Fix" in payload["fix"]


def test_transition_list_reports_what_each_one_requires(monkeypatch):
    seen, *_ = stub_workflow(monkeypatch)
    r = run("transition", "P-1", "--json")
    assert r.exit_code == 0, r.output
    assert seen["expand"] == "transitions.fields"
    close = json.loads(r.stdout)["transitions"][1]
    assert close == {
        "id": "21",
        "name": "Close Issue",
        "to_status": "Closed",
        "required_fields": ["resolution"],
        "resolutions": ["Fixed", "Won't Fix"],
    }


def test_transition_to_the_current_status_is_a_no_op(monkeypatch):
    posted = stub_workflow(monkeypatch, status="Closed", transitions=[])
    r = run("transition", "P-1", "closed", "--yes", "--json")
    assert r.exit_code == 0, r.output
    payload = json.loads(r.stdout)
    assert payload["changed"] is False
    assert payload["before"] == {"status": "Closed"}
    assert posted[1:] == []


def test_transition_with_nowhere_to_go_is_an_error_not_a_success(monkeypatch):
    """Exiting 0 here would tell a script the ticket was closed."""
    posted = stub_workflow(monkeypatch, status="Resolved", transitions=[])
    r = run("transition", "P-1", "Closed", "--yes", "--json")
    assert r.exit_code != 0
    payload = json.loads(r.stdout)
    assert payload["error"] == "no_transition_match"
    assert "'Resolved'" in payload["message"]
    assert posted[1:] == []


def test_transition_list_is_empty_but_fine_when_there_are_none(monkeypatch):
    stub_workflow(monkeypatch, transitions=[])
    r = run("transition", "P-1", "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout) == {"key": "P-1", "transitions": []}
