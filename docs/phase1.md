# Phase 1: VM + project structure + FileTool

## Assumptions
- Host RAM/GPU not given: VM gets 4 GB RAM, 4 vCPU, 40 GB disk, no GPU passthrough.
- Nothing from this project runs on the host except unit tests, which only touch
  temporary folders. The real sandbox (`~/aios-sandbox`) is used inside the VM.
- Existing `services/agent` (FastAPI task tracker) is untouched; it gets wired in later.

## Structure
```
arix-os/
├── aios/
│   ├── cli.py                 manual test CLI
│   ├── tools/
│   │   ├── base.py            ToolResult
│   │   └── file_tool.py       FileTool (sandboxed CRUD + trash)
│   ├── tests/test_file_tool.py
│   └── requirements-dev.txt
├── scripts/vm-create.sh       host: make disk + boot installer
├── scripts/vm-run.sh          host: boot installed VM (ssh on :2222)
└── services/agent/            existing task API
```

## 1. Host: install QEMU (once)
```bash
egrep -c '(vmx|svm)' /proc/cpuinfo        # must be > 0 (virtualization on in BIOS)
sudo apt update && sudo apt install -y qemu-system-x86 qemu-utils cpu-checker
kvm-ok                                    # "KVM acceleration can be used"
sudo usermod -aG kvm $USER                # then log out and back in
```

## 2. Host: create VM and install Ubuntu 24.04
Download `ubuntu-24.04.x-desktop-amd64.iso` from ubuntu.com, then:
```bash
cd ~/arix-os
scripts/vm-create.sh ~/Downloads/ubuntu-24.04.3-desktop-amd64.iso
```
Install normally (username e.g. `dev`, enable "Install OpenSSH" is not offered on
desktop, so run `sudo apt install -y openssh-server` after first boot). Shut the VM
down, then take a clean snapshot:
```bash
qemu-img snapshot -c clean-install ~/vms/aios-dev/disk.qcow2
# roll back later (VM must be off):
qemu-img snapshot -a clean-install ~/vms/aios-dev/disk.qcow2
```
Next boots: `scripts/vm-run.sh`

## 3. Copy the project into the VM
From the host (VM running):
```bash
rsync -av -e 'ssh -p 2222' --exclude .venv --exclude .git --exclude __pycache__ \
  ~/arix-os/ dev@localhost:~/arix-os/
```

## 4. Inside the VM: set up and test
```bash
sudo apt install -y python3-venv
cd ~/arix-os
python3 -m venv .venv && source .venv/bin/activate
pip install -r aios/requirements-dev.txt
python -m pytest aios -v          # expect 19 passed
```

## 5. Try it by hand (inside the VM)
```bash
export AIOS_SANDBOX=~/aios-sandbox
python -m aios.cli create notes/todo.txt --content "buy milk"
python -m aios.cli read notes/todo.txt
python -m aios.cli update notes/todo.txt --content " + eggs" --append
python -m aios.cli list_dir
python -m aios.cli delete notes/todo.txt      # prints a trash_id
python -m aios.cli list_trash
python -m aios.cli restore <trash_id>
python -m aios.cli read ../../etc/passwd      # must fail: path_denied
```

## FileTool behaviour
| Operation | Rule |
|---|---|
| create | fails if file exists; makes parent folders |
| read | UTF-8 text only, max 1 MB |
| update | atomic overwrite, or `--append`; file must exist |
| delete | moves to `<sandbox>/.trash/<id>`; never unlinks |
| restore | refuses to overwrite an existing file |
| any path | must resolve inside the sandbox (`..`, absolute paths and escaping symlinks are denied); `.trash` is not addressable |

Errors come back as `ToolResult(ok=False, error_code=...)`:
`path_denied, not_found, already_exists, wrong_type, too_large, not_text,
bad_arguments, unknown_operation, os_error`.

## Common errors
| Problem | Fix |
|---|---|
| `Could not access KVM kernel module` | enable VT-x/AMD-V in BIOS; `sudo usermod -aG kvm $USER` and re-login |
| `qemu-system-x86_64: command not found` | `sudo apt install qemu-system-x86` |
| VM window is tiny | set the resolution in Ubuntu Settings > Displays inside the VM |
| `ssh: connection refused` on :2222 | inside VM: `sudo apt install openssh-server`; VM must be started with `vm-run.sh` |
| `externally-managed-environment` from pip | you forgot the venv: `source .venv/bin/activate` |
| `ModuleNotFoundError: aios` | run commands from the `~/arix-os` folder |
| Slow VM | raise `RAM_MB=6144 CPUS=6 scripts/vm-run.sh` if the host can spare it |

## Not in this phase (on purpose)
No Policy Engine, no audit log, no kill switch yet. FileTool's sandbox and trash are a
safety floor only; Phase 2 puts the Policy Engine in front of `FileTool.call()`.
