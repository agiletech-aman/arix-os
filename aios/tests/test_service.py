"""Phase 2b tests: ask_once level, policy service over sockets, approvals, kill switch."""

import json
import os
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

from aios.ctl import roundtrip as admin
from aios.policy import AuditLog, Level, Policy, PolicyEngine, load_policy
from aios.policy.client import PolicyClient
from aios.policy.daemon import PolicyService
from aios.tools import FileTool

POLICY = {
    "default": "deny",
    "operations": {
        "read": "allow", "create": "allow", "list_dir": "allow", "list_trash": "allow",
        "update": "ask_once", "delete": "confirm",
    },
    "rules": [{"paths": [".env", "secrets/*"], "level": "deny", "reason": "protected path"}],
}


# ---------- ask_once in-process ----------

def test_ask_once_is_between_allow_and_confirm():
    assert Level.ALLOW < Level.ASK_ONCE < Level.CONFIRM < Level.DENY
    assert Level.parse("ask_once") is Level.ASK_ONCE


def test_ask_once_asks_once_per_operation(tmp_path):
    asked = []
    eng = PolicyEngine(FileTool(tmp_path / "sb"), Policy.from_dict(POLICY),
                       confirm=lambda op, a, r: asked.append(op) or True)
    eng.call("create", path="a.txt", content="1")
    eng.call("create", path="b.txt", content="1")
    assert eng.call("update", path="a.txt", content="2").ok
    assert eng.call("update", path="b.txt", content="2").ok   # no second prompt
    assert asked == ["update"]
    eng.call("delete", path="a.txt")
    eng.call("delete", path="b.txt")                           # confirm: asked every time
    assert asked == ["update", "delete", "delete"]


def test_ask_once_declined_is_not_remembered(tmp_path):
    answers = iter([False, True])
    eng = PolicyEngine(FileTool(tmp_path / "sb"), Policy.from_dict(POLICY),
                       confirm=lambda *a: next(answers))
    eng.call("create", path="a.txt")
    assert eng.call("update", path="a.txt", content="x").error_code == "CONFIRMATION_REQUIRED"
    assert eng.call("update", path="a.txt", content="x").ok


def test_revoke_session(tmp_path):
    asked = []
    eng = PolicyEngine(FileTool(tmp_path / "sb"), Policy.from_dict(POLICY),
                       confirm=lambda op, a, r: asked.append(op) or True)
    eng.call("create", path="a.txt")
    eng.call("update", path="a.txt", content="1")
    eng.revoke_session()
    eng.call("update", path="a.txt", content="2")
    assert asked == ["update", "update"]


# ---------- service fixture ----------

@pytest.fixture
def svc():
    d = Path(tempfile.mkdtemp(prefix="aios"))   # short path: AF_UNIX limit is ~108 chars
    audit = AuditLog(d / "audit.jsonl")
    service = PolicyService(FileTool(d / "sb"), Policy.from_dict(POLICY), audit)
    service.start(d / "agent.sock", d / "admin.sock")
    service.dir = d
    service.agent = PolicyClient(str(d / "agent.sock"))
    service.admin = lambda **req: admin(str(d / "admin.sock"), req)
    service.audit_path = d / "audit.jsonl"
    yield service
    service.stop()
    shutil.rmtree(d, ignore_errors=True)


# ---------- service: basic ----------

def test_call_through_socket(svc):
    assert svc.agent.call("create", path="a.txt", content="hi").ok
    assert svc.agent.call("read", path="a.txt").data["content"] == "hi"
    assert "delete" in svc.agent.OPERATIONS


def test_deny_through_socket(svc):
    res = svc.agent.call("read", path=".env")
    assert res.error_code == "POLICY_DENIED"
    assert svc.agent.call("read", path="foo/../secrets/k").error_code == "POLICY_DENIED"


def test_fails_closed_when_service_down(tmp_path):
    res = PolicyClient(str(tmp_path / "nope.sock")).call("read", path="a")
    assert not res.ok and res.error_code == "POLICY_SERVICE_UNAVAILABLE"


def test_socket_permissions(svc):
    assert oct(os.stat(svc.dir / "agent.sock").st_mode & 0o777) == "0o660"
    assert oct(os.stat(svc.dir / "admin.sock").st_mode & 0o777) == "0o600"


def test_agent_socket_has_no_admin_commands(svc):
    s = PolicyClient(str(svc.dir / "agent.sock"))
    for cmd in ("approve", "pending", "kill", "resume", "revoke", "status"):
        resp = s._roundtrip({"cmd": cmd, "id": "x"})
        assert not resp["ok"] and resp["error_code"] == "BAD_REQUEST", cmd


def test_malformed_requests_do_not_kill_service(svc):
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(str(svc.dir / "agent.sock"))
        s.sendall(b"not json\n[1,2]\n")
        data = s.recv(4096).decode().splitlines()
    assert all(json.loads(x)["error_code"] == "BAD_REQUEST" for x in data)
    resp = svc.agent._roundtrip({"cmd": "call", "operation": "read", "args": {"operation": "x"}})
    assert resp["error_code"] == "BAD_REQUEST"
    resp = svc.agent._roundtrip({"cmd": "call", "operation": 5, "args": {}})
    assert resp["error_code"] == "BAD_REQUEST"
    assert svc.agent.call("create", path="ok.txt").ok


# ---------- service: approvals ----------

