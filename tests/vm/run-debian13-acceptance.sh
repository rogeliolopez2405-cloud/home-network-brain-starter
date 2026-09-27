#!/usr/bin/env bash
# Clean Debian 13 (x86_64) VM acceptance for M1.
#
#   tests/vm/run-debian13-acceptance.sh <starter.tar.gz>
#
# Boots the official Debian 13 genericcloud image (checksum-pinned) under QEMU
# (KVM when available), copies a `git archive` export of the Starter into it,
# runs ./install.sh --yes, then drives the M1 definition of done:
# bootstrap → sign in → Hub → control a light → SQLite → audit → verified
# backup → REBOOT (new boot id) → healthy, auth, state and audit preserved →
# provider outage shows CAN'T REACH → restore → re-install (upgrade path).
# Needs: qemu-system-x86, qemu-utils, cloud-image-utils, python3, ssh.
set -Eeuo pipefail

TARBALL="$(readlink -f "${1:?usage: $0 <starter.tar.gz>}")"
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="${WORK:-$PWD/vm-work}"
IMG_BUILD=20260914-2601
IMG_URL="https://cloud.debian.org/images/cloud/trixie/${IMG_BUILD}/debian-13-genericcloud-amd64-${IMG_BUILD}.qcow2"
IMG_SHA512=95e110dfcdbd0ed8a82a75ed9579802f9950cabf51a810dcc6388e81bc778188713878b9f28d583a0ea602fbf48b35996ae9ad37f584166d8fbd6489df248f53
SSH_PORT=2222; WEB_PORT=18780
BASE="http://127.0.0.1:${WEB_PORT}"

mkdir -p "$WORK"; cd "$WORK"
log() { printf '\n\033[1m### %s\033[0m\n' "$*"; }

log "Debian 13 cloud image ${IMG_BUILD}"
[ -f base.qcow2 ] || curl -fsSL --retry 3 -o base.qcow2 "$IMG_URL"
echo "${IMG_SHA512}  base.qcow2" | sha512sum -c -
qemu-img create -q -f qcow2 -F qcow2 -b base.qcow2 disk.qcow2 20G

rm -f id id.pub; ssh-keygen -q -t ed25519 -N '' -f id
cat > user-data <<EOF
#cloud-config
hostname: hnb-acceptance
users:
  - name: debian
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    shell: /bin/bash
    ssh_authorized_keys: ["$(cat id.pub)"]
ssh_pwauth: false
EOF
printf 'instance-id: hnb-m1-acceptance\nlocal-hostname: hnb-acceptance\n' > meta-data
cloud-localds seed.iso user-data meta-data

if [ -w /dev/kvm ]; then ACCEL=(-enable-kvm -cpu host); echo "KVM: yes"; else ACCEL=(-accel tcg,thread=multi -cpu max); echo "KVM: no (software emulation, slow)"; fi
qemu-system-x86_64 -machine q35 "${ACCEL[@]}" -smp 2 -m 3072 \
  -drive file=disk.qcow2,if=virtio,format=qcow2 \
  -drive file=seed.iso,if=virtio,format=raw,readonly=on \
  -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22,hostfwd=tcp:127.0.0.1:${WEB_PORT}-:8780" \
  -device virtio-net-pci,netdev=n0 \
  -display none -serial file:serial.log -daemonize -pidfile qemu.pid

SSH="ssh -i $WORK/id -p ${SSH_PORT} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o ServerAliveInterval=10 -o LogLevel=ERROR debian@127.0.0.1"
SCP="scp -i $WORK/id -P ${SSH_PORT} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
wait_ssh() { timeout 1200 bash -c "until $SSH true 2>/dev/null; do sleep 5; done"; }

log "Waiting for the VM"
wait_ssh
$SSH "cloud-init status --wait >/dev/null; . /etc/os-release; echo \"\$PRETTY_NAME \$(dpkg --print-architecture) kernel \$(uname -r)\"" | tee vm-os.txt
$SSH "command -v docker >/dev/null && echo 'docker preinstalled (NOT clean)' || echo 'no docker: clean image'" | tee -a vm-os.txt

log "Copying the clean export into the VM"
$SCP "$TARBALL" debian@127.0.0.1:starter.tar.gz
$SSH "mkdir -p starter && tar -xzf starter.tar.gz -C starter && ls starter | head -40"

PHASE_FAIL=0
run_phase() { python3 "$HERE/acceptance.py" "$1" --base "$BASE" --ssh "$SSH" || PHASE_FAIL=1; }

log "install.sh --yes"
$SSH "cd starter && sudo ./install.sh --yes" 2>&1 | tee install.log
[ "${PIPESTATUS[0]}" -eq 0 ] || { echo "install.sh failed"; PHASE_FAIL=1; }

log "Phase: install and use"
run_phase install

log "Rebooting the VM"
$SSH "sudo systemctl reboot" || true
sleep 15
wait_ssh
log "Phase: after reboot"
run_phase reboot

log "Phase: the Home Hub in a real browser"
run_phase browser

log "Phase: provider outage"
run_phase outage

log "Phase: restore"
run_phase restore

log "Re-running install.sh (upgrade path)"
$SSH "cd starter && sudo ./install.sh --yes" 2>&1 | tee reinstall.log
[ "${PIPESTATUS[0]}" -eq 0 ] || { echo "re-install failed"; PHASE_FAIL=1; }
run_phase upgrade

log "Phase: Matter (real protocol, virtual light)"
run_phase matter

log "Collecting evidence"
$SSH "sudo /opt/hnb/bin/status; sudo journalctl -u hnb.service --no-pager -n 50; sudo docker logs --tail 80 hnb-hub-1; sudo docker logs --tail 60 hnb-matter-1" > vm-status.log 2>&1 || true
kill "$(cat qemu.pid)" 2>/dev/null || true
rm -f cookies.txt state.json id id.pub      # credentials of the throwaway VM never leave the runner

python3 - <<'EOF'
import json
rows = [json.loads(l) for l in open("results.jsonl")]
ok = sum(r["pass"] for r in rows)
lines = [f"## M1 Debian 13 VM acceptance: {ok}/{len(rows)} checks passed", "",
         open("vm-os.txt").read().strip().replace("\n", "  \n"), "", "| Phase | Check | Result |", "|---|---|---|"]
lines += [f"| {r['phase']} | {r['check']} | {'✅' if r['pass'] else '❌ ' + r['detail'][:120]} |" for r in rows]
open("summary.md", "w").write("\n".join(lines) + "\n")
print("\n".join(lines[:3]))
EOF
exit "$PHASE_FAIL"
