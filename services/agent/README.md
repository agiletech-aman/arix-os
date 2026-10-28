# Arix Agent (v0.2)

FastAPI service that tracks the lifecycle of AI-OS tasks.

**This service only records task state.** It never executes shell commands,
modifies files, installs packages, or contacts external services. Any future
action that affects files, accounts, networks, or the operating system must go
through the `awaiting_approval` state and require explicit user approval; the
state machine does not allow `executing` to be reached without it.

> **Storage is in memory.** All tasks disappear when the server restarts.

## Setup

```bash
cd ~/arix-os/services/agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

The server binds to `127.0.0.1` (local only). Do not use `--host 0.0.0.0`.

```bash
.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 2006
```

Interactive docs: http://127.0.0.1:2006/docs

## Test

```bash
.venv/bin/python -m pytest -v
```

## Task states

`queued`, `planning`, `awaiting_approval`, `executing`, `verifying`,
`completed`, `failed`, `cancelled`.

| From | Allowed next states |
|---|---|
| queued | planning, cancelled, failed |
| planning | awaiting_approval, cancelled, failed |
| awaiting_approval | executing, cancelled, failed |
| executing | verifying, cancelled, failed |
| verifying | completed, cancelled, failed |
| completed / failed / cancelled | none (terminal) |

New tasks start as `queued`. Nothing in v0.2 advances them further, so no task
is ever reported `completed` automatically.

## API

| Method | Path | Result |
|---|---|---|
| GET | `/health` | 200 |
| POST | `/v1/tasks` | 202 with the task; 422 if goal is empty/whitespace/too long (max 4000) |
| GET | `/v1/tasks` | 200, `{"tasks": [...], "count": n}` newest first |
| GET | `/v1/tasks/{task_id}` | 200, or 404 |
| POST | `/v1/tasks/{task_id}/cancel` | 200 cancelled task; 404 unknown; 409 if already completed/failed/cancelled |

Timestamps (`created_at`, `updated_at`) are UTC ISO 8601.

```bash
curl http://127.0.0.1:2006/health

curl -X POST http://127.0.0.1:2006/v1/tasks \
  -H 'Content-Type: application/json' \
  -d '{"goal": "Inspect a Laravel project"}'

curl http://127.0.0.1:2006/v1/tasks
curl http://127.0.0.1:2006/v1/tasks/<task_id>
curl -X POST http://127.0.0.1:2006/v1/tasks/<task_id>/cancel
```

Example task:

```json
{
  "id": "6f1c…",
  "goal": "Inspect a Laravel project",
  "status": "queued",
  "created_at": "2026-10-27T09:00:00.123456Z",
  "updated_at": "2026-10-27T09:00:00.123456Z"
}
```
