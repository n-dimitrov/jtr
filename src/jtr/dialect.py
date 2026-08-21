"""Which Jira are we talking to, and how does it want to be talked to.

jtr speaks two REST dialects. They differ in more places than the version
number in the path, so the differences live here rather than as `if cloud:`
branches scattered through the client:

    Server / Data Center      Cloud (*.atlassian.net)
    ----------------------    -----------------------------------
    Bearer <PAT>              Basic base64(email:api-token)
    /rest/api/2/search        /rest/api/{v}/search/jql
    startAt/total paging      nextPageToken cursor, no total
    assignee {"name": ...}    assignee {"accountId": ...}
    REST v2 only              REST v2 and v3 (v3 bodies are ADF)

The API version is deliberately *not* something the user is asked about at
init: on Server/DC v3 doesn't exist at all, and on Cloud v3's only real
difference is ADF bodies, which this CLI has no use for. It stays an
override for the day Atlassian forces the issue.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from urllib.parse import urlparse

SERVER = "server"
CLOUD = "cloud"
DEPLOYMENTS = (SERVER, CLOUD)

AUTO = "auto"

DEFAULT_API_VERSION = "2"
API_VERSIONS = ("2", "3")

# Hostnames Atlassian serves Cloud tenants from. `.jira.com` covers the
# legacy OnDemand names that still redirect.
_CLOUD_HOST_SUFFIXES = (".atlassian.net", ".jira.com", ".jira-dev.com")


class DialectError(ValueError):
    """A deployment / API-version combination that cannot work."""


def detect_deployment(base_url: str) -> str:
    """Guess the deployment from the hostname alone.

    A guess, not a verdict — Cloud tenants can sit behind a vanity domain
    and DC instances can be hosted anywhere, which is exactly why
    `JTR_DEPLOYMENT` exists. Server/DC is the safe default: it is what a
    self-hosted URL almost always is, and getting it wrong surfaces as an
    immediate 404 rather than as silently wrong data.
    """
    host = (urlparse(base_url).hostname or "").lower()
    return CLOUD if host.endswith(_CLOUD_HOST_SUFFIXES) else SERVER


def normalize_deployment(value: str | None) -> str | None:
    """Validate a configured deployment. `auto`/empty means "detect"."""
    if value is None:
        return None
    v = value.strip().lower()
    if not v or v == AUTO:
        return None
    if v in ("datacenter", "data-center", "dc", "onpremise", "on-premise"):
        return SERVER
    if v not in DEPLOYMENTS:
        raise DialectError(
            f"Unknown deployment {value!r}. Use one of: "
            + ", ".join((*DEPLOYMENTS, AUTO))
            + "."
        )
    return v


def normalize_api_version(value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip().lower().lstrip("v")
    if not v or v == AUTO:
        return None
    if v not in API_VERSIONS:
        raise DialectError(
            f"Unsupported API version {value!r}. Use one of: "
            + ", ".join(API_VERSIONS)
            + "."
        )
    return v


@dataclass(frozen=True)
class Dialect:
    deployment: str
    api_version: str = DEFAULT_API_VERSION

    @classmethod
    def resolve(
        cls,
        base_url: str,
        *,
        deployment: str | None = None,
        api_version: str | None = None,
    ) -> Dialect:
        """Build the dialect for a base URL, honouring explicit overrides."""
        dep = normalize_deployment(deployment) or detect_deployment(base_url)
        ver = normalize_api_version(api_version) or DEFAULT_API_VERSION
        if dep == SERVER and ver == "3":
            raise DialectError(
                "Jira Server / Data Center has no REST API v3 — every call "
                "would 404.\n"
                "Fix: unset JTR_API_VERSION (or set it to 2)."
            )
        return cls(deployment=dep, api_version=ver)

    # -- Traits --------------------------------------------------------

    @property
    def is_cloud(self) -> bool:
        return self.deployment == CLOUD

    @property
    def supports_sso(self) -> bool:
        """Cookie-jar SSO only exists for a gateway-fronted Server/DC.

        Atlassian does not accept browser cookies as REST credentials on
        Cloud, so capturing them would produce a session that authenticates
        nothing.
        """
        return not self.is_cloud

    @property
    def supports_offset_paging(self) -> bool:
        """Cloud's search endpoint pages by cursor and reports no total."""
        return not self.is_cloud

    @property
    def uses_adf(self) -> bool:
        """v3 returns comment/description bodies as ADF documents, not text."""
        return self.api_version == "3"

    @property
    def auth_method(self) -> str:
        """The credential shape this deployment expects."""
        return "token" if self.is_cloud else "pat"

    # -- Paths ---------------------------------------------------------

    def api(self, path: str) -> str:
        """`/issue/PROJ-1` -> `/rest/api/2/issue/PROJ-1`."""
        return f"/rest/api/{self.api_version}{path}"

    @property
    def myself_path(self) -> str:
        return self.api("/myself")

    @property
    def search_path(self) -> str:
        """`/search` was removed from Cloud; `/search/jql` replaced it."""
        return self.api("/search/jql" if self.is_cloud else "/search")

    @property
    def projects_path(self) -> str:
        """The unpaginated `/project` is deprecated on Cloud."""
        return self.api("/project/search" if self.is_cloud else "/project")

    @property
    def user_search_path(self) -> str:
        return self.api("/user/search")

    # -- Payloads ------------------------------------------------------

    def auth_headers(self, *, token: str | None, email: str | None) -> dict:
        """The Authorization header for this deployment, if we can build one.

        Cloud needs both halves; a token without an email is a half-configured
        setup and gets no header at all rather than a malformed one.
        """
        if not token:
            return {}
        if not self.is_cloud:
            return {"Authorization": f"Bearer {token}"}
        if not email:
            return {}
        raw = f"{email}:{token}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}

    def assignee_payload(self, identifier: str | None) -> dict:
        """Body for PUT /issue/{key}/assignee. None clears the assignee.

        Cloud dropped `name` from the user model for GDPR reasons; the only
        stable handle is the opaque accountId. `-1` would mean "automatic",
        which is not the same thing as unassigned, so null it is.
        """
        field = "accountId" if self.is_cloud else "name"
        return {field: identifier or None}

    def user_identifier(self, user) -> str:
        """The value `assignee_payload` expects, read back off a user object.

        Used to spot a no-op assignment, which is why it has to agree with
        `assignee_payload` about which field identifies a person.
        """
        if user is None:
            return ""
        return (user.account_id if self.is_cloud else user.name) or ""

    def search_params(
        self,
        jql: str,
        *,
        fields: list[str],
        limit: int,
        start_at: int = 0,
        cursor: str | None = None,
    ) -> dict:
        """Query params for one page of search results.

        Cloud caps `maxResults` at 100 and ignores `startAt` entirely; it
        walks pages with the opaque token from the previous response.
        """
        params = {
            "jql": jql,
            "fields": ",".join(fields),
            "maxResults": str(min(limit, 100) if self.is_cloud else limit),
        }
        if self.is_cloud:
            if cursor:
                params["nextPageToken"] = cursor
        else:
            params["startAt"] = str(start_at)
        return params

    def describe(self) -> str:
        name = "Jira Cloud" if self.is_cloud else "Jira Server/Data Center"
        return f"{name} (REST v{self.api_version})"
