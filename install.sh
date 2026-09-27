#!/usr/bin/env bash
# Home Network BRAIN Starter — installer for Debian 13 (x86_64) with Docker.
#
#   sudo ./install.sh                  install or upgrade (asks before installing packages)
#   sudo ./install.sh --yes            no questions; installs Docker from Debian if missing
#   sudo ./install.sh --no-demo        start without the fictional demo home
#   sudo ./install.sh --port 8780      web port
#
# Safe to re-run: keeps config, data and the vault key; takes a verified backup
# before upgrading an existing install. Never prints a secret.
set -Eeuo pipefail
umask 022

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX=/opt/hnb
ETC=/etc/hnb
ROOT=/var/lib/hnb
ENV_FILE="$ETC/hnb.env"
DOCKER_PKGS=(docker.io docker-cli docker-compose docker-buildx)
ASSUME_YES=0; ALLOW_UNSUPPORTED=0; PORT=8780; DEMO=true

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
trap 'die "install stopped at line $LINENO. Nothing was removed; fix the problem and re-run ./install.sh."' ERR

while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y) ASSUME_YES=1 ;;
    --install-docker) ASSUME_YES=1 ;;
    --allow-unsupported) ALLOW_UNSUPPORTED=1 ;;
    --no-demo) DEMO=false ;;
    --port) PORT="${2:?--port needs a number}"; shift ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
  shift
