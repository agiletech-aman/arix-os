
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from main import InvalidTransitionError, TaskStatus, app, tasks, transition_task

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_tasks():
    tasks.clear()
    yield
    tasks.clear()


def test_health():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_create_and_get_task():
    response = client.post(
        "/v1/tasks",
        json={"goal": "Inspect a Laravel project"},
    )

    assert response.status_code == 202

    task = response.json()
    assert task["goal"] == "Inspect a Laravel project"
    assert task["status"] == "queued"

    result = client.get(f"/v1/tasks/{task['id']}")

    assert result.status_code == 200
    assert result.json()["id"] == task["id"]


def test_blank_goal_is_rejected():
    response = client.post(
        "/v1/tasks",
        json={"goal": "   "},
    )

    assert response.status_code == 422


def test_unknown_task_returns_404():
    response = client.get("/v1/tasks/does-not-exist")

    assert response.status_code == 404


def _create(goal="Do a thing"):
    return client.post("/v1/tasks", json={"goal": goal}).json()


def test_create_task_shape_and_utc_timestamps():
    task = _create("  padded goal  ")

    assert task["goal"] == "padded goal"
    assert task["status"] == "queued"
    assert set(task) == {"id", "goal", "status", "created_at", "updated_at"}
    created = datetime.fromisoformat(task["created_at"])
    assert created.utcoffset() == timedelta(0)
    assert task["created_at"] == task["updated_at"]


@pytest.mark.parametrize("payload", [{"goal": ""}, {"goal": " \n\t "}, {}])
def test_invalid_goal_is_rejected(payload):
    assert client.post("/v1/tasks", json=payload).status_code == 422
    assert tasks == {}


def test_list_tasks_newest_first():
    assert client.get("/v1/tasks").json() == {"tasks": [], "count": 0}

    ids = [_create(f"task {i}")["id"] for i in range(3)]
    body = client.get("/v1/tasks").json()

    assert body["count"] == 3
    assert [t["id"] for t in body["tasks"]] == list(reversed(ids))


def test_cancel_queued_task():
    task = _create()

    response = client.post(f"/v1/tasks/{task['id']}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["updated_at"] >= task["updated_at"]
    assert client.get(f"/v1/tasks/{task['id']}").json()["status"] == "cancelled"


def test_cancel_unknown_task_returns_404():
    assert client.post("/v1/tasks/nope/cancel").status_code == 404


@pytest.mark.parametrize(
    "status",
    [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED],
)
def test_cancel_finished_task_returns_409_and_keeps_status(status):
    task = _create()
    tasks[task["id"]].status = status

    response = client.post(f"/v1/tasks/{task['id']}/cancel")

    assert response.status_code == 409
    assert tasks[task["id"]].status == status


def test_valid_transition_chain_updates_timestamp():
    task = tasks[_create()["id"]]
    before = task.updated_at

    for status in [
        TaskStatus.PLANNING,
        TaskStatus.AWAITING_APPROVAL,
        TaskStatus.EXECUTING,
        TaskStatus.VERIFYING,
        TaskStatus.COMPLETED,
    ]:
        transition_task(task, status)
        assert task.status == status

    assert task.updated_at >= before


@pytest.mark.parametrize(
    "start,target",
    [
        (TaskStatus.QUEUED, TaskStatus.COMPLETED),
        (TaskStatus.QUEUED, TaskStatus.EXECUTING),  # must pass approval first
        (TaskStatus.PLANNING, TaskStatus.EXECUTING),
        (TaskStatus.COMPLETED, TaskStatus.QUEUED),
        (TaskStatus.CANCELLED, TaskStatus.PLANNING),
        (TaskStatus.FAILED, TaskStatus.COMPLETED),
    ],
)
def test_invalid_transitions_raise(start, target):
    task = tasks[_create()["id"]]
    task.status = start

    with pytest.raises(InvalidTransitionError):
        transition_task(task, target)

    assert task.status == start


def test_queued_task_is_never_reported_completed():
    task = _create()

    assert client.get(f"/v1/tasks/{task['id']}").json()["status"] == "queued"
