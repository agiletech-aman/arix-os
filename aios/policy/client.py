"""Client for the policy service agent socket. Same call() -> ToolResult contract.

Fails closed: if the service cannot be reached, nothing runs.
"""

import json
import socket

from aios.tools.base import ToolResult


class PolicyClient:
    def __init__(self, socket_path: str, timeout: float = 30.0):
        self.socket_path = socket_path
        self.timeout = timeout

    def _roundtrip(self, req: dict) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(self.timeout)
            s.connect(self.socket_path)
            s.sendall(json.dumps(req).encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf)

    @property
    def OPERATIONS(self) -> tuple[str, ...]:
        return tuple(self._roundtrip({"cmd": "operations"}).get("data") or ())

    def call(self, operation: str, *, approval_id: str | None = None, **args) -> ToolResult:
        req = {"cmd": "call", "operation": operation, "args": args}
        if approval_id:
            req["approval_id"] = approval_id
        try:
            resp = self._roundtrip(req)
        except (OSError, ValueError) as exc:
            return ToolResult(False, error=f"policy service unavailable: {exc}",
                              error_code="POLICY_SERVICE_UNAVAILABLE")
        return ToolResult(
            ok=bool(resp.get("ok")), data=resp.get("data"), error=resp.get("error"),
            error_code=resp.get("error_code"), meta=resp.get("meta") or {},
        )
