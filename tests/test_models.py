from __future__ import annotations

from jtr.models import SearchPage, Ticket, User


def ticket(key="PROJ-1"):
    return Ticket.from_api({"key": key, "fields": {"summary": "S"}})


def test_offset_paging_reports_next_offset():
    page = SearchPage(tickets=[ticket()], start_at=0, max_results=1, total=3)
    assert page.next_start_at == 1
    assert page.has_more


def test_offset_paging_stops_at_the_end():
    page = SearchPage(tickets=[ticket()], start_at=2, max_results=1, total=3)
    assert page.next_start_at is None
    assert not page.has_more


def test_empty_page_has_no_next_offset():
    page = SearchPage(tickets=[], start_at=0, max_results=50, total=0)
    assert page.next_start_at is None
    assert not page.has_more


def test_cursor_paging_has_no_offset():
    """No total means no arithmetic is possible — only the token counts."""
    page = SearchPage(tickets=[ticket()], total=None, next_page_token="tok")
    assert page.next_start_at is None
    assert page.has_more


def test_cursor_last_page():
    page = SearchPage(tickets=[ticket()], total=None, next_page_token=None)
    assert not page.has_more


def test_user_from_server_payload():
    u = User.from_api({"name": "jdoe", "displayName": "J Doe", "key": "jdoe"})
    assert u.name == "jdoe"
    assert u.account_id == ""
    assert u.short() == "J Doe"


def test_user_from_cloud_payload():
    """Cloud sends no name/key at all — only accountId identifies the person."""
    u = User.from_api({
        "accountId": "557058:abc",
        "displayName": "J Doe",
        "emailAddress": "j@acme.com",
    })
    assert u.account_id == "557058:abc"
    assert u.name == ""
    assert u.short() == "J Doe"


def test_user_short_falls_back_to_email_on_cloud():
    u = User.from_api({"accountId": "1", "emailAddress": "j@acme.com"})
    assert u.short() == "j@acme.com"
