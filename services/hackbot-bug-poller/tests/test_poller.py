from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import poller
from app.bugzilla import Bug
from app.config import Query

QUERY = Query(
    name="new-tab", url="https://bugzilla.mozilla.org/buglist.cgi?product=Firefox"
)


def bug(bug_id: int, groups: list[str] | None = None) -> Bug:
    return Bug(
        id=bug_id,
        summary=f"bug {bug_id}",
        component="New Tab Page",
        groups=groups or [],
    )


def run(source: str | None = poller.POLLER_SOURCE, age_minutes: int = 1) -> dict:
    created = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    inputs: dict = {"bug_id": 1}
    if source is not None:
        inputs["source"] = source
    return {"run_id": "r-1", "inputs": inputs, "created_at": created.isoformat()}


def _runs_by_status(
    pending: list[dict] | None = None, running: list[dict] | None = None
):
    """Stub for client.list_runs, which the poller calls once per status."""

    def _list_runs(status=None, limit=100, offset=0):
        return {"pending": pending or [], "running": running or []}.get(status, [])

    return _list_runs


# --- capacity accounting -----------------------------------------------------


def test_manual_runs_do_not_consume_capacity():
    """A run launched by hand in the UI carries no `source`, so it is not ours."""
    with patch.object(
        poller.client, "list_runs", _runs_by_status(running=[run(source=None)])
    ):
        assert poller.count_in_flight() == 0


def test_poller_runs_consume_capacity():
    with patch.object(
        poller.client, "list_runs", _runs_by_status(pending=[run()], running=[run()])
    ):
        assert poller.count_in_flight() == 2


def test_stale_runs_release_their_slot(monkeypatch):
    """A lost completion event must not wedge a slot forever."""
    monkeypatch.setattr(poller.settings, "stale_run_minutes", 180)
    with patch.object(
        poller.client, "list_runs", _runs_by_status(running=[run(age_minutes=999)])
    ):
        assert poller.count_in_flight() == 0


def test_unparsable_timestamp_keeps_holding_the_slot():
    bad = {
        "run_id": "r-x",
        "inputs": {"source": poller.POLLER_SOURCE},
        "created_at": "nonsense",
    }
    with patch.object(poller.client, "list_runs", _runs_by_status(running=[bad])):
        assert poller.count_in_flight() == 1


# --- candidate ordering ------------------------------------------------------


def test_candidates_interleave_so_a_big_query_cannot_starve_a_small_one():
    big = Query(name="big", url="https://bugzilla.mozilla.org/buglist.cgi?product=A")
    small = Query(
        name="small", url="https://bugzilla.mozilla.org/buglist.cgi?product=B"
    )
    results = {"big": [bug(1), bug(2), bug(3)], "small": [bug(90)]}

    with patch.object(poller.bugzilla, "search", side_effect=lambda q: results[q.name]):
        assert [b.id for b in poller.collect_candidates([big, small])] == [1, 90, 2, 3]


def test_a_bug_matching_two_queries_appears_once():
    a = Query(name="a", url="https://bugzilla.mozilla.org/buglist.cgi?product=A")
    b = Query(name="b", url="https://bugzilla.mozilla.org/buglist.cgi?product=B")
    results = {"a": [bug(7), bug(8)], "b": [bug(7)]}

    with patch.object(poller.bugzilla, "search", side_effect=lambda q: results[q.name]):
        assert [x.id for x in poller.collect_candidates([a, b])] == [7, 8]


def test_one_failing_query_does_not_sink_the_others():
    good = Query(name="good", url="https://bugzilla.mozilla.org/buglist.cgi?product=A")
    bad = Query(name="bad", url="https://bugzilla.mozilla.org/buglist.cgi?product=B")

    def _search(q):
        if q.name == "bad":
            raise RuntimeError("bugzilla is having a moment")
        return [bug(5)]

    with patch.object(poller.bugzilla, "search", side_effect=_search):
        assert [x.id for x in poller.collect_candidates([good, bad])] == [5]


# --- dispatch ----------------------------------------------------------------


def test_tick_fills_every_free_slot(monkeypatch):
    monkeypatch.setattr(poller.settings, "max_in_flight", 3)
    candidates = [bug(1), bug(2), bug(3), bug(4), bug(5)]

    with (
        patch.object(poller.client, "list_runs", _runs_by_status()),
        patch.object(poller, "collect_candidates", return_value=candidates),
        patch.object(poller.client, "dispatched_bug_ids", return_value=set()),
        patch.object(poller.client, "trigger_run", return_value="run-id") as trigger,
    ):
        assert poller.tick([QUERY]) == 3

    assert [c.args[0]["bug_id"] for c in trigger.call_args_list] == [1, 2, 3]


