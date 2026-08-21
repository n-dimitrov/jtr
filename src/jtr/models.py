from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class User:
    name: str
    display_name: str
    key: str = ""
    email: str = ""
    # Cloud's only stable handle for a person. Server/DC never sends it;
    # Cloud stopped sending `name`/`key` when GDPR landed, so exactly one
    # of `name` and `account_id` is populated for any given deployment.
    account_id: str = ""

    @classmethod
    def from_api(cls, data: dict | None) -> User | None:
        if not data:
            return None
        return cls(
            name=data.get("name", ""),
            display_name=data.get("displayName", ""),
            key=data.get("key", ""),
            email=data.get("emailAddress", ""),
            account_id=data.get("accountId", "") or "",
        )

    def short(self) -> str:
        return self.display_name or self.name or self.email or self.key


@dataclass
class Comment:
    id: str
    author: User | None
    body: str
    created: str
    updated: str

    @classmethod
    def from_api(cls, data: dict) -> Comment:
        return cls(
            id=data.get("id", ""),
            author=User.from_api(data.get("author")),
            body=data.get("body", "") or "",
            created=data.get("created", ""),
            updated=data.get("updated", ""),
        )


@dataclass
class Transition:
    id: str
    name: str
    to_status: str

    @classmethod
    def from_api(cls, data: dict) -> Transition:
        to = data.get("to") or {}
        return cls(
            id=data.get("id", ""),
            name=data.get("name", "") or "",
            to_status=to.get("name", "") or "",
        )


@dataclass
class Project:
    key: str
    name: str
    id: str = ""
    lead: str = ""

    @classmethod
    def from_api(cls, data: dict) -> Project:
        return cls(
            key=data.get("key", "") or "",
            name=data.get("name", "") or "",
            id=str(data.get("id", "") or ""),
            lead=((data.get("lead") or {}).get("displayName")) or "",
        )


@dataclass
class Ticket:
    key: str
    summary: str
    status: str
    priority: str
    issue_type: str
    project_key: str
    assignee: User | None
    reporter: User | None
    created: str
    updated: str
    labels: list[str] = field(default_factory=list)
    fix_versions: list[str] = field(default_factory=list)
    description: str = ""

    @classmethod
    def from_api(cls, data: dict) -> Ticket:
        f = data.get("fields") or {}
        return cls(
            key=data.get("key", ""),
            summary=f.get("summary", "") or "",
            status=((f.get("status") or {}).get("name")) or "",
            priority=((f.get("priority") or {}).get("name")) or "",
            issue_type=((f.get("issuetype") or {}).get("name")) or "",
            project_key=((f.get("project") or {}).get("key")) or "",
            assignee=User.from_api(f.get("assignee")),
            reporter=User.from_api(f.get("reporter")),
            created=f.get("created", "") or "",
            updated=f.get("updated", "") or "",
            labels=list(f.get("labels") or []),
            fix_versions=[
                v.get("name", "") for v in (f.get("fixVersions") or [])
            ],
            description=f.get("description") or "",
        )


@dataclass
class SearchPage:
    """A page of search results plus whatever it takes to fetch the next one.

    The two deployments page differently and the difference is not
    cosmetic. Server/DC returns `startAt`/`total`, so a caller can jump to
    an arbitrary offset and knows how many results exist. Cloud's
    `/search/jql` returns an opaque `nextPageToken` and *no total at all* —
    there is no count to report and no offset to jump to. Hence `total` is
    optional and both cursors are exposed; consumers should branch on
    `has_more` and use whichever of `next_start_at` / `next_page_token` is
    not None.
    """

    tickets: list[Ticket]
    start_at: int = 0
    max_results: int = 0
    total: int | None = None
    next_page_token: str | None = None

    @property
    def next_start_at(self) -> int | None:
        if self.total is None:
            return None
        nxt = self.start_at + len(self.tickets)
        return nxt if nxt < self.total and self.tickets else None

    @property
    def has_more(self) -> bool:
        return self.next_page_token is not None or self.next_start_at is not None
