from __future__ import annotations

import time

import httpx

from . import auth
from .dialect import Dialect
from .models import Comment, Project, SearchPage, Ticket, Transition

DEFAULT_LIST_FIELDS = [
    "summary",
    "status",
    "priority",
    "issuetype",
    "assignee",
    "reporter",
    "created",
    "updated",
    "project",
    "labels",
    "fixVersions",
]

# Cloud enforces per-tenant rate limits and expects clients to back off.
_RATE_LIMIT_RETRIES = 3
_RATE_LIMIT_MAX_WAIT = 30.0


class JiraError(Exception):
    """HTTP 4xx/5xx with a parsed Jira error payload, if available."""

    def __init__(self, status: int, message: str, payload: dict | None = None):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


class AmbiguousUser(Exception):
    """A user query matched more than one account."""

    def __init__(self, query: str, candidates: list):
        self.query = query
        self.candidates = candidates
        names = ", ".join(
            f"{u.short()} ({u.account_id or u.name})" for u in candidates[:5]
        )
        super().__init__(f"'{query}' matches {len(candidates)} users: {names}")


class UserNotFound(Exception):
    pass


class JiraClient:
    def __init__(self, http: httpx.Client, dialect: Dialect | None = None):
        self._http = http
        # Server/DC is the historical behaviour, so it's the fallback for
        # any caller that constructs a client without saying which Jira.
        self.dialect = dialect or Dialect.resolve(str(http.base_url))

    @classmethod
    def from_session(cls) -> JiraClient:
        http, dialect = auth.session()
        return cls(http, dialect)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> JiraClient:
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def _api(self, path: str) -> str:
        return self.dialect.api(path)

    def _check_auth(self, r: httpx.Response) -> None:
        if r.status_code in (401, 403) or 300 <= r.status_code < 400:
            fix = (
                "Run `jtr auth token`."
                if self.dialect.is_cloud
                else "Run `jtr auth pat` or `jtr auth sso`."
            )
            raise auth.SessionExpired(f"Auth failed (HTTP {r.status_code}). {fix}")

    def _check_status(self, r: httpx.Response) -> None:
        if r.status_code < 400:
            return
        payload: dict = {}
        msg = f"HTTP {r.status_code}"
        try:
            body = r.json()
            if isinstance(body, dict):
                payload = body
                msgs = body.get("errorMessages") or []
                errs = body.get("errors") or {}
                parts = list(msgs) + [f"{k}: {v}" for k, v in errs.items()]
                if parts:
                    msg = f"HTTP {r.status_code}: {'; '.join(parts)}"
        except Exception:
            snippet = (r.text or "").strip()[:200]
            if snippet:
                msg = f"HTTP {r.status_code}: {snippet}"
        raise JiraError(r.status_code, msg, payload)

    def _retry_after(self, r: httpx.Response) -> float | None:
        """Seconds to wait before retrying a throttled request, if we should.

        Cloud answers 429 with `Retry-After`. Honour it, but cap the wait:
        a CLI that silently sleeps for minutes reads as a hang.
        """
        if r.status_code != 429:
            return None
        raw = r.headers.get("Retry-After", "")
        try:
            wait = float(raw)
        except ValueError:
            wait = 1.0
        return min(max(wait, 0.5), _RATE_LIMIT_MAX_WAIT)

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        for attempt in range(_RATE_LIMIT_RETRIES):
            r = self._http.request(method, path, **kwargs)
            wait = self._retry_after(r)
            if wait is None or attempt == _RATE_LIMIT_RETRIES - 1:
                return r
            time.sleep(wait)
        return r  # pragma: no cover - loop always returns

    def _get(self, path: str, **kwargs):
        r = self._request("GET", path, **kwargs)
        self._check_auth(r)
        self._check_status(r)
        ct = (r.headers.get("content-type") or "").lower()
        if "application/json" not in ct:
            body = r.text or ""
            if auth.gateway_page(body):
                raise auth.GatewayIntercepted(
                    "Not authenticated — the WebSEAL gateway intercepted the "
                    "request. SSO cookies are missing or expired.\n"
                    "Fix: jtr auth sso   "
                    "(or connect to VPN if you only use a PAT)."
                )
            raise auth.SessionExpired(
                f"Unexpected non-JSON response (status={r.status_code} "
                f"ct={ct.split(';')[0]}). Check `jtr config show` — the base "
                "URL may be missing the Jira context path (e.g. /jira)."
            )
        return r.json()

    def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        r = self._request(method, path, **kwargs)
        self._check_auth(r)
        self._check_status(r)
        return r

    def myself(self) -> dict:
        return self._get(self.dialect.myself_path)

    def search(
        self,
        jql: str,
        fields: list[str] | None = None,
        limit: int = 50,
        start_at: int = 0,
        cursor: str | None = None,
    ) -> SearchPage:
        """One page of results.

        Server/DC pages by offset and reports a total. Cloud's
        `/search/jql` pages by opaque cursor and reports no total at all —
        `SearchPage.total` is None there, and `next_page_token` is what the
        caller feeds back in to get the following page.
        """
        params = self.dialect.search_params(
            jql,
            fields=fields or DEFAULT_LIST_FIELDS,
            limit=limit,
            start_at=start_at,
            cursor=cursor,
        )
        data = self._get(self.dialect.search_path, params=params)
        tickets = [Ticket.from_api(issue) for issue in data.get("issues", [])]
        if self.dialect.is_cloud:
            # `isLast` is advisory and absent on some responses; the token's
            # presence is the authoritative "there is more".
            token = data.get("nextPageToken")
            return SearchPage(
                tickets=tickets,
                start_at=0,
                max_results=len(tickets),
                total=None,
                next_page_token=token if token and not data.get("isLast") else None,
            )
        return SearchPage(
            tickets=tickets,
            start_at=int(data.get("startAt", start_at)),
            # Jira may return fewer than asked; report what it actually used.
            max_results=int(data.get("maxResults", limit)),
            total=int(data.get("total", len(tickets))),
        )

    def projects(self) -> list[Project]:
        if self.dialect.is_cloud:
            return self._projects_paged()
        # `expand=lead` is what makes the lead column non-empty on Server/DC.
        data = self._get(self.dialect.projects_path, params={"expand": "lead"})
        rows = data if isinstance(data, list) else data.get("values", [])
        return [Project.from_api(p) for p in rows]

    def _projects_paged(self) -> list[Project]:
        """Cloud's /project/search is paginated; walk it to the end."""
        out: list[Project] = []
        start = 0
        while True:
            data = self._get(
                self.dialect.projects_path,
                params={"startAt": str(start), "maxResults": "50", "expand": "lead"},
            )
            rows = data.get("values", []) if isinstance(data, dict) else []
            out.extend(Project.from_api(p) for p in rows)
            if not rows or (isinstance(data, dict) and data.get("isLast", True)):
                return out
            start += len(rows)

    def get_issue(self, key: str) -> Ticket:
        return Ticket.from_api(self._get(self._api(f"/issue/{key}")))

    def get_comments(self, key: str) -> list[Comment]:
        data = self._get(self._api(f"/issue/{key}/comment"))
        return [Comment.from_api(c) for c in data.get("comments", [])]

    def search_users(self, query: str, limit: int = 10) -> list:
        """Find users by name / email. Cloud's only route to an accountId.

        Server/DC takes `username`, Cloud takes `query` — and Cloud rejects
        the other one outright rather than ignoring it.
        """
        from .models import User

        param = "query" if self.dialect.is_cloud else "username"
        data = self._get(
            self.dialect.user_search_path,
            params={param: query, "maxResults": str(limit)},
        )
        rows = data if isinstance(data, list) else data.get("values", [])
        users = [User.from_api(u) for u in rows]
        return [u for u in users if u is not None]

    def resolve_assignee(self, query: str) -> str:
        """Turn a human-typed identifier into what `assign` needs to send.

        On Server/DC the username *is* the identifier, so this is a no-op —
        it deliberately doesn't spend a request confirming what the API will
        confirm anyway. On Cloud nobody knows their own accountId, so an
        email or display name gets looked up, with exact-email and
        exact-display-name matches winning over a partial one.
        """
        query = query.strip()
        if not self.dialect.is_cloud or not query:
            return query
        # Already an accountId (Atlassian's are opaque but never contain @).
        if _looks_like_account_id(query):
            return query
        candidates = self.search_users(query)
        if not candidates:
            raise UserNotFound(f"No Jira Cloud user matches '{query}'.")
        if len(candidates) > 1:
            lowered = query.lower()
            exact = [
                u
                for u in candidates
                if u.email.lower() == lowered or u.display_name.lower() == lowered
            ]
            if len(exact) != 1:
                raise AmbiguousUser(query, candidates)
            candidates = exact
        account_id = candidates[0].account_id
        if not account_id:
            raise UserNotFound(f"Jira returned no accountId for '{query}'.")
        return account_id

    # -- Writes (M2) ---------------------------------------------------

    def add_comment(self, key: str, body: str) -> Comment:
        r = self._send(
            "POST",
            self._api(f"/issue/{key}/comment"),
            json={"body": body},
        )
        return Comment.from_api(r.json())

    def edit_issue(self, key: str, fields: dict) -> None:
        """PUT a partial field update. Returns 204; no body parsed."""
        self._send(
            "PUT",
            self._api(f"/issue/{key}"),
            json={"fields": fields},
        )

    def update_labels(self, key: str, add: list[str] | None = None,
                      remove: list[str] | None = None) -> None:
        """Idempotent add/remove via Jira's update primitive.

        Server-side: adding an existing label or removing an absent one
        is a no-op. Pass either or both lists.
        """
        ops = [{"add": n} for n in (add or [])] + [{"remove": n} for n in (remove or [])]
        if not ops:
            return
        self._send(
            "PUT",
            self._api(f"/issue/{key}"),
            json={"update": {"labels": ops}},
        )

    def assign(self, key: str, identifier: str | None) -> None:
        """Set the assignee. Pass None to unassign.

        `identifier` is a username on Server/DC and an accountId on Cloud —
        see `resolve_assignee` for turning a human-typed value into one.
        """
        self._send(
            "PUT",
            self._api(f"/issue/{key}/assignee"),
            json=self.dialect.assignee_payload(identifier),
        )

    def get_transitions(self, key: str) -> list[Transition]:
        data = self._get(self._api(f"/issue/{key}/transitions"))
        return [Transition.from_api(t) for t in data.get("transitions", [])]

    def do_transition(
        self,
        key: str,
        transition_id: str,
        comment: str | None = None,
    ) -> None:
        body: dict = {"transition": {"id": transition_id}}
        if comment:
            body["update"] = {"comment": [{"add": {"body": comment}}]}
        self._send(
            "POST",
            self._api(f"/issue/{key}/transitions"),
            json=body,
        )


def _looks_like_account_id(value: str) -> bool:
    """Atlassian accountIds are opaque ids, never email-shaped.

    Both the classic `557058:<uuid>` form and the newer bare-hex form are
    covered; anything with an `@` or a space is a person's email or name.
    """
    if "@" in value or " " in value:
        return False
    return ":" in value or (len(value) >= 24 and all(
        c.isalnum() or c in "-_" for c in value
    ))
