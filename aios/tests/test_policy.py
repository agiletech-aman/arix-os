"""Phase 2 tests: policy loader, decisions, engine, audit log, CLI wiring."""

import json

import pytest

from aios.policy import AuditLog, Level, Policy, PolicyEngine, PolicyError, load_policy
from aios.tools import FileTool
from aios.tools.base import ToolResult


# ---------- helpers ----------

class FakeTool:
    OPERATIONS = ("create", "read", "update", "delete", "restore", "mkdir", "list_dir", "list_trash")

    def __init__(self):
        self.calls = []

    def call(self, operation, **args):
        self.calls.append((operation, args))
        return ToolResult(ok=True, data={"op": operation})


def write_policy(tmp_path, text):
    p = tmp_path / "policy.toml"
    p.write_text(text)
    return p


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


# ---------- levels / loader ----------

def test_level_order_and_parse():
    assert Level.ALLOW < Level.CONFIRM < Level.DENY
    assert Level.parse(" Confirm ") is Level.CONFIRM
    with pytest.raises(ValueError):
        Level.parse("maybe")


def test_default_policy_loads():
    p = load_policy()
    assert p.decide("read", "a.txt")[0] is Level.ALLOW
    assert p.decide("delete", "a.txt")[0] is Level.CONFIRM
    assert p.decide("read", ".env")[0] is Level.DENY
    assert p.decide("read", "secrets/db.txt")[0] is Level.DENY
    assert p.decide("read", "deep/dir/server.key")[0] is Level.DENY
    assert p.decide("something_new")[0] is Level.DENY  # default = deny
    assert p.max_content_bytes == 1048576


def test_load_from_file(tmp_path):
    f = write_policy(tmp_path, 'default = "allow"\n[operations]\ndelete = "deny"\n')
    p = load_policy(f)
    assert p.decide("read")[0] is Level.ALLOW
    assert p.decide("delete", "x")[0] is Level.DENY


@pytest.mark.parametrize("text", [
    'default = "nope"',
    '[operations]\nread = "sometimes"',
    '[[rules]]\npaths = ["a"]',                       # missing level
    '[[rules]]\nlevel = "deny"\nbogus = 1',           # unknown key
    'default = ',                                      # bad TOML
])
def test_bad_policy_raises(tmp_path, text):
    with pytest.raises(PolicyError):
        load_policy(write_policy(tmp_path, text))


def test_missing_policy_file(tmp_path):
    with pytest.raises(PolicyError):
        load_policy(tmp_path / "nope.toml")


def test_rules_only_tighten():
    p = Policy.from_dict({
        "default": "allow",
        "operations": {"delete": "deny"},
        "rules": [{"level": "allow", "operations": ["delete"], "paths": ["tmp/*"]}],
    })
    assert p.decide("delete", "tmp/x")[0] is Level.DENY


def test_path_normalisation_cannot_dodge_rule():
    p = load_policy()
    for path in ["./.env", "/.env", "./secrets/a", "secrets//a",
                 "foo/../secrets/a", "x/../.env", "a/b/../../secrets/k", "secrets/./a"]:
        assert p.decide("read", path)[0] is Level.DENY, path


def test_rule_with_paths_skips_pathless_ops():
    p = Policy.from_dict({"default": "allow",
                          "rules": [{"level": "deny", "paths": ["*"]}]})
    assert p.decide("list_trash", None)[0] is Level.ALLOW
    assert p.decide("read", "a")[0] is Level.DENY


# ---------- engine ----------

def test_allow_passes_through(audit):
    tool = FakeTool()
    res = PolicyEngine(tool, load_policy(), audit=audit).call("read", path="a.txt")
    assert res.ok and tool.calls == [("read", {"path": "a.txt"})]
    assert res.meta["policy"]["decision"] == "allow"


def test_deny_never_reaches_tool(audit):
    tool = FakeTool()
    res = PolicyEngine(tool, load_policy(), audit=audit).call("read", path=".env")
    assert not res.ok and res.error_code == "POLICY_DENIED"
    assert tool.calls == []


def test_unknown_operation(audit):
    tool = FakeTool()
    res = PolicyEngine(tool, load_policy(), audit=audit).call("format_disk", path="/")
    assert res.error_code == "UNKNOWN_OPERATION" and tool.calls == []


def test_confirm_without_callback_is_blocked(audit):
    tool = FakeTool()
    res = PolicyEngine(tool, load_policy(), audit=audit).call("delete", path="a.txt")
    assert res.error_code == "CONFIRMATION_REQUIRED" and tool.calls == []


@pytest.mark.parametrize("answer,ok", [(True, True), (False, False)])
def test_confirm_callback(audit, answer, ok):
    tool = FakeTool()
    seen = []

    def confirm(op, args, reason):
        seen.append((op, args["path"], reason))
        return answer

    res = PolicyEngine(tool, load_policy(), audit=audit, confirm=confirm).call("delete", path="a.txt")
    assert res.ok is ok
    assert seen == [("delete", "a.txt", "operation:delete")]
    assert len(tool.calls) == (1 if ok else 0)
    if ok:
        assert res.meta["policy"]["decision"] == "confirmed"


