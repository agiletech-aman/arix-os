"""Manual test CLI:  python -m aios.cli <operation> [path] [--content TEXT]

Sandbox folder: $AIOS_SANDBOX (default ~/aios-sandbox).
Policy file:    --policy FILE or $AIOS_POLICY (default: built-in policy).
Audit log:      $AIOS_AUDIT (default ~/.aios/audit.jsonl).
`confirm`-level operations (e.g. delete) need --yes, or a y/N prompt on a terminal.
"""

import argparse
import json
import os
import sys

from aios.policy import AuditLog, PolicyEngine, PolicyError, load_policy
from aios.tools import FileTool


def _make_confirm(assume_yes: bool):
    def confirm(operation: str, args: dict, reason: str) -> bool:
        if assume_yes:
            return True
        if not sys.stdin.isatty():
            return False
        target = args.get("path") or args.get("trash_id") or ""
        ans = input(f"Allow {operation} {target} ({reason})? [y/N] ")
        return ans.strip().lower() in {"y", "yes"}
    return confirm


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="aios.cli")
    parser.add_argument("operation", choices=FileTool.OPERATIONS)
    parser.add_argument("path", nargs="?", help="path (or trash id for restore)")
    parser.add_argument("--content", default="")
    parser.add_argument("--append", action="store_true")
    parser.add_argument("--policy", default=os.environ.get("AIOS_POLICY"))
    parser.add_argument("--yes", "-y", action="store_true", help="approve confirm-level ops")
    args = parser.parse_args(argv)

    try:
        policy = load_policy(args.policy)
    except PolicyError as e:
        print(json.dumps({"ok": False, "error": str(e), "error_code": "POLICY_LOAD_ERROR"}, indent=2))
        return 2

    tool = PolicyEngine(
        FileTool(os.environ.get("AIOS_SANDBOX", "~/aios-sandbox")),
        policy,
        audit=AuditLog(os.environ.get("AIOS_AUDIT", "~/.aios/audit.jsonl")),
        confirm=_make_confirm(args.yes),
    )

    op, kwargs = args.operation, {}
    if op in {"create", "update"}:
        kwargs = {"path": args.path, "content": args.content}
        if op == "update":
            kwargs["append"] = args.append
    elif op == "restore":
        kwargs = {"trash_id": args.path}
    elif op == "list_dir":
        kwargs = {"path": args.path or "."}
    elif op != "list_trash":
        kwargs = {"path": args.path}

    result = tool.call(op, **kwargs)
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
