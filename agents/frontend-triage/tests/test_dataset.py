# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Tests for the eval row builder's run-URL parsing and fetch guards."""

import httpx
import pytest
from evals.dataset import AGENT, DatasetError, fetch_run, parse_run_ref

RUN_ID = "bc09dcf8-9db6-4779-8975-671582a8a0d6"


def test_parse_run_ref_url_forms():
    assert parse_run_ref(f"https://hackbot.moz.tools/runs/{RUN_ID}") == RUN_ID
    assert (
        parse_run_ref(f"https://hackbot.moz.tools/runs/{RUN_ID}?tab=actions") == RUN_ID
    )
    assert parse_run_ref(f"http://localhost:3000/runs/{RUN_ID.upper()}") == RUN_ID
    assert parse_run_ref(RUN_ID) == RUN_ID
    assert parse_run_ref(f"  {RUN_ID}  ") == RUN_ID


@pytest.mark.parametrize(
    "bad",
    [
        "2014702",  # a bug id is not a run
        "https://hackbot.moz.tools/runs/",
        "https://bugzilla.mozilla.org/show_bug.cgi?id=2063412",
        "not-a-uuid",
        "",
    ],
)
def test_parse_run_ref_rejects(bad):
    with pytest.raises(DatasetError):
        parse_run_ref(bad)


def _client(handler) -> httpx.Client:
    return httpx.Client(
        base_url="http://hackbot-api.test", transport=httpx.MockTransport(handler)
    )


def test_fetch_run_ok():
    def handler(request):
        assert request.url.path == f"/runs/{RUN_ID}"
        return httpx.Response(
            200, json={"run_id": RUN_ID, "agent": AGENT, "status": "succeeded"}
        )

    with _client(handler) as client:
        assert fetch_run(client, RUN_ID)["run_id"] == RUN_ID


def test_fetch_run_not_found():
    with _client(lambda request: httpx.Response(404)) as client:
        with pytest.raises(DatasetError, match="not found"):
            fetch_run(client, RUN_ID)


def test_fetch_run_wrong_agent():
    def handler(request):
        return httpx.Response(
            200, json={"run_id": RUN_ID, "agent": "build-repair", "status": "succeeded"}
        )

    with _client(handler) as client:
        with pytest.raises(DatasetError, match="build-repair"):
            fetch_run(client, RUN_ID)


def test_fetch_run_not_succeeded():
    def handler(request):
        return httpx.Response(
            200, json={"run_id": RUN_ID, "agent": AGENT, "status": "failed"}
        )

    with _client(handler) as client:
        with pytest.raises(DatasetError, match="failed"):
            fetch_run(client, RUN_ID)
