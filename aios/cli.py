"""Manual test CLI:  python -m aios.cli <operation> [path] [--content TEXT]

Sandbox folder: $AIOS_SANDBOX (default ~/aios-sandbox).
"""

import argparse
import json
import os
import sys

from aios.tools import FileTool


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="aios.cli")
    parser.add_argument("operation", choices=FileTool.OPERATIONS)
    parser.add_argument("path", nargs="?", help="path (or trash id for restore)")
    parser.add_argument("--content", default="")
    parser.add_argument("--append", action="store_true")
    args = parser.parse_args(argv)

    tool = FileTool(os.environ.get("AIOS_SANDBOX", "~/aios-sandbox"))
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
