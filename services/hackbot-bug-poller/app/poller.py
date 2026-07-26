import logging
from datetime import datetime, timedelta, timezone
from itertools import zip_longest

from app import bugzilla, client
from app.bugzilla import Bug
from app.config import Query, settings

logger = logging.getLogger(__name__)

NON_TERMINAL_STATUSES = ("pending", "running")

# Written to `inputs.source` on every run we create, so we can tell our runs from
# ones a human launched in the UI.
POLLER_SOURCE = "bug-poller"


def _is_stale(run: dict, cutoff: datetime) -> bool:
    """Whether a non-terminal run is old enough to have been abandoned.

    An unparsable timestamp counts as fresh: the safe direction is to keep
    holding the slot rather than over-dispatch.
    """
    raw = run.get("created_at")
    if not raw:
        return False
    try:
        return datetime.fromisoformat(raw) < cutoff
    except ValueError:
        logger.warning(
            "run %s has an unparsable created_at: %r", run.get("run_id"), raw
        )
        return False


def count_in_flight() -> int:
    """How many poller-started runs are currently occupying a slot.

    Manual UI runs carry no `source`, so they never starve the pipeline.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.stale_run_minutes)
    in_flight = 0
    for status in NON_TERMINAL_STATUSES:
        for run in client.list_runs(status=status):
            if (run.get("inputs") or {}).get("source") != POLLER_SOURCE:
                continue
            if _is_stale(run, cutoff):
                logger.warning(
                    "ignoring stale %s run %s (older than %d minutes)",
                    status,
                    run.get("run_id"),
                    settings.stale_run_minutes,
                )
                continue
            in_flight += 1
    return in_flight


def collect_candidates(queries: list[Query]) -> list[Bug]:
    """Run every query and merge the results into one ordered list.

    Interleaved rather than concatenated, or the largest query would starve the
    others. A bug matching two queries keeps its first position and appears once.
    """
    per_query = []
    for query in queries:
        try:
            per_query.append(bugzilla.search(query))
        except Exception:
            # One flaky query should not stop the others from being serviced.
            logger.exception("query %s failed; skipping it this tick", query.name)

    seen: set[int] = set()
    candidates = []
    for bug in (b for group in zip_longest(*per_query) for b in group if b is not None):
        if bug.id in seen:
            continue
        seen.add(bug.id)
        candidates.append(bug)
    return candidates


def agent_inputs(bug_id: int) -> dict:
    """Build the POST body. Unset knobs are omitted so hackbot uses its defaults."""
    inputs: dict = {"bug_id": bug_id, "source": POLLER_SOURCE}
    for field in ("model", "max_turns", "effort"):
        value = getattr(settings, field)
        if value is not None:
            inputs[field] = value
    return inputs


def tick(queries: list[Query]) -> int:
    """Top the pool back up to max_in_flight. Returns how many runs it started."""
    capacity = settings.max_in_flight - count_in_flight()
    if capacity <= 0:
        # Before searching, so a full pool does not poll Bugzilla every minute.
        logger.info("at capacity (%d in flight); nothing to do", settings.max_in_flight)
        return 0

    # Triaging does not modify a bug, so Bugzilla keeps returning ones we have
    # already done. This is what lets us walk past them.
    already_run = client.dispatched_bug_ids()

    dispatched = 0
    for bug in collect_candidates(queries):
        if bug.groups:
            logger.info("skipping security-restricted bug %d", bug.id)
            continue
        if bug.id in already_run:
            continue

        run_id = client.trigger_run(agent_inputs(bug.id))
        logger.info(
            "triggered %s run %s for bug %d (%s)",
            settings.agent_name,
            run_id,
            bug.id,
            bug.summary[:80],
        )
        dispatched += 1
        if dispatched >= capacity:
            break

    if dispatched == 0:
        logger.info("no untriaged bugs to dispatch (%d slots free)", capacity)
    return dispatched
