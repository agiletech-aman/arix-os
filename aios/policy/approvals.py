"""Pending approval requests held by the policy service.

A `confirm`-level approval is bound to the exact operation + arguments and can
be used once. Approvals expire. Only the admin socket can create approvals.
"""

import hashlib
import json
import secrets
import threading
import time

from aios.policy.audit import _redact
from aios.policy.levels import Level


def fingerprint(operation: str, args: dict) -> str:
    blob = json.dumps({"op": operation, "args": args}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


class Approvals:
    def __init__(self, ttl: float = 300.0, clock=time.monotonic):
        self.ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}

    def _purge(self) -> None:
        now = self._clock()
        for key in [k for k, v in self._items.items() if now - v["created"] > self.ttl]:
            del self._items[key]

    def request(self, operation: str, args: dict, reason: str, level: Level) -> str:
        fp = fingerprint(operation, args)
        with self._lock:
            self._purge()
            for key, rec in self._items.items():
                if rec["fp"] == fp and not rec["approved"]:
                    return key
            key = secrets.token_urlsafe(6)
            self._items[key] = {
                "operation": operation, "args": dict(args), "fp": fp, "reason": reason,
                "level": level, "created": self._clock(), "approved": False,
            }
            return key

    def pending(self) -> list[dict]:
        with self._lock:
            self._purge()
            return [
                {"id": k, "operation": v["operation"], "args": _redact(v["args"]),
                 "reason": v["reason"], "level": str(v["level"]),
                 "age_s": round(self._clock() - v["created"], 1)}
                for k, v in self._items.items() if not v["approved"]
            ]

    def approve(self, key: str) -> dict:
        with self._lock:
            self._purge()
            rec = self._items.get(key)
            if rec is None:
                raise KeyError(key)
            rec["approved"] = True
            return rec

    def deny(self, key: str) -> bool:
        with self._lock:
            return self._items.pop(key, None) is not None

    def consume(self, key: str, operation: str, args: dict) -> bool:
        """True once if `key` was approved for exactly this operation + args."""
        with self._lock:
            self._purge()
            rec = self._items.get(key)
            if rec and rec["approved"] and rec["fp"] == fingerprint(operation, args):
                del self._items[key]
                return True
            return False