def test_tick_only_tops_up_the_difference(monkeypatch):
    monkeypatch.setattr(poller.settings, "max_in_flight", 5)
    with (
        patch.object(
            poller.client,
            "list_runs",
            _runs_by_status(running=[run(), run(), run(), run()]),
        ),
        patch.object(poller, "collect_candidates", return_value=[bug(1), bug(2)]),
        patch.object(poller.client, "dispatched_bug_ids", return_value=set()),
        patch.object(poller.client, "trigger_run", return_value="run-id"),
    ):
        assert poller.tick([QUERY]) == 1


def test_at_capacity_does_no_further_work(monkeypatch):
    """With no free slots, skip both Bugzilla and the already-run sweep."""
    monkeypatch.setattr(poller.settings, "max_in_flight", 1)
    with (
        patch.object(poller.client, "list_runs", _runs_by_status(running=[run()])),
        patch.object(poller.bugzilla, "search") as search,
        patch.object(poller.client, "dispatched_bug_ids") as sweep,
        patch.object(poller.client, "trigger_run") as trigger,
    ):
        assert poller.tick([QUERY]) == 0

    search.assert_not_called()
    sweep.assert_not_called()
    trigger.assert_not_called()


def test_max_in_flight_zero_dispatches_nothing(monkeypatch):
    """The `look without touching` setting used when checking a new query."""
    monkeypatch.setattr(poller.settings, "max_in_flight", 0)
    with (
        patch.object(poller.client, "list_runs", _runs_by_status()),
        patch.object(poller.client, "trigger_run") as trigger,
    ):
        assert poller.tick([QUERY]) == 0
    trigger.assert_not_called()


def test_already_run_bugs_are_skipped_including_manual_ones(monkeypatch):
    """Bug 1 was triaged by hand; the poller must not repeat it."""
    monkeypatch.setattr(poller.settings, "max_in_flight", 5)
    with (
        patch.object(poller.client, "list_runs", _runs_by_status()),
        patch.object(poller, "collect_candidates", return_value=[bug(1), bug(2)]),
        patch.object(poller.client, "dispatched_bug_ids", return_value={1}),
        patch.object(poller.client, "trigger_run", return_value="run-id") as trigger,
    ):
        assert poller.tick([QUERY]) == 1

    assert trigger.call_args.args[0]["bug_id"] == 2


def test_drains_a_backlog_bigger_than_one_response(monkeypatch):
    """Regression: capping the Bugzilla result size used to stall the poller.

    Triaging a bug does not modify it, so it stays in the query's result set. When
    search() capped results at N, every tick got back the same first N bugs; once
    those had all been run the poller found nothing new and never reached N+1.
    """
    monkeypatch.setattr(poller.settings, "max_in_flight", 5)
    backlog = [bug(i) for i in range(1, 251)]
    already_run: set[int] = set()

    def trigger(inputs):
        already_run.add(inputs["bug_id"])
        return "run-id"

    with (
        patch.object(poller.client, "list_runs", _runs_by_status()),
        patch.object(poller.bugzilla, "search", return_value=backlog),
        patch.object(poller.client, "dispatched_bug_ids", lambda: set(already_run)),
        patch.object(poller.client, "trigger_run", trigger),
    ):
        for _ in range(50):
            poller.tick([QUERY])

    assert len(already_run) == 250


def test_security_restricted_bugs_are_never_dispatched(monkeypatch):
    monkeypatch.setattr(poller.settings, "max_in_flight", 5)
    with (
        patch.object(poller.client, "list_runs", _runs_by_status()),
        patch.object(
            poller,
            "collect_candidates",
            return_value=[bug(1, groups=["core-security"])],
        ),
        patch.object(poller.client, "dispatched_bug_ids", return_value=set()),
        patch.object(poller.client, "trigger_run") as trigger,
    ):
        assert poller.tick([QUERY]) == 0
    trigger.assert_not_called()


# --- agent inputs ------------------------------------------------------------


def test_unset_knobs_are_omitted_so_hackbot_uses_its_defaults(monkeypatch):
    for field in ("model", "max_turns", "effort"):
        monkeypatch.setattr(poller.settings, field, None)
    assert poller.agent_inputs(99) == {"bug_id": 99, "source": "bug-poller"}


def test_configured_knobs_are_passed_through(monkeypatch):
    monkeypatch.setattr(poller.settings, "model", "claude-haiku-4-5-20251001")
    monkeypatch.setattr(poller.settings, "max_turns", 40)
    monkeypatch.setattr(poller.settings, "effort", None)
    assert poller.agent_inputs(99) == {
        "bug_id": 99,
        "source": "bug-poller",
        "model": "claude-haiku-4-5-20251001",
        "max_turns": 40,
    }
