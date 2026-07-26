from unittest.mock import patch

from app import client


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _run(bug_id: int) -> dict:
    return {"run_id": f"r-{bug_id}", "inputs": {"bug_id": bug_id}}


def test_dispatched_bug_ids_pages_until_the_last_short_page():
    """The API caps `limit` at 100, so a long history has to be paged."""
    pages = [
        [_run(i) for i in range(1, 101)],  # full page -> keep going
        [_run(i) for i in range(101, 201)],  # full page -> keep going
        [_run(201), _run(202)],  # short page -> stop
    ]
    with patch.object(
        client.httpx, "get", side_effect=[_Resp(p) for p in pages]
    ) as get:
        seen = client.dispatched_bug_ids()

    assert seen == set(range(1, 203))
    assert [c.kwargs["params"].get("offset", 0) for c in get.call_args_list] == [
        0,
        100,
        200,
    ]


def test_dispatched_bug_ids_stops_after_one_short_page():
    with patch.object(client.httpx, "get", return_value=_Resp([_run(7)])) as get:
        assert client.dispatched_bug_ids() == {7}
    assert get.call_count == 1


def test_dispatched_bug_ids_tolerates_runs_without_a_bug_id():
    """Other agents' schemas differ; a run with no bug_id must not blow up."""
    page = [_run(1), {"run_id": "r-x", "inputs": {}}, {"run_id": "r-y"}]
    with patch.object(client.httpx, "get", return_value=_Resp(page)):
        assert client.dispatched_bug_ids() == {1}


def test_dispatched_bug_ids_is_empty_when_nothing_has_run():
    with patch.object(client.httpx, "get", return_value=_Resp([])):
        assert client.dispatched_bug_ids() == set()
