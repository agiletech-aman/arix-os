# Phase 2b: Policy service (separate process) + L1 + kill switch

## What changed
- **Levels** are now L0 `allow`, L1 `ask_once`, L2 `confirm`, L3 `deny`.
  `ask_once` asks the human once per operation per session; `revoke` forgets it.
- **Policy service** (`aios/policy/daemon.py`) runs FileTool behind the Policy Engine in
  its own process. The AI only gets `agent.sock`; it cannot touch the sandbox, the
  policy or the audit log directly.
- **Approvals** exist only on `admin.sock`. A `confirm` approval is single-use, bound to the
  exact operation + arguments, and expires after 5 minutes.
- **Kill switch**: `aios.ctl kill` makes every call return `KILLED` until `resume`.
  (The global hotkey comes with the AI Shell, Phase 6; it will just call this.)
- Service is **fail closed**: if it is unreachable, the client returns
  `POLICY_SERVICE_UNAVAILABLE` and nothing runs.

## Layout in the VM after install
| Thing | Owner | Why |
|---|---|---|
| `/opt/aios` (code) | root, read-only | AI and service cannot edit the code |
| `/etc/aios/policy.toml` | root, 0644 | service reads it, nobody else can change it |
| `/var/lib/aios/sandbox` | aios-policy, 0700 | only the service touches files |
| `/var/log/aios/audit.jsonl` | aios-policy, group aios-admin 0640 | AI cannot edit or delete the log |
| `/run/aios/agent.sock` | 0660 group aios-agents | the AI user |
| `/run/aios/admin.sock` | 0600 group aios-admin | you |

Policy is TOML (stdlib `tomllib`, no root `pip`). To change policy: edit
`/etc/aios/policy.toml` as root, then `sudo systemctl restart aios-policyd`.

## Test without installing (any machine, no root)
```bash
python -m pytest aios -q                      # 67 passed
```

## Install in the VM (needs root; run only inside the VM)
```bash
# host: sync, then in the VM:
sudo scripts/install-policyd.sh <your-login>
# log out/in once so you are in group aios-admin
```

## Try it
```bash
# the AI side: run as aios-agent (cannot read the sandbox or the log)
sudo -u aios-agent env AIOS_SOCKET=/run/aios/agent.sock ~/arix-os/.venv/bin/python -m aios.cli ...
# simpler for a first test, as yourself (you are not in aios-agents yet):
sudo usermod -aG aios-agents $USER   # re-login

export AIOS_SOCKET=/run/aios/agent.sock AIOS_ADMIN_SOCKET=/run/aios/admin.sock
python -m aios.cli create a.txt --content hi          # allow
python -m aios.cli update a.txt --content v2          # L1: CONFIRMATION_REQUIRED + approval_id
python -m aios.ctl pending
python -m aios.ctl approve <id>
python -m aios.cli update a.txt --content v3          # runs; no more prompts this session
python -m aios.cli delete a.txt                       # L2: needs approval every time
python -m aios.ctl approve <id>
python -m aios.cli delete a.txt --approval <id>
python -m aios.ctl kill      # everything returns KILLED
python -m aios.ctl resume
sudo tail -n 5 /var/log/aios/audit.jsonl
sudo -u aios-agent cat /var/log/aios/audit.jsonl      # must fail: permission denied
sudo -u aios-agent ls /var/lib/aios/sandbox           # must fail: permission denied
```

## Common errors
| Problem | Fix |
|---|---|
| `POLICY_SERVICE_UNAVAILABLE` | `systemctl status aios-policyd`; `journalctl -u aios-policyd -n 30` |
| `Permission denied` on the socket | you are not in the group yet: re-login, check `id` |
| `AF_UNIX path too long` | socket paths must be under ~100 characters |
| `NOT_FOUND` on approve | the request expired (5 min) or was already used: repeat the call |
| service won't start, policy error | fix `/etc/aios/policy.toml`; `python3 -c "import tomllib;tomllib.load(open('/etc/aios/policy.toml','rb'))"` |

## Known limits (honest list)
- `install-policyd.sh` and the systemd unit are written but **not yet run on a real VM**; the
  service itself is tested over real sockets in `aios/tests/test_service.py`.
- Admin socket trust = Unix permissions (group). A human at a terminal approves; there is no
  GUI prompt until Phase 6.
- Policy matching covers `..` and symlinks. Hard links and race conditions between check and
  use (TOCTOU) are not covered; the sandbox is only reachable through the service, which
  limits who can plant links.
- Rules protect by path only; there is no per-user or per-app policy yet.
