"""The pasted-URL contract: what a human copies out of Bugzilla must survive."""

from unittest.mock import patch

import pytest
from app import bugzilla
from app.bugzilla import QueryConfigError
from app.config import Query

# A real buglist.cgi URL, exactly as the browser writes it: percent-encoded
# email, `+` for spaces, and the display-only params Bugzilla tacks on.
PASTED_URL = (
    "https://bugzilla.mozilla.org/buglist.cgi"
    "?product=Firefox&component=New+Tab+Page&bug_status=__open__"
    "&f1=assigned_to&o1=equals&v1=nobody%40mozilla.org"
    "&list_id=18055715&query_format=advanced&order=bug_list&columnlist=summary"
)


def test_pasted_url_keeps_the_search_fields():
    params = dict(bugzilla.to_rest_params(PASTED_URL))
    assert params["product"] == "Firefox"
    assert params["component"] == "New Tab Page"  # `+` decoded back to a space
    assert params["bug_status"] == "__open__"


def test_percent_encoded_values_are_decoded_intact():
    """The whole reason pasting beats hand-writing params."""
    params = dict(bugzilla.to_rest_params(PASTED_URL))
    assert params["v1"] == "nobody@mozilla.org"


def test_display_only_params_are_dropped():
    keys = {k for k, _ in bugzilla.to_rest_params(PASTED_URL)}
    assert not keys & {"list_id", "query_format", "order", "columnlist"}


def test_unknown_search_fields_pass_through():
    """Unusual Bugzilla fields keep working without a code change here."""
    params = dict(
        bugzilla.to_rest_params(
            "https://bugzilla.mozilla.org/buglist.cgi?keywords=access"
        )
    )
    assert params["keywords"] == "access"


def test_repeated_fields_are_all_preserved():
    """`bug_status=NEW&bug_status=UNCONFIRMED` means "either" -- a dict would eat one."""
    params = bugzilla.to_rest_params(
        "https://bugzilla.mozilla.org/buglist.cgi?bug_status=NEW&bug_status=UNCONFIRMED"
    )
    assert sorted(v for k, v in params if k == "bug_status") == ["NEW", "UNCONFIRMED"]


def test_bare_query_string_is_accepted():
    """Pasting just the part after `?` should also work."""
    params = dict(bugzilla.to_rest_params("product=Firefox&component=Sidebar"))
    assert params == {"product": "Firefox", "component": "Sidebar"}


def test_url_with_no_parameters_is_a_config_error():
    with pytest.raises(QueryConfigError):
        bugzilla.to_rest_params("https://bugzilla.mozilla.org/buglist.cgi")


def test_url_with_only_display_params_is_a_config_error():
    with pytest.raises(QueryConfigError):
        bugzilla.to_rest_params("https://bugzilla.mozilla.org/buglist.cgi?list_id=123")


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_search_pins_fields_and_ordering():
    payload = {
        "bugs": [
            {"id": 42, "summary": "Tab strip jitters", "component": "Tabbed Browser"}
        ]
    }

    with patch.object(bugzilla.httpx, "get", return_value=_Resp(payload)) as get:
        bugs = bugzilla.search(Query(name="new-tab", url=PASTED_URL))

    params = dict(get.call_args.kwargs["params"])
    assert params["include_fields"] == "id,summary,component,groups"
    assert params["order"] == "bug_id"  # oldest first, so backlogs drain in order
    assert get.call_args.args[0].endswith("/rest/bug")
    assert [b.id for b in bugs] == [42]
    assert bugs[0].groups == []


def test_search_never_caps_the_result_size():
    """A cap would stall the poller.

    A triaged bug is not modified, so it stays in the result set. With a `limit`
    the query would return the same first N bugs every tick, and once those had
    been run the poller would find nothing new and never reach bug N+1.
    """
    with patch.object(bugzilla.httpx, "get", return_value=_Resp({"bugs": []})) as get:
        bugzilla.search(Query(name="q", url=PASTED_URL))

    assert "limit" not in dict(get.call_args.kwargs["params"])


def test_search_is_anonymous_unless_a_key_is_configured(monkeypatch):
    monkeypatch.setattr(bugzilla.settings, "bugzilla_api_key", None)
    with patch.object(bugzilla.httpx, "get", return_value=_Resp({"bugs": []})) as get:
        bugzilla.search(Query(name="q", url=PASTED_URL))
    assert get.call_args.kwargs["headers"] == {}
