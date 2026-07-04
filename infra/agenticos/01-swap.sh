#!/usr/bin/env bash
# GOL-53 P0.1 — Swap file OOM safety net for the AgenticOS droplet (DO id 572389418, nyc1).
#
# Why: the box OOM-crashed at RAM ~103% with no swap. A swap file lets a RAM spike
# degrade (page out) instead of hard-killing the UI/control-plane. Low swappiness
# keeps hot pages in RAM so this is a safety net, not a perf regression.
#
# Safe to run twice (idempotent): skips creation if /swapfile already active, and
# only appends fstab/sysctl lines once. No reboot required.
#
# Run ON the AgenticOS host as root:
#   sudo bash 01-swap.sh
set -euo pipefail

SWAPFILE=/swapfile
SWAPSIZE=4G          # tune via SWAPSIZE env (default 4G; box currently ~2GB tier)
SWAPPINESS=10        # prefer RAM; only swap under real pressure

SWAPSIZE="${SWAPSIZE:-4G}"

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: must run as root (sudo)." >&2
  exit 1
fi

echo "==> AgenticOS swap setup (size=${SWAPSIZE}, swappiness=${SWAPPINESS})"

# 1. Create the swap file if it is not already an active swap device.
if swapon --show=NAME --noheadings 2>/dev/null | grep -qx "${SWAPFILE}"; then
  echo "    swap already active on ${SWAPFILE}; skipping create/enable."
else
  if [[ -f "${SWAPFILE}" ]]; then
    echo "    ${SWAPFILE} exists but is not active; re-initialising."
    swapoff "${SWAPFILE}" 2>/dev/null || true
  fi
  # fallocate is instant; fall back to dd on filesystems that reject it (e.g. some overlay).
  if ! fallocate -l "${SWAPSIZE}" "${SWAPFILE}" 2>/dev/null; then
    echo "    fallocate unsupported; using dd (slower)."
    # convert e.g. 4G -> 4096 MiB blocks of 1M
    MB=$(( $(numfmt --from=iec "${SWAPSIZE}") / 1024 / 1024 ))
    dd if=/dev/zero of="${SWAPFILE}" bs=1M count="${MB}" status=progress
  fi
  chmod 600 "${SWAPFILE}"
  mkswap "${SWAPFILE}"
  swapon "${SWAPFILE}"
  echo "    swap enabled."
fi

# 2. Persist across reboots via fstab (append once).
if ! grep -qE "^\s*${SWAPFILE}\s+none\s+swap" /etc/fstab; then
  echo "${SWAPFILE} none swap sw 0 0" >> /etc/fstab
  echo "    fstab entry added."
else
  echo "    fstab entry already present; skipping."
fi

# 3. Set swappiness now and persist it.
sysctl -w vm.swappiness="${SWAPPINESS}"
SYSCTL_FILE=/etc/sysctl.d/99-swap.conf
if [[ ! -f "${SYSCTL_FILE}" ]] || ! grep -qE "^\s*vm.swappiness" "${SYSCTL_FILE}"; then
  echo "vm.swappiness=${SWAPPINESS}" > "${SYSCTL_FILE}"
  echo "    persisted vm.swappiness=${SWAPPINESS} to ${SYSCTL_FILE}."
else
  echo "    ${SYSCTL_FILE} already sets vm.swappiness; leaving as-is."
fi

echo "==> Done. Current swap:"
swapon --show
free -h

# Rollback:
#   swapoff /swapfile && rm -f /swapfile
#   sed -i '\#^/swapfile none swap#d' /etc/fstab
#   rm -f /etc/sysctl.d/99-swap.conf && sysctl -w vm.swappiness=60