def test_confirm_flow_single_use_and_bound_to_args(svc):
    svc.agent.call("create", path="a.txt", content="x")
    svc.agent.call("create", path="b.txt", content="x")

    res = svc.agent.call("delete", path="a.txt")
    assert res.error_code == "CONFIRMATION_REQUIRED"
    aid = res.meta["approval_id"]

    pending = svc.admin(cmd="pending")["data"]
    assert [p["id"] for p in pending] == [aid] and pending[0]["operation"] == "delete"

    # not approved yet -> still blocked
    assert svc.agent.call("delete", path="a.txt", approval_id=aid).error_code == "CONFIRMATION_REQUIRED"

    assert svc.admin(cmd="approve", id=aid)["ok"]
    assert svc.agent.call("delete", path="b.txt", approval_id=aid).error_code == "CONFIRMATION_REQUIRED"
    assert svc.agent.call("delete", path="a.txt", approval_id=aid).ok          # exact match works
    assert svc.agent.call("delete", path="a.txt", approval_id=aid).error_code in (
        "CONFIRMATION_REQUIRED", "not_found")                                   # single use
    assert svc.agent.call("read", path="a.txt").error_code == "not_found"      # it really went to trash


def test_agent_cannot_self_approve(svc):
    svc.agent.call("create", path="a.txt")
    aid = svc.agent.call("delete", path="a.txt").meta["approval_id"]
    for fake in (aid, "approved", "yes", ""):
        assert svc.agent.call("delete", path="a.txt", approval_id=fake).error_code == "CONFIRMATION_REQUIRED"
    assert svc.agent.call("read", path="a.txt").ok


def test_deny_request(svc):
    svc.agent.call("create", path="a.txt")
    aid = svc.agent.call("delete", path="a.txt").meta["approval_id"]
    assert svc.admin(cmd="deny", id=aid)["ok"]
    assert svc.admin(cmd="approve", id=aid)["error_code"] == "NOT_FOUND"
    assert svc.admin(cmd="pending")["data"] == []


def test_approvals_expire():
    d = Path(tempfile.mkdtemp(prefix="aios"))
    try:
        t = [0.0]
        from aios.policy.approvals import Approvals
        ap = Approvals(ttl=10, clock=lambda: t[0])
        key = ap.request("delete", {"path": "a"}, "r", Level.CONFIRM)
        ap.approve(key)
        t[0] = 11
        assert not ap.consume(key, "delete", {"path": "a"})
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_ask_once_flow_via_admin(svc):
    svc.agent.call("create", path="a.txt")
    svc.agent.call("create", path="b.txt")
    res = svc.agent.call("update", path="a.txt", content="1")
    assert res.error_code == "CONFIRMATION_REQUIRED"
    svc.admin(cmd="approve", id=res.meta["approval_id"])
    assert svc.agent.call("update", path="a.txt", content="1").ok
    assert svc.agent.call("update", path="b.txt", content="2").ok               # session grant
    assert svc.admin(cmd="status")["data"]["session_grants"] == ["update"]

    svc.admin(cmd="revoke")
    assert svc.agent.call("update", path="b.txt", content="3").error_code == "CONFIRMATION_REQUIRED"


def test_pending_view_hides_content(svc):
    svc.agent.call("create", path="a.txt")
    svc.agent.call("update", path="a.txt", content="very secret text")
    pending = json.dumps(svc.admin(cmd="pending")["data"])
    assert "very secret text" not in pending and "sha256" in pending


# ---------- service: kill switch + audit ----------

def test_kill_switch(svc):
    assert svc.agent.call("create", path="a.txt").ok
    svc.admin(cmd="kill")
    for op, args in [("read", {"path": "a.txt"}), ("create", {"path": "b.txt"}), ("list_dir", {})]:
        assert svc.agent.call(op, **args).error_code == "KILLED"
    assert not (svc.dir / "sb" / "b.txt").exists()
    svc.admin(cmd="resume")
    assert svc.agent.call("read", path="a.txt").ok


def test_audit_covers_agent_and_admin_actions(svc):
    svc.agent.call("create", path="a.txt", content="topsecret")
    svc.agent.call("read", path=".env")
    aid = svc.agent.call("delete", path="a.txt").meta["approval_id"]
    svc.admin(cmd="approve", id=aid)
    svc.agent.call("delete", path="a.txt", approval_id=aid)
    svc.admin(cmd="kill")
    svc.agent.call("read", path="a.txt")
    text = svc.audit_path.read_text()
    rows = [json.loads(x) for x in text.splitlines()]
    assert [r["decision"] for r in rows] == ["allow", "deny", "confirm", "admin", "confirmed", "admin", "deny"]
    assert rows[-1]["error_code"] == "KILLED" and rows[3]["operation"] == "admin:approve"
    assert "topsecret" not in text


def test_ctl_cli(svc, capsys):
    from aios import ctl
    assert ctl.main(["--socket", str(svc.dir / "admin.sock"), "status"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["killed"] is False
    assert ctl.main(["--socket", str(svc.dir / "nope.sock"), "status"]) == 2


def test_cli_service_mode(svc, monkeypatch, capsys):
    from aios import cli
    sock = str(svc.dir / "agent.sock")
    assert cli.main(["--socket", sock, "create", "a.txt", "--content", "hi"]) == 0
    capsys.readouterr()
    assert cli.main(["--socket", sock, "delete", "a.txt"]) == 1
    aid = json.loads(capsys.readouterr().out)["meta"]["approval_id"]
    svc.admin(cmd="approve", id=aid)
    assert cli.main(["--socket", sock, "delete", "a.txt", "--approval", aid]) == 0
