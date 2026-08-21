"""Client behaviour against both dialects, with the network stubbed out.

Every test asserts on the request jtr actually builds, because the whole
Cloud/Server split lives in the difference between those requests.
"""

from __future__ import annotations

import httpx
import pytest

from jtr.client import AmbiguousUser, JiraClient, UserNotFound
from jtr.dialect import Dialect

SERVER_URL = "https://tracker.example.com/jira"
CLOUD_URL = "https://acme.atlassian.net"


def make_client(handler, *, url=SERVER_URL, **dialect_kwargs) -> JiraClient:
    dialect = Dialect.resolve(url, **dialect_kwargs)
    http = httpx.Client(base_url=url, transport=httpx.MockTransport(handler))
    return JiraClient(http, dialect)


def json_response(payload, status=200, headers=None):
    return httpx.Response(
        status, json=payload, headers={"content-type": "application/json", **(headers or {})}
    )


def issue(key="PROJ-1", **fields):
    base = {
        "summary": "S",
        "status": {"name": "Open"},
        "assignee": None,
        "labels": [],
    }
    base.update(fields)
    return {"key": key, "fields": base}


# -- Search ------------------------------------------------------------


def test_server_search_uses_offset_endpoint_and_reports_total():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return json_response(
            {"issues": [issue()], "startAt": 0, "maxResults": 50, "total": 137}
        )

    page = make_client(handler).search("project = X", limit=50)

    assert "/rest/api/2/search?" in seen["url"]
    assert "startAt=0" in seen["url"]
    assert page.total == 137
    assert page.next_start_at == 1
    assert page.next_page_token is None
    assert page.has_more


def test_cloud_search_uses_jql_endpoint_and_cursor():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return json_response({"issues": [issue()], "nextPageToken": "tok2"})

    page = make_client(handler, url=CLOUD_URL).search("project = X")

    assert "/rest/api/2/search/jql?" in seen["url"]
    assert "startAt" not in seen["url"]
    assert page.total is None
    assert page.next_start_at is None
    assert page.next_page_token == "tok2"
    assert page.has_more


def test_cloud_search_last_page_has_no_cursor():
    """isLast means stop, even when a token is echoed back."""

    def handler(request):
        return json_response(
            {"issues": [issue()], "nextPageToken": "tok", "isLast": True}
        )

    page = make_client(handler, url=CLOUD_URL).search("project = X")
    assert page.next_page_token is None
    assert not page.has_more


def test_cloud_search_forwards_cursor():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return json_response({"issues": []})

    make_client(handler, url=CLOUD_URL).search("project = X", cursor="abc")
    assert "nextPageToken=abc" in seen["url"]


def test_search_requests_explicit_fields():
    """Cloud's /search/jql returns only `id` unless fields are named."""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return json_response({"issues": []})

    make_client(handler, url=CLOUD_URL).search("project = X")
    assert "fields=" in seen["url"]
    assert "summary" in seen["url"]


# -- Assignment --------------------------------------------------------


def test_server_assign_sends_name():
    seen = {}

    def handler(request):
        seen["body"] = request.read().decode()
        return httpx.Response(204)

    make_client(handler).assign("PROJ-1", "jdoe")
    assert '"name"' in seen["body"] and "jdoe" in seen["body"]


def test_cloud_assign_sends_account_id():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode()
        return httpx.Response(204)

    make_client(handler, url=CLOUD_URL).assign("PROJ-1", "557058:abc")
    assert "/rest/api/2/issue/PROJ-1/assignee" in seen["url"]
    assert '"accountId"' in seen["body"]


def test_unassign_sends_null_not_automatic():
    seen = {}

    def handler(request):
        seen["body"] = request.read().decode()
        return httpx.Response(204)

    make_client(handler, url=CLOUD_URL).assign("PROJ-1", None)
    assert "null" in seen["body"]
    # -1 would mean "automatic assignee", which is a different outcome.
    assert "-1" not in seen["body"]


# -- User resolution ---------------------------------------------------


