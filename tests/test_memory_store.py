"""Tests for the in-memory TaskStore.

These pin the semantics that the Postgres backend will later have to match
exactly (the same test suite gets pointed at both). The headline property is
idempotent create: the same key never produces a second task.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from mcp_durable_tasks.errors import InvalidTransition, TaskNotFound
from mcp_durable_tasks.memory import InMemoryTaskStore
from mcp_durable_tasks.store import TaskStore
from mcp_durable_tasks.task import TaskState


@pytest.fixture
def store() -> InMemoryTaskStore:
    return InMemoryTaskStore()


def test_conforms_to_protocol(store):
    # runtime_checkable Protocol: structural conformance, no subclassing.
    assert isinstance(store, TaskStore)


# --- create + get --------------------------------------------------------------


def test_create_starts_in_working(store):
    task = store.create_task({"op": "count_rows"})
    assert task.state == TaskState.WORKING
    assert store.get_task(task.id) is not None


def test_get_unknown_raises(store):
    with pytest.raises(TaskNotFound):
        store.get_task("task_does_not_exist")


# --- idempotency: the headline property ---------------------------------------


def test_same_idempotency_key_returns_same_task(store):
    a = store.create_task({"op": "charge", "amount": 50}, idempotency_key="charge-1")
    b = store.create_task({"op": "charge", "amount": 50}, idempotency_key="charge-1")
    # Same handle, not two tasks.
    assert a.id == b.id
    assert len(store.list_tasks()) == 1


def test_different_keys_create_different_tasks(store):
    a = store.create_task({"op": "charge"}, idempotency_key="charge-1")
    b = store.create_task({"op": "charge"}, idempotency_key="charge-2")
    assert a.id != b.id
    assert len(store.list_tasks()) == 2


def test_no_key_means_no_dedup(store):
    a = store.create_task({"op": "charge"})
    b = store.create_task({"op": "charge"})
    assert a.id != b.id
    assert len(store.list_tasks()) == 2


def test_dedup_returns_current_state_not_a_fresh_task(store):
    """A retried create must return the task *as it is now* — if it already
    completed, the caller sees completed, not a new WORKING task."""
    a = store.create_task({"op": "charge"}, idempotency_key="charge-1")
    store.update_task(a.id, TaskState.COMPLETED, result={"charged": True})
    again = store.create_task({"op": "charge"}, idempotency_key="charge-1")
    assert again.state == TaskState.COMPLETED
    assert again.result == {"charged": True}


# --- update / transitions ------------------------------------------------------


def test_update_applies_transition(store):
    task = store.create_task({"op": "x"})
    moved = store.update_task(task.id, TaskState.INPUT_REQUIRED)
    assert moved.state == TaskState.INPUT_REQUIRED
    assert store.get_task(task.id).state == TaskState.INPUT_REQUIRED


def test_illegal_transition_propagates_and_does_not_persist(store):
    task = store.create_task({"op": "x"})
    store.update_task(task.id, TaskState.COMPLETED, result={"ok": True})
    with pytest.raises(InvalidTransition):
        store.update_task(task.id, TaskState.WORKING)
    # State is unchanged after the rejected move.
    assert store.get_task(task.id).state == TaskState.COMPLETED


def test_update_unknown_raises(store):
    with pytest.raises(TaskNotFound):
        store.update_task("task_missing", TaskState.COMPLETED)


def test_cancel(store):
    task = store.create_task({"op": "x"})
    assert store.cancel_task(task.id).state == TaskState.CANCELLED


# --- TTL reaping ---------------------------------------------------------------


def test_reap_removes_only_expired_terminal_tasks(store):
    past = datetime.now(timezone.utc) - timedelta(hours=1)

    # expired + terminal -> reaped
    done = store.create_task({"op": "done"}, idempotency_key="k-done", ttl_seconds=1)
    store.update_task(done.id, TaskState.COMPLETED, result={})
    # force its expiry into the past
    store._tasks[done.id] = replace(store._tasks[done.id], expires_at=past)

    # terminal but not expired -> kept
    fresh = store.create_task({"op": "fresh"}, ttl_seconds=3600)
    store.update_task(fresh.id, TaskState.COMPLETED, result={})

    # in-flight (never terminal) -> kept even if somehow expired
    working = store.create_task({"op": "working"}, ttl_seconds=1)

    removed = store.reap_expired()
    assert removed == 1
    with pytest.raises(TaskNotFound):
        store.get_task(done.id)
    assert store.get_task(fresh.id) is not None
    assert store.get_task(working.id) is not None
    # secondary index was cleaned up too: the key is free to reuse
    assert store.create_task({"op": "x"}, idempotency_key="k-done").state == TaskState.WORKING
