"""Tests for the run-actions HTTP endpoints (list + manual apply-all + edit).

Exercises the route handlers directly with a fake DB (matching this suite's
fake-based style), stubbing the applier/query helpers to keep the handlers'
own logic — 404 handling, calling apply_all_pending, returning the list, and
the edit endpoint's status/type guards — in focus.
"""

import uuid
from types import SimpleNamespace

import pytest
from app.routers import runs as runs_router
from app.schemas import RunActionDoc, RunActionEdit
from fastapi import HTTPException


class _FakeDB:
    def __init__(self, run):
        self._run = run

    async def get(self, model, run_id):
        return self._run


class _FakeEditDB(_FakeDB):
    """A fake DB that also answers the edit handler's `select(RunAction)`."""

    def __init__(self, run, action):
        super().__init__(run)
        self._action = action
        self.commits = 0

    async def execute(self, *args, **kwargs):
        return SimpleNamespace(scalar_one_or_none=lambda: self._action)

    async def commit(self):
        self.commits += 1


def _action_row(
    *, status="pending", action_type="bugzilla.add_comment", params=None, idx=0
):
    return SimpleNamespace(
        idx=idx,
        type=action_type,
        params=params if params is not None else {"bug_id": 42, "text": "original"},
        ref=None,
        status=status,
        result=None,
        error=None,
        applied_at=None,
        edited_by=None,
        edited_at=None,
    )


_ACTIONS = [
    RunActionDoc(idx=0, type="bugzilla.add_comment", params={}, status="pending")
]


async def test_list_run_actions_404():
    with pytest.raises(HTTPException) as exc:
        await runs_router.list_run_actions(uuid.uuid4(), _FakeDB(None))
    assert exc.value.status_code == 404


async def test_list_run_actions_returns_rows(monkeypatch):
    async def fake_list(db, run_id):
        return _ACTIONS

    monkeypatch.setattr(runs_router, "_list_actions", fake_list)
    out = await runs_router.list_run_actions(uuid.uuid4(), _FakeDB(SimpleNamespace()))
    assert out is _ACTIONS


async def test_apply_run_actions_404():
    with pytest.raises(HTTPException) as exc:
        await runs_router.apply_run_actions(uuid.uuid4(), _FakeDB(None))
    assert exc.value.status_code == 404


async def test_apply_run_actions_applies_then_returns(monkeypatch):
    applied = {"called": False}

    async def fake_apply(db, run):
        applied["called"] = True

    async def fake_list(db, run_id):
        return _ACTIONS

    monkeypatch.setattr(runs_router, "apply_all_pending", fake_apply)
    monkeypatch.setattr(runs_router, "_list_actions", fake_list)

    out = await runs_router.apply_run_actions(uuid.uuid4(), _FakeDB(SimpleNamespace()))
    assert applied["called"] is True
    assert out is _ACTIONS


# --- edit_run_action ---


async def test_edit_run_action_404_unknown_run():
    with pytest.raises(HTTPException) as exc:
        await runs_router.edit_run_action(
            uuid.uuid4(), 0, RunActionEdit(text="new"), None, _FakeEditDB(None, None)
        )
    assert exc.value.status_code == 404


async def test_edit_run_action_404_unknown_idx():
    db = _FakeEditDB(SimpleNamespace(), None)
    with pytest.raises(HTTPException) as exc:
        await runs_router.edit_run_action(
            uuid.uuid4(), 7, RunActionEdit(text="new"), None, db
        )
    assert exc.value.status_code == 404


async def test_edit_run_action_409_when_already_applied():
    db = _FakeEditDB(SimpleNamespace(), _action_row(status="applied"))
    with pytest.raises(HTTPException) as exc:
        await runs_router.edit_run_action(
            uuid.uuid4(), 0, RunActionEdit(text="new"), None, db
        )
    assert exc.value.status_code == 409
    assert db.commits == 0


async def test_edit_run_action_422_for_uneditable_type():
    db = _FakeEditDB(
        SimpleNamespace(), _action_row(action_type="bugzilla.update_bug", params={})
    )
    with pytest.raises(HTTPException) as exc:
        await runs_router.edit_run_action(
            uuid.uuid4(), 0, RunActionEdit(text="new"), None, db
        )
    assert exc.value.status_code == 422
    assert db.commits == 0


async def test_edit_run_action_422_for_blank_text():
    db = _FakeEditDB(SimpleNamespace(), _action_row())
    with pytest.raises(HTTPException) as exc:
        await runs_router.edit_run_action(
            uuid.uuid4(), 0, RunActionEdit(text="   \n  "), None, db
        )
    assert exc.value.status_code == 422
    assert db.commits == 0


async def test_edit_run_action_409_when_failed():
    # A failed action records a real attempt, so it's frozen like an applied one.
    row = _action_row(status="failed")
    db = _FakeEditDB(SimpleNamespace(), row)
    with pytest.raises(HTTPException) as exc:
        await runs_router.edit_run_action(
            uuid.uuid4(), 0, RunActionEdit(text="retry me"), None, db
        )
    assert exc.value.status_code == 409
    assert row.params["text"] == "original"
    assert db.commits == 0


async def test_edit_run_action_replaces_text_and_stamps_editor():
    row = _action_row(params={"bug_id": 42, "text": "original", "is_private": False})
    db = _FakeEditDB(SimpleNamespace(), row)

    # Already-normalized: `X-On-Behalf-Of` lowercasing lives in the `UserEmail`
    # annotation, which only runs under FastAPI's request handling.
    out = await runs_router.edit_run_action(
        uuid.uuid4(),
        0,
        RunActionEdit(text="  human rewrite  "),
        "person@example.com",
        db,
    )

    # Text replaced (and trimmed); the agent's other params are untouched.
    assert row.params == {
        "bug_id": 42,
        "text": "human rewrite",
        "is_private": False,
    }
    assert out.params["text"] == "human rewrite"
    assert row.edited_by == "person@example.com"
    assert row.edited_at is not None
    assert db.commits == 1


async def test_edit_run_action_reassigns_params_object():
    """The JSONB dict must be replaced, not mutated, or SQLAlchemy won't flush."""
    original = {"bug_id": 42, "text": "original"}
    row = _action_row(params=original)
    db = _FakeEditDB(SimpleNamespace(), row)

    await runs_router.edit_run_action(
        uuid.uuid4(), 0, RunActionEdit(text="new"), None, db
    )

    assert row.params is not original
    assert original["text"] == "original"