done
[[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1024 ] && [ "$PORT" -le 65535 ] || die "--port must be 1024-65535"

# ── 1. preflight ──────────────────────────────────────────────────────────
[ "$(id -u)" -eq 0 ] || die "run as root: sudo ./install.sh"
[ -r /etc/os-release ] || die "cannot read /etc/os-release"
. /etc/os-release
if [ "${ID:-}" != "debian" ] || [ "${VERSION_ID:-}" != "13" ]; then
  [ "$ALLOW_UNSUPPORTED" -eq 1 ] || die "supported OS is Debian 13; this is ${PRETTY_NAME:-unknown}"
  warn "unsupported OS ${PRETTY_NAME:-unknown}; continuing because --allow-unsupported was given"
fi
ARCH="$(dpkg --print-architecture 2>/dev/null || uname -m)"
if [ "$ARCH" != "amd64" ]; then
  [ "$ALLOW_UNSUPPORTED" -eq 1 ] || die "supported architecture is x86_64 (amd64); this is $ARCH"
  warn "unsupported architecture $ARCH; continuing because --allow-unsupported was given"
fi
for f in hnb/__init__.py web/index.html deploy/Dockerfile deploy/compose.yaml deploy/hnb.env.example \
         deploy/hnb.service deploy/hnb-backup.service deploy/hnb-backup.timer requirements.lock bin/backup; do
  [ -e "$SRC/$f" ] || die "incomplete download: missing $f"
done
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/hnb/__init__.py")"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "could not read the release version"
FREE_KB="$(df -Pk /var/lib | awk 'NR==2 {print $4}')"
[ "${FREE_KB:-0}" -ge 2097152 ] || die "need at least 2 GB free under /var/lib"
MEM_KB="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
[ "${MEM_KB:-0}" -ge 900000 ] || warn "less than 1 GB of RAM; the hub may be slow"
say "Installing Home Network BRAIN Starter $VERSION on ${PRETTY_NAME:-this host} ($ARCH)"

# ── 2. Docker (only the packages listed, only with approval) ──────────────
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  say "Docker Engine and Compose are needed. Debian packages: ${DOCKER_PKGS[*]}"
  if [ "$ASSUME_YES" -ne 1 ]; then
    [ -t 0 ] || die "Docker is missing. Re-run with --yes to install: ${DOCKER_PKGS[*]}"
    read -r -p "Install them now from Debian's repositories? [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]] || die "Docker is required. Install it and re-run ./install.sh"
  fi
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q
  apt-get install -y -q --no-install-recommends "${DOCKER_PKGS[@]}"
fi
systemctl enable --now docker.service >/dev/null
docker info >/dev/null 2>&1 || die "the Docker daemon is not answering"

# ── 3. service account and directories ────────────────────────────────────
if ! getent passwd hnb >/dev/null; then
  useradd --system --home-dir "$ROOT" --no-create-home --shell /usr/sbin/nologin --user-group hnb
  say "Created service account 'hnb'"
fi
HNB_UID="$(id -u hnb)"; HNB_GID="$(id -g hnb)"
install -d -m 0750 -o hnb -g hnb "$ROOT" "$ROOT/data" "$ROOT/backups" "$ROOT/logs"
install -d -m 0700 -o hnb -g hnb "$ROOT/matter"      # Matter fabric credentials
install -d -m 0700 -o hnb -g hnb "$ROOT/secrets"

# ── 4. vault key — generated once, never overwritten, never printed ───────
KEY="$ROOT/secrets/vault.key"
if [ ! -e "$KEY" ]; then
  TMP="$(mktemp "$ROOT/secrets/.vault.key.XXXXXX")"
  head -c 32 /dev/urandom > "$TMP"
  chown hnb:hnb "$TMP"; chmod 0400 "$TMP"
  mv -n "$TMP" "$KEY"
  say "Generated a new vault key (kept on this machine only; see BACKUP_RESTORE.md)"
else
  [ "$(stat -c %s "$KEY")" -eq 32 ] || die "existing vault key has the wrong size; refusing to touch it"
  chown hnb:hnb "$KEY"; chmod 0400 "$KEY"
fi

# ── 4b. networking for Matter (IPv6 on, accept Thread routes) ─────────────
IFACE="$(ip -4 route show default 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); exit}}')"
if [ "$(cat /proc/sys/net/ipv6/conf/all/disable_ipv6 2>/dev/null || echo 1)" != "0" ]; then
  warn "IPv6 is disabled on this host; Matter devices will not be reachable until it is enabled"
fi
if [ -n "$IFACE" ] && [ -e "/proc/sys/net/ipv6/conf/$IFACE/accept_ra_rt_info_max_plen" ]; then
  # Thread devices shared from Alexa are reached through an Echo's Thread border
  # router, which announces its route in IPv6 router advertisements.
  printf 'net.ipv6.conf.%s.accept_ra = 1\nnet.ipv6.conf.%s.accept_ra_rt_info_max_plen = 64\n' "$IFACE" "$IFACE" > /etc/sysctl.d/60-hnb-matter.conf
  sysctl -q -p /etc/sysctl.d/60-hnb-matter.conf || warn "could not apply the Matter IPv6 settings"
fi

# ── 5. install this release to /opt/hnb ───────────────────────────────────
FILES=(hnb web plugins examples deploy bin requirements.lock requirements.in pyproject.toml install.sh .dockerignore
       README.md INSTALL.md SECURITY.md BACKUP_RESTORE.md TROUBLESHOOTING.md RELEASE_NOTES.md CHANGELOG.md)
if [ "$SRC" != "$PREFIX" ]; then
  rm -rf "$PREFIX.new"
  install -d -m 0755 "$PREFIX.new"
  present=(); for f in "${FILES[@]}"; do [ -e "$SRC/$f" ] && present+=("$f"); done
  tar -C "$SRC" --exclude='__pycache__' --exclude='*.pyc' -cf - "${present[@]}" | tar -C "$PREFIX.new" -xf -
  chown -R root:root "$PREFIX.new"; chmod -R go-w "$PREFIX.new"
  if [ -d "$PREFIX" ]; then rm -rf "$PREFIX.prev"; mv "$PREFIX" "$PREFIX.prev"; fi
  mv "$PREFIX.new" "$PREFIX"
fi
chmod 0755 "$PREFIX"/bin/* "$PREFIX/install.sh"

# ── 6. configuration (created once; version and account kept current) ────
install -d -m 0750 -o root -g hnb "$ETC"
if [ ! -e "$ENV_FILE" ]; then
  TZ_NAME="$(timedatectl show -p Timezone --value 2>/dev/null || echo UTC)"
  sed -e "s/@VERSION@/$VERSION/" -e "s/@UID@/$HNB_UID/" -e "s/@GID@/$HNB_GID/" \
      -e "s/@PORT@/$PORT/" -e "s/@DEMO@/$DEMO/" -e "s#@TZ@#${TZ_NAME:-UTC}#" -e "s/@IFACE@/${IFACE:-}/" \
      "$PREFIX/deploy/hnb.env.example" > "$ENV_FILE.tmp"
  install -m 0640 -o root -g hnb "$ENV_FILE.tmp" "$ENV_FILE"; rm -f "$ENV_FILE.tmp"
  say "Wrote $ENV_FILE"
else
  sed -i -e "s/^HNB_VERSION=.*/HNB_VERSION=$VERSION/" -e "s/^HNB_UID=.*/HNB_UID=$HNB_UID/" \
         -e "s/^HNB_GID=.*/HNB_GID=$HNB_GID/" "$ENV_FILE"
  grep -q '^HNB_MATTER_INTERFACE=' "$ENV_FILE" || printf '\nHNB_MATTER_INTERFACE=%s\n' "${IFACE:-}" >> "$ENV_FILE"
  chown root:hnb "$ENV_FILE"; chmod 0640 "$ENV_FILE"
fi
. "$PREFIX/bin/_lib.sh"

# ── 7. build the image (base pinned by digest, dependencies by hash) ──────
say "Building the hub image and fetching the Matter server (first time takes a few minutes)"
compose build --pull hub
compose pull matter

# ── 8. upgrading? verified backup first, then stop the old hub ────────────
if [ -e "$ROOT/data/hnb.db" ]; then
  say "Existing hub found; taking a verified backup before upgrading"
  "$PREFIX/bin/backup" || die "pre-upgrade backup failed; the running hub was not changed"
  systemctl stop hnb.service 2>/dev/null || true
fi

# ── 9. database: create or migrate, then integrity check ──────────────────
say "Preparing the database"
compose run --rm --no-deps -T hub migrate >/dev/null

# ── 10. start at boot, nightly backups ────────────────────────────────────
install -m 0644 "$PREFIX/deploy/hnb.service" /etc/systemd/system/hnb.service
install -m 0644 "$PREFIX/deploy/hnb-backup.service" /etc/systemd/system/hnb-backup.service
install -m 0644 "$PREFIX/deploy/hnb-backup.timer" /etc/systemd/system/hnb-backup.timer
systemctl daemon-reload
systemctl enable hnb.service hnb-backup.timer >/dev/null
systemctl restart hnb.service
systemctl start hnb-backup.timer

# ── 11. wait until healthy ────────────────────────────────────────────────
say "Starting the hub"
wait_healthy 180 || { compose logs --tail 60 hub >&2 || true; die "the hub did not become healthy; logs above"; }
trap - ERR

HOST="$(hostname)"
# The LAN address is the source address of the default route — never a Docker
# bridge. `ip route get` only consults the routing table; nothing is sent.
LAN_IP="$(ip -4 route get 192.0.2.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") {print $(i + 1); exit}}')"
URLS=""
[ -n "$LAN_IP" ] && URLS="      http://$LAN_IP:$PORT"
if systemctl is-active --quiet avahi-daemon 2>/dev/null; then
  URLS="$URLS${URLS:+$'\n'}      http://$HOST.local:$PORT"
fi
[ -n "$URLS" ] || URLS="      http://<this box's address>:$PORT   (find it with: ip -4 route get 192.0.2.1)"
cat <<EOF

  Home Network BRAIN Starter $VERSION is running.

  Open it from a phone or computer on your home network:
$URLS
  First time? You'll be asked for a one-time setup code. Show it with:
      sudo $PREFIX/bin/setup-code

  Health:   sudo $PREFIX/bin/status
  Backups:  nightly, plus  sudo $PREFIX/bin/backup
  Guide:    $PREFIX/INSTALL.md

EOF