def test_resolve_assignee_is_noop_on_server():
    """The username is the identifier, so this must not spend a request."""

    def handler(request):  # pragma: no cover - must never be called
        raise AssertionError(f"unexpected request to {request.url}")

    assert make_client(handler).resolve_assignee("jdoe") == "jdoe"


def test_resolve_assignee_looks_up_email_on_cloud():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return json_response(
            [{"accountId": "557058:abc", "displayName": "J Doe",
              "emailAddress": "j@acme.com"}]
        )

    got = make_client(handler, url=CLOUD_URL).resolve_assignee("j@acme.com")
    assert got == "557058:abc"
    assert "/rest/api/2/user/search?" in seen["url"]
    assert "query=" in seen["url"]


def test_resolve_assignee_passes_through_account_id():
    def handler(request):  # pragma: no cover - must never be called
        raise AssertionError("should not look up an accountId")

    client = make_client(handler, url=CLOUD_URL)
    assert client.resolve_assignee("557058:abc-def") == "557058:abc-def"


def test_resolve_assignee_ambiguous_raises():
    def handler(request):
        return json_response([
            {"accountId": "1", "displayName": "J Doe", "emailAddress": "a@x.com"},
            {"accountId": "2", "displayName": "J Doer", "emailAddress": "b@x.com"},
        ])

    with pytest.raises(AmbiguousUser):
        make_client(handler, url=CLOUD_URL).resolve_assignee("J Do")


def test_resolve_assignee_exact_email_wins_over_partial():
    def handler(request):
        return json_response([
            {"accountId": "1", "displayName": "J Doe", "emailAddress": "j@acme.com"},
            {"accountId": "2", "displayName": "J Doer", "emailAddress": "j2@acme.com"},
        ])

    got = make_client(handler, url=CLOUD_URL).resolve_assignee("j@acme.com")
    assert got == "1"


def test_resolve_assignee_not_found_raises():
    def handler(request):
        return json_response([])

    with pytest.raises(UserNotFound):
        make_client(handler, url=CLOUD_URL).resolve_assignee("nobody@acme.com")


def test_server_user_search_uses_username_param():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return json_response([])

    make_client(handler).search_users("jdoe")
    assert "username=jdoe" in seen["url"]


# -- Projects ----------------------------------------------------------


def test_server_projects_is_a_flat_list():
    def handler(request):
        assert "/rest/api/2/project" in str(request.url)
        return json_response([
            {"key": "A", "name": "Alpha", "lead": {"displayName": "Lead A"}}
        ])

    projects = make_client(handler).projects()
    assert [p.key for p in projects] == ["A"]
    assert projects[0].lead == "Lead A"


def test_cloud_projects_walks_all_pages():
    pages = [
        {"values": [{"key": "A", "name": "Alpha"}], "isLast": False},
        {"values": [{"key": "B", "name": "Beta"}], "isLast": True},
    ]
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return json_response(pages[len(calls) - 1])

    projects = make_client(handler, url=CLOUD_URL).projects()
    assert [p.key for p in projects] == ["A", "B"]
    assert "/rest/api/2/project/search" in calls[0]
    assert "startAt=1" in calls[1]


# -- Transport behaviour -----------------------------------------------


def test_429_is_retried_honouring_retry_after(monkeypatch):
    monkeypatch.setattr("jtr.client.time.sleep", lambda _: None)
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return json_response({"issues": [], "total": 0})

    page = make_client(handler, url=CLOUD_URL).search("project = X")
    assert len(calls) == 2
    assert page.tickets == []


def test_429_eventually_surfaces_as_an_error(monkeypatch):
    monkeypatch.setattr("jtr.client.time.sleep", lambda _: None)

    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "1"})

    from jtr.client import JiraError

    with pytest.raises(JiraError) as exc:
        make_client(handler).search("project = X")
    assert exc.value.status == 429


def test_401_raises_session_expired_with_deployment_specific_fix():
    from jtr import auth

    def handler(request):
        return httpx.Response(401)

    with pytest.raises(auth.SessionExpired, match="jtr auth token"):
        make_client(handler, url=CLOUD_URL).myself()

    with pytest.raises(auth.SessionExpired, match="jtr auth pat"):
        make_client(handler).myself()
