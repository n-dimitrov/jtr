from __future__ import annotations

import base64

import pytest

from jtr.dialect import CLOUD, SERVER, Dialect, DialectError, detect_deployment
from jtr.models import User


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://acme.atlassian.net", CLOUD),
        ("https://acme.atlassian.net/", CLOUD),
        ("https://ACME.ATLASSIAN.NET", CLOUD),
        ("https://acme.jira.com", CLOUD),
        ("https://tracker.example.com/jira", SERVER),
        ("https://jira.internal.corp", SERVER),
        # Not a Cloud host — merely containing the string isn't enough.
        ("https://atlassian.net.example.com/jira", SERVER),
    ],
)
def test_detect_deployment_from_hostname(url, expected):
    assert detect_deployment(url) == expected


def test_explicit_deployment_beats_detection():
    """A Cloud tenant on a vanity domain is exactly why the override exists."""
    d = Dialect.resolve("https://jira.acme.com", deployment="cloud")
    assert d.is_cloud


def test_auto_and_blank_mean_detect():
    for value in ("auto", "", "  "):
        assert Dialect.resolve("https://acme.atlassian.net", deployment=value).is_cloud


def test_unknown_deployment_rejected():
    with pytest.raises(DialectError):
        Dialect.resolve("https://x.example.com", deployment="onprem-ish")


def test_v3_on_server_is_rejected():
    """Server/DC has no v3 at all, so this must fail loudly, not 404 later."""
    with pytest.raises(DialectError, match="no REST API v3"):
        Dialect.resolve("https://tracker.example.com/jira", api_version="3")


def test_v3_allowed_on_cloud():
    d = Dialect.resolve("https://acme.atlassian.net", api_version="3")
    assert d.api_version == "3"
    assert d.uses_adf


def test_default_version_is_2_on_both():
    assert Dialect.resolve("https://acme.atlassian.net").api_version == "2"
    assert Dialect.resolve("https://t.example.com/jira").api_version == "2"
    assert not Dialect.resolve("https://acme.atlassian.net").uses_adf


def test_paths_by_deployment():
    server = Dialect.resolve("https://t.example.com/jira")
    cloud = Dialect.resolve("https://acme.atlassian.net")

    assert server.search_path == "/rest/api/2/search"
    assert cloud.search_path == "/rest/api/2/search/jql"
    assert server.projects_path == "/rest/api/2/project"
    assert cloud.projects_path == "/rest/api/2/project/search"
    assert server.api("/issue/PROJ-1") == "/rest/api/2/issue/PROJ-1"


def test_sso_only_on_server():
    assert Dialect.resolve("https://t.example.com/jira").supports_sso
    assert not Dialect.resolve("https://acme.atlassian.net").supports_sso


def test_auth_header_server_is_bearer():
    d = Dialect.resolve("https://t.example.com/jira")
    assert d.auth_headers(token="abc", email=None) == {"Authorization": "Bearer abc"}


def test_auth_header_cloud_is_basic():
    d = Dialect.resolve("https://acme.atlassian.net")
    headers = d.auth_headers(token="tok", email="me@acme.com")
    scheme, _, value = headers["Authorization"].partition(" ")
    assert scheme == "Basic"
    assert base64.b64decode(value).decode() == "me@acme.com:tok"


def test_cloud_without_email_sends_no_header():
    """Half a Basic credential is worse than none — it can't be diagnosed."""
    d = Dialect.resolve("https://acme.atlassian.net")
    assert d.auth_headers(token="tok", email=None) == {}


def test_no_token_sends_no_header():
    d = Dialect.resolve("https://t.example.com/jira")
    assert d.auth_headers(token=None, email=None) == {}


def test_assignee_payload_field_differs():
    server = Dialect.resolve("https://t.example.com/jira")
    cloud = Dialect.resolve("https://acme.atlassian.net")
    assert server.assignee_payload("jdoe") == {"name": "jdoe"}
    assert cloud.assignee_payload("557058:abc") == {"accountId": "557058:abc"}
    assert server.assignee_payload(None) == {"name": None}
    assert cloud.assignee_payload(None) == {"accountId": None}


def test_user_identifier_matches_assignee_payload():
    """These two must agree, or every assign looks like a change."""
    server = Dialect.resolve("https://t.example.com/jira")
    cloud = Dialect.resolve("https://acme.atlassian.net")
    server_user = User(name="jdoe", display_name="J Doe")
    cloud_user = User(name="", display_name="J Doe", account_id="557058:abc")

    assert server.user_identifier(server_user) == "jdoe"
    assert cloud.user_identifier(cloud_user) == "557058:abc"
    assert server.user_identifier(None) == ""
    assert cloud.user_identifier(None) == ""


def test_search_params_server_uses_offsets():
    d = Dialect.resolve("https://t.example.com/jira")
    params = d.search_params("project = X", fields=["summary"], limit=50, start_at=100)
    assert params["startAt"] == "100"
    assert params["maxResults"] == "50"
    assert "nextPageToken" not in params


def test_search_params_cloud_uses_cursor_and_caps_limit():
    d = Dialect.resolve("https://acme.atlassian.net")
    params = d.search_params(
        "project = X", fields=["summary"], limit=500, start_at=100, cursor="tok"
    )
    assert "startAt" not in params
    assert params["nextPageToken"] == "tok"
    # Cloud rejects anything above 100 for search.
    assert params["maxResults"] == "100"


def test_search_params_cloud_first_page_has_no_cursor():
    d = Dialect.resolve("https://acme.atlassian.net")
    params = d.search_params("project = X", fields=["summary"], limit=10)
    assert "nextPageToken" not in params
