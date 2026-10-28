"""Append-only JSONL audit log. File content is never stored — only size + sha256."""

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path


def _redact(args: dict) -> dict:
    out = {}
    for k, v in args.items():
        if k == "content" and isinstance(v, str):
            b = v.encode("utf-8", "replace")
            out[k] = {"bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()}
        else:
            out[k] = v
    return out


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, *, tool: str, operation: str, args: dict, decision: str,
              reason: str, ok: bool | None, error_code: str | None) -> dict:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "tool": tool,
            "operation": operation,
            "args": _redact(args),
            "decision": decision,
            "reason": reason,
            "ok": ok,
            "error_code": error_code,
        }
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode())
            finally:
                os.close(fd)
        return entry

    def tail(self, n: int = 20) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text().splitlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
