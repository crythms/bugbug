import httpx

from app.config import settings

_TIMEOUT = httpx.Timeout(30.0)


def _headers() -> dict[str, str]:
    return {"X-API-Key": settings.hackbot_api_key}


def _url(path: str) -> str:
    return f"{settings.hackbot_api_url.rstrip('/')}{path}"


def trigger_run(inputs: dict) -> str:
    """Create a run for the configured agent. Returns the run id."""
    resp = httpx.post(
        _url(f"/agents/{settings.agent_name}/runs"),
        json=inputs,
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["run_id"]


_PAGE_SIZE = 100  # the API's maximum


def list_runs(
    status: str | None = None,
    limit: int = _PAGE_SIZE,
    offset: int = 0,
) -> list[dict]:
    """List runs for the configured agent, newest first."""
    params: dict[str, str | int] = {"agent": settings.agent_name, "limit": limit}
    if status is not None:
        params["status"] = status
    if offset:
        params["offset"] = offset

    resp = httpx.get(_url("/runs"), params=params, headers=_headers(), timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def dispatched_bug_ids() -> set[int]:
    """Every bug this agent has ever been run against, in one sweep.

    Swept once per tick and compared in memory rather than asked per candidate:
    a 2000-bug query would otherwise mean 2000 round trips a tick, versus one
    per 100 runs in history.

    Deliberately includes manual UI runs -- a bug triaged by hand has been
    triaged, and re-running it would duplicate work.
    """
    seen: set[int] = set()
    offset = 0
    while True:
        page = list_runs(limit=_PAGE_SIZE, offset=offset)
        for run in page:
            bug_id = (run.get("inputs") or {}).get("bug_id")
            if bug_id is not None:
                seen.add(int(bug_id))
        if len(page) < _PAGE_SIZE:
            return seen
        offset += _PAGE_SIZE
