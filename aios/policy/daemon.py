"""Policy service: runs the FileTool behind the Policy Engine in its own process.

Two Unix sockets, line-delimited JSON:
  agent socket  (AI process)   {"cmd":"call","operation":..,"args":{..},"approval_id":..?}
                               {"cmd":"operations"}
  admin socket  (human only)   pending | approve | deny | revoke | kill | resume | status

The AI can never approve its own request: approvals exist only on the admin
socket, which has different permissions. The policy file is read once at start
and cannot be changed through either socket.

Run:  python -m aios.policy.daemon --policy FILE --sandbox DIR --audit FILE \\
          --agent-socket PATH --admin-socket PATH
"""

import argparse
import json
import os
import shutil
import signal
import socketserver
import stat
import threading

from aios.policy.approvals import Approvals
from aios.policy.audit import AuditLog
from aios.policy.engine import PolicyEngine
from aios.policy.levels import Level
from aios.policy.loader import PolicyError, load_policy
from aios.tools import FileTool
from aios.tools.base import ToolResult

MAX_LINE = 8 * 1024 * 1024
RESERVED_ARGS = {"operation", "self"}


def _err(msg: str, code: str) -> dict:
    return {"ok": False, "data": None, "error": msg, "error_code": code, "meta": {}}


class PolicyService:
    def __init__(self, tool, policy, audit: AuditLog, approval_ttl: float = 300.0):
        self.audit = audit
        self.approvals = Approvals(approval_ttl)
        self.engine = PolicyEngine(tool, policy, audit=audit, confirm=self._confirm)
        self.killed = False
        self._ctx = threading.local()
        self._servers: list[socketserver.BaseServer] = []

    # ---- confirmation hook used by the engine ------------------------

    def _confirm(self, operation: str, args: dict, reason: str) -> bool:
        approval_id = getattr(self._ctx, "approval_id", None)
        if approval_id and self.approvals.consume(approval_id, operation, args):
            return True
        level = self.engine.check(operation, **args)[0]
        self._ctx.pending_id = self.approvals.request(operation, args, reason, level)
        return False

    def _admin_log(self, what: str, **info) -> None:
        try:
            self.audit.write(tool="PolicyService", operation=f"admin:{what}", args=info,
                             decision="admin", reason="", ok=True, error_code=None)
        except OSError:
            pass

    # ---- agent socket -------------------------------------------------

    def agent_dispatch(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd == "operations":
            return {"ok": True, "data": list(self.engine.OPERATIONS)}
        if cmd != "call":
            return _err(f"unknown command: {cmd!r}", "BAD_REQUEST")

        operation, args = req.get("operation"), req.get("args", {})
        approval_id = req.get("approval_id")
        if not isinstance(operation, str) or not isinstance(args, dict):
            return _err("operation must be a string and args an object", "BAD_REQUEST")
        if RESERVED_ARGS & set(args):
            return _err("reserved argument name", "BAD_REQUEST")
        if approval_id is not None and not isinstance(approval_id, str):
            return _err("approval_id must be a string", "BAD_REQUEST")

        if self.killed:
            res = ToolResult(False, error="AI is stopped by the kill switch", error_code="KILLED")
            try:
                self.audit.write(tool="PolicyService", operation=operation, args=args,
                                 decision="deny", reason="kill switch", ok=False, error_code="KILLED")
            except OSError:
                pass
            return res.to_dict()

        self._ctx.approval_id, self._ctx.pending_id = approval_id, None
        res = self.engine.call(operation, **args)
        if self._ctx.pending_id:
            res.meta["approval_id"] = self._ctx.pending_id
        return res.to_dict()

    # ---- admin socket -------------------------------------------------

    def admin_dispatch(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd == "pending":
            return {"ok": True, "data": self.approvals.pending()}
        if cmd == "status":
            return {"ok": True, "data": {
                "killed": self.killed,
                "session_grants": sorted(self.engine.session_grants),
                "pending": len(self.approvals.pending()),
            }}
        if cmd in ("approve", "deny"):
            key = req.get("id")
            if not isinstance(key, str):
                return _err("id required", "BAD_REQUEST")
            if cmd == "deny":
                found = self.approvals.deny(key)
                self._admin_log("deny", id=key, found=found)
                return {"ok": found, "data": None, "error": None if found else "no such request",
                        "error_code": None if found else "NOT_FOUND"}
            try:
                rec = self.approvals.approve(key)
            except KeyError:
                return _err("no such request (expired?)", "NOT_FOUND")
            if rec["level"] is Level.ASK_ONCE:
                self.engine.session_grants.add(rec["operation"])
            self._admin_log("approve", id=key, operation=rec["operation"], level=str(rec["level"]))
            return {"ok": True, "data": {"operation": rec["operation"], "level": str(rec["level"])}}
        if cmd == "revoke":
            self.engine.revoke_session()
            self._admin_log("revoke")
            return {"ok": True, "data": None}
        if cmd in ("kill", "resume"):
            self.killed = cmd == "kill"
            self._admin_log(cmd)
            return {"ok": True, "data": {"killed": self.killed}}
        return _err(f"unknown command: {cmd!r}", "BAD_REQUEST")

    # ---- lifecycle ----------------------------------------------------

    def start(self, agent_socket, admin_socket, *, agent_mode=0o660, admin_mode=0o600,
              agent_group=None, admin_group=None) -> "PolicyService":
        for path, fn, mode, group in (
            (agent_socket, self.agent_dispatch, agent_mode, agent_group),
            (admin_socket, self.admin_dispatch, admin_mode, admin_group),
        ):
            server = _Server(str(path), fn, mode, group)
            self._servers.append(server)
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def stop(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        self._servers.clear()


class _Handler(socketserver.StreamRequestHandler):
    def _send(self, obj: dict) -> None:
        try:
            self.wfile.write(json.dumps(obj, default=str).encode() + b"\n")
            self.wfile.flush()
        except OSError:
            pass

    def handle(self) -> None:
        self.request.settimeout(30)
        while True:
            try:
                line = self.rfile.readline(MAX_LINE + 1)
            except OSError:
                return
            if not line:
                return
            if len(line) > MAX_LINE:
                self._send(_err("request too large", "BAD_REQUEST"))
                return
            try:
                req = json.loads(line)
                if not isinstance(req, dict):
                    raise ValueError
            except ValueError:
                self._send(_err("bad JSON request", "BAD_REQUEST"))
                continue
            try:
                resp = self.server.dispatch(req)
            except Exception:
                resp = _err("internal error", "INTERNAL_ERROR")
            self._send(resp)


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, dispatch, mode: int, group: str | None):
        self.dispatch = dispatch
        if os.path.lexists(path):
            if not stat.S_ISSOCK(os.lstat(path).st_mode):
                raise RuntimeError(f"{path} exists and is not a socket")
            os.unlink(path)
        old = os.umask(0o177)
        try:
            super().__init__(path, _Handler)
        finally:
            os.umask(old)
        if group:
            shutil.chown(path, group=group)
        os.chmod(path, mode)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="aios.policy.daemon")
    ap.add_argument("--policy", required=True)
    ap.add_argument("--sandbox", required=True)
    ap.add_argument("--audit", required=True)
    ap.add_argument("--agent-socket", required=True)
    ap.add_argument("--admin-socket", required=True)
    ap.add_argument("--agent-group")
    ap.add_argument("--admin-group")
    ap.add_argument("--audit-mode", default="0640", help="octal, default 0640")
    ap.add_argument("--approval-ttl", type=float, default=300.0)
    args = ap.parse_args(argv)

    try:
        policy = load_policy(args.policy)
    except PolicyError as e:
        print(f"policy error: {e}")
        return 2

    service = PolicyService(
        FileTool(args.sandbox), policy,
        AuditLog(args.audit, mode=int(args.audit_mode, 8)),
        approval_ttl=args.approval_ttl,
    ).start(args.agent_socket, args.admin_socket,
            agent_group=args.agent_group, admin_group=args.admin_group)

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    print(f"aios policy service up: agent={args.agent_socket} admin={args.admin_socket}", flush=True)
    stop.wait()
    service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
