"""Turn a pasted Bugzilla buglist URL into a REST search.

Queries are configured as the literal ``buglist.cgi`` URL copied out of the
browser's address bar. Bugzilla's REST ``/bug`` endpoint accepts the same search
parameters the web UI uses, so the query that runs is exactly the one whose
results were on screen -- and percent-encoding is already correct, because the
browser produced it.
"""

import logging
from urllib.parse import parse_qsl, urlsplit

import httpx
from pydantic import BaseModel

from app.config import Query, settings

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(60.0)

# Web-UI-only params, or ones we set ourselves. Anything else passes through
# untouched, so an unusual search field needs no change here.
_DROPPED_PARAMS = frozenset(
    {
        "list_id",
        "query_format",
        "columnlist",
        "ctype",
        "human",
        "order",
        "limit",
        "offset",
        "include_fields",
    }
)

# `groups` is what lets us drop security-restricted bugs.
_INCLUDE_FIELDS = "id,summary,component,groups"


class QueryConfigError(Exception):
    """A configured query URL is malformed. Not retryable -- fail at startup."""


class Bug(BaseModel):
    id: int
    summary: str = ""
    component: str = ""
    groups: list[str] = []


def to_rest_params(url: str) -> list[tuple[str, str]]:
    """Extract the search parameters from a buglist URL.

    Returns pairs rather than a dict because Bugzilla search fields legitimately
    repeat -- ``bug_status=NEW&bug_status=UNCONFIRMED`` means "either", and
    collapsing it into a dict would silently drop one of them.

    A bare query string (no scheme or host) is accepted too, so pasting just the
    part after ``?`` also works.
    """
    parts = urlsplit(url.strip())
    query = parts.query or (
        parts.path if "=" in parts.path and not parts.scheme else ""
    )
    if not query:
        raise QueryConfigError(
            f"no search parameters found in {url!r}; paste the full buglist.cgi "
            "URL from your browser's address bar"
        )

    params = [(k, v) for k, v in parse_qsl(query) if k not in _DROPPED_PARAMS]
    if not params:
        raise QueryConfigError(
            f"{url!r} has no usable search parameters once display-only ones are "
            "removed; it may be a saved-search link rather than a search result"
        )
    return params


def search(query: Query) -> list[Bug]:
    """Run one configured query. Oldest bug first, so backlogs drain in order.

    Deliberately unlimited. A `limit` would return the same first N bugs every
    tick -- triaged bugs never leave the result set -- so the poller would stall
    once it had run them. Payloads stay small anyway: ~190 KB for 1400 bugs.
    """
    params = to_rest_params(query.url)
    params += [
        ("include_fields", _INCLUDE_FIELDS),
        ("order", "bug_id"),
    ]

    headers = {}
    if settings.bugzilla_api_key:
        headers["X-Bugzilla-API-Key"] = settings.bugzilla_api_key

    resp = httpx.get(
        f"{settings.bugzilla_api_url.rstrip('/')}/rest/bug",
        params=params,
        headers=headers,
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    bugs = [Bug.model_validate(b) for b in resp.json().get("bugs", [])]
    logger.info("query %s: %d bugs", query.name, len(bugs))
    return bugs
