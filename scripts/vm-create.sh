#!/usr/bin/env bash
# Run on the HOST. Creates an empty QEMU disk and boots the Ubuntu 24.04 installer.
# Usage: scripts/vm-create.sh /path/to/ubuntu-24.04-desktop-amd64.iso
set -euo pipefail

ISO="${1:?usage: $0 /path/to/ubuntu-24.04-desktop-amd64.iso}"
VM_DIR="${VM_DIR:-$HOME/vms/aios-dev}"
RAM_MB="${RAM_MB:-4096}"
CPUS="${CPUS:-4}"
DISK_GB="${DISK_GB:-40}"

mkdir -p "$VM_DIR"
if [ ! -f "$VM_DIR/disk.qcow2" ]; then
  qemu-img create -f qcow2 "$VM_DIR/disk.qcow2" "${DISK_GB}G"
fi

exec qemu-system-x86_64 \
  -enable-kvm -cpu host -m "$RAM_MB" -smp "$CPUS" \
  -drive file="$VM_DIR/disk.qcow2",if=virtio \
  -cdrom "$ISO" -boot d \
  -device virtio-vga -display gtk \
  -nic user,model=virtio-net-pci
