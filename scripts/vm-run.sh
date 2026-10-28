#!/usr/bin/env bash
# Run on the HOST. Boots the installed dev VM (no ISO). ssh: ssh -p 2222 <user>@localhost
set -euo pipefail

VM_DIR="${VM_DIR:-$HOME/vms/aios-dev}"
RAM_MB="${RAM_MB:-4096}"
CPUS="${CPUS:-4}"

exec qemu-system-x86_64 \
  -enable-kvm -cpu host -m "$RAM_MB" -smp "$CPUS" \
  -drive file="$VM_DIR/disk.qcow2",if=virtio \
  -device virtio-vga -display gtk \
  -nic user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:2222-:22
