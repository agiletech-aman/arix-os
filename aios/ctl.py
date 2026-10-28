"""Admin CLI for the policy service (human use; talks to the admin socket).

  python -m aios.ctl pending
  python -m aios.ctl approve <id>     deny <id>
  python -m aios.ctl revoke           forget ask_once approvals
  python -m aios.ctl kill | resume    kill switch
  python -m aios.ctl status
"""

import argparse
import json
import os
import socket
import sys


def roundtrip(path: str, req: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(10)
        s.connect(path)
        s.sendall(json.dumps(req).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="aios.ctl")
    ap.add_argument("--socket", default=os.environ.get("AIOS_ADMIN_SOCKET", "/run/aios/admin.sock"))
    ap.add_argument("cmd", choices=["pending", "approve", "deny", "revoke", "kill", "resume", "status"])
    ap.add_argument("id", nargs="?")
    args = ap.parse_args(argv)
    req = {"cmd": args.cmd}
    if args.cmd in ("approve", "deny"):
        if not args.id:
            ap.error(f"{args.cmd} needs an id")
        req["id"] = args.id
    try:
        resp = roundtrip(args.socket, req)
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    print(json.dumps(resp, indent=2))
    return 0 if resp.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