def test_confirm_callback_that_raises_is_a_no(audit):
    def boom(*_):
        raise RuntimeError("x")
    res = PolicyEngine(FakeTool(), load_policy(), audit=audit, confirm=boom).call("delete", path="a")
    assert res.error_code == "CONFIRMATION_REQUIRED"


def test_content_limit(audit):
    p = Policy.from_dict({"default": "allow", "limits": {"max_content_bytes": 4}})
    tool = FakeTool()
    eng = PolicyEngine(tool, p, audit=audit)
    assert eng.call("create", path="a", content="1234").ok
    res = eng.call("create", path="b", content="12345")
    assert res.error_code == "POLICY_TOO_LARGE" and len(tool.calls) == 1


def test_tool_exception_becomes_result(audit):
    class Raising(FakeTool):
        def call(self, operation, **args):
            raise RuntimeError("kaboom")
    res = PolicyEngine(Raising(), load_policy(), audit=audit).call("read", path="a")
    assert not res.ok and res.error_code == "INTERNAL_ERROR"


def test_check_is_dry_run(audit):
    tool = FakeTool()
    eng = PolicyEngine(tool, load_policy(), audit=audit)
    assert eng.check("delete", path="a")[0] is Level.CONFIRM
    assert tool.calls == [] and audit.tail() == []


# ---------- audit ----------

def test_audit_records_every_decision_and_hides_content(audit):
    eng = PolicyEngine(FakeTool(), load_policy(), audit=audit, confirm=lambda *a: True)
    eng.call("create", path="a.txt", content="top secret")
    eng.call("read", path=".env")
    eng.call("delete", path="a.txt")
    rows = audit.tail()
    assert [r["decision"] for r in rows] == ["allow", "deny", "confirmed"]
    assert rows[0]["args"]["content"]["bytes"] == 10
    assert "top secret" not in audit.path.read_text()
    assert rows[1]["error_code"] == "POLICY_DENIED"
    assert all({"ts", "tool", "operation", "reason", "ok"} <= r.keys() for r in rows)


def test_audit_failure_does_not_break_call(tmp_path):
    a = AuditLog(tmp_path / "audit.jsonl")
    a.path.mkdir()  # make the log path a directory -> write fails
    res = PolicyEngine(FakeTool(), load_policy(), audit=a).call("read", path="x")
    assert res.ok


# ---------- integration with the real FileTool ----------

def test_real_filetool_round_trip(tmp_path, audit):
    eng = PolicyEngine(FileTool(str(tmp_path / "sb")), load_policy(), audit=audit,
                       confirm=lambda *a: True)
    assert eng.call("create", path="note.txt", content="hi").ok
    assert eng.call("read", path="note.txt").ok
    assert eng.call("create", path=".env", content="X=1").error_code == "POLICY_DENIED"
    assert not (tmp_path / "sb" / ".env").exists()
    assert eng.call("delete", path="note.txt").ok


# ---------- CLI ----------

def test_cli_delete_needs_yes(tmp_path, monkeypatch, capsys):
    from aios import cli
    monkeypatch.setenv("AIOS_SANDBOX", str(tmp_path / "sb"))
    monkeypatch.setenv("AIOS_AUDIT", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("AIOS_POLICY", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)

    assert cli.main(["create", "a.txt", "--content", "hi"]) == 0
    capsys.readouterr()
    assert cli.main(["delete", "a.txt"]) == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "CONFIRMATION_REQUIRED"
    assert cli.main(["delete", "a.txt", "--yes"]) == 0
    assert cli.main(["read", ".env"]) == 1


def test_cli_bad_policy(tmp_path, monkeypatch, capsys):
    from aios import cli
    monkeypatch.setenv("AIOS_SANDBOX", str(tmp_path / "sb"))
    monkeypatch.setenv("AIOS_AUDIT", str(tmp_path / "audit.jsonl"))
    assert cli.main(["read", "a", "--policy", str(tmp_path / "missing.toml")]) == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "POLICY_LOAD_ERROR"


def test_symlink_cannot_dodge_protected_path(tmp_path, audit):
    sb = tmp_path / "sb"
    (sb / "secrets").mkdir(parents=True)
    (sb / "secrets" / "k.txt").write_text("TOP")
    (sb / "alias").symlink_to(sb / "secrets")
    eng = PolicyEngine(FileTool(str(sb)), load_policy(), audit=audit)

    assert eng.call("read", path="alias/k.txt").error_code == "POLICY_DENIED"
    assert eng.call("read", path="foo/../secrets/k.txt").error_code == "POLICY_DENIED"
    assert eng.call("update", path="alias/k.txt", content="x").error_code == "POLICY_DENIED"
    assert (sb / "secrets" / "k.txt").read_text() == "TOP"
