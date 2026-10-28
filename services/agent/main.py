"""Arix Agent API v0.3.

Task lifecycle bookkeeping only. This service never executes commands,
modifies files, installs packages, or contacts external services. Any future
action with side effects must go through the `awaiting_approval` state and
require explicit user approval.
"""

import os
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

from store import TaskStore

app = FastAPI(
    title="Arix Agent API",
    version="0.3.0",
)


class TaskStatus(str, Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Allowed state transitions. Terminal states have no outgoing transitions.
# EXECUTING is only reachable through AWAITING_APPROVAL, so nothing with side
# effects can start without an explicit approval step.
ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset(
        {TaskStatus.PLANNING, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.PLANNING: frozenset(
        {TaskStatus.AWAITING_APPROVAL, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.AWAITING_APPROVAL: frozenset(
        {TaskStatus.EXECUTING, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.EXECUTING: frozenset(
        {TaskStatus.VERIFYING, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.VERIFYING: frozenset(
        {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


class InvalidTransitionError(Exception):
    def __init__(self, current: TaskStatus, target: TaskStatus):
        self.current = current
        self.target = target
        super().__init__(
            f"Cannot change task status from '{current.value}' to '{target.value}'"
        )


class TaskRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=4000)

    @field_validator("goal")
    @classmethod
    def goal_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Task goal cannot be blank")
        return value


class TaskResponse(BaseModel):
    id: str
    goal: str
    status: TaskStatus
    created_at: datetime
    updated_at: datetime


class TaskListResponse(BaseModel):
    tasks: list[TaskResponse]
    count: int


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Tasks are persisted in SQLite so they survive restarts. Override the
# location with ARIX_DB_PATH.
DB_PATH = Path(os.environ.get("ARIX_DB_PATH", Path(__file__).parent / "arix.db"))
store = TaskStore(DB_PATH)


def transition_task(task: TaskResponse, target: TaskStatus) -> TaskResponse:
    """Move a task to `target`, or raise InvalidTransitionError."""
    if target not in ALLOWED_TRANSITIONS[task.status]:
        raise InvalidTransitionError(task.status, target)
    task.status = target
    task.updated_at = utcnow()
    return task


def _get_or_404(task_id: str) -> TaskResponse:
    row = store.get(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskResponse(**row)


@app.get("/health")
def health():
    return {"status": "ok", "service": "arix-agent"}


@app.post("/v1/tasks", response_model=TaskResponse, status_code=202)
def create_task(request: TaskRequest):
    now = utcnow()
    task = TaskResponse(
        id=str(uuid4()),
        goal=request.goal,
        status=TaskStatus.QUEUED,
        created_at=now,
        updated_at=now,
    )
    store.insert(task.id, task.goal, task.status.value, task.created_at, task.updated_at)
    return task


@app.get("/v1/tasks", response_model=TaskListResponse)
def list_tasks():
    newest_first = [TaskResponse(**row) for row in store.list_newest_first()]
    return TaskListResponse(tasks=newest_first, count=len(newest_first))


@app.get("/v1/tasks/{task_id}", response_model=TaskResponse)
def get_task(task_id: str):
    return _get_or_404(task_id)


@app.post("/v1/tasks/{task_id}/cancel", response_model=TaskResponse)
def cancel_task(task_id: str):
    task = _get_or_404(task_id)
    try:
        transition_task(task, TaskStatus.CANCELLED)
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    store.update_status(task.id, task.status.value, task.updated_at)
    return task
