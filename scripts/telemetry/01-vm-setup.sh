#!/usr/bin/env bash
#
# Stand up a Tesla Fleet Telemetry receiver on a fresh Ubuntu 24.04 box.
#
#   curl -fsSL https://raw.githubusercontent.com/chuahph/Tesla-Analyzer/main/scripts/telemetry/01-vm-setup.sh -o s.sh
#   sudo bash s.sh
#
# Downloaded rather than piped into bash so the script keeps a usable stdin and
# can prompt. It is safe to re-run: every step checks for its own result first,
# so a failure halfway through is fixed by running it again, not by unpicking
# what it already did.
#
# What it does NOT do: register the partner domain, pair the virtual key, or
# send the telemetry config. Those need a human with a phone and a browser, and
# they come after this (see 02-configure-vehicle.sh).
set -euo pipefail

FT_VERSION="v0.9.0"
FT_IMAGE="tesla/fleet-telemetry:${FT_VERSION}"
KEY_DIR="/etc/tesla/keys"
CONF_DIR="/etc/fleet-telemetry"
BRIDGE_DIR="/opt/tesla-bridge"
# Where this script fetches its siblings from. Overridable because a branch
# that has not been merged to main yet still has to be runnable — otherwise
# the failure is a 404 inside a pipe, which reports as nothing happening at
# all rather than as a missing file.
RAW_BASE="${RAW_BASE:-https://raw.githubusercontent.com/chuahph/Tesla-Analyzer/main/scripts/telemetry}"

LOG="/var/log/tesla-setup.log"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo: sudo bash $0"

# Everything from here is teed to a file. Run as a GCP startup script there is
# no terminal to watch, and hunting the reason for a failure through
# journalctl on a phone keyboard is its own small ordeal — one predictable
# path to "what went wrong" is worth the two lines.
exec > >(tee -a "$LOG") 2>&1
echo "=== $(date -Is) starting ==="

# apt on a freshly booted GCP image is frequently already busy: unattended
# upgrades and apt-daily fire at boot and hold the dpkg lock. A startup script
# that runs `apt-get install` immediately loses that race, exits under `set
# -e`, and leaves nothing behind but an empty /etc/tesla — which is
# indistinguishable from never having run at all.
wait_for_apt() {
  local waited=0
  while fuser /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock \
        /var/cache/apt/archives/lock >/dev/null 2>&1; do
    [ "$waited" -eq 0 ] && echo "  waiting for another apt/dpkg to finish..."
    sleep 5
    waited=$((waited + 5))
    [ "$waited" -ge 300 ] && die "apt has been locked for 5 minutes; try again later"
  done
}

# ---------------------------------------------------------------- inputs
# Every answer can arrive as an environment variable instead of a prompt, so
# the whole thing can run as a GCP startup script. That is not a convenience:
# Google's SSH-in-browser will neither paste nor upload reliably on iOS, which
# leaves pasting a block into the instance's metadata form as the only way
# some people can get this script onto the box at all.
say "Configuration"
if [ -t 0 ]; then
  [ -n "${TELEMETRY_HOST:-}" ] || {
    read -rp "Telemetry hostname [telemetry.evperkm.xyz]: " TELEMETRY_HOST; }
  [ -n "${APP_URL:-}" ] || { read -rp "App URL [https://evperkm.xyz]: " APP_URL; }
  [ -n "${SYNC_KEY:-}" ] || {
    read -rsp "SYNC_KEY (same value as the app's env var): " SYNC_KEY; echo; }
  [ -n "${LE_EMAIL:-}" ] || {
    read -rp "Email for Let's Encrypt expiry notices (blank to skip): " LE_EMAIL; }
fi
TELEMETRY_HOST=${TELEMETRY_HOST:-telemetry.evperkm.xyz}
APP_URL=${APP_URL:-https://evperkm.xyz}
LE_EMAIL=${LE_EMAIL:-}

[ -n "${SYNC_KEY:-}" ] || die "SYNC_KEY is required — the app rejects unauthenticated posts.
  Interactively you are asked for it; unattended, set it in the environment:
    SYNC_KEY=... bash $0"

# ------------------------------------------------------------------ DNS
# Checked before anything is installed. certbot's HTTP-01 challenge needs the
# name to point here already, and a wrong record is far cheaper to find now
# than after a failed certificate order has burned a rate limit.
say "Checking DNS"
MY_IP=$(curl -fsS -H 'Metadata-Flavor: Google' \
  http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip \
  2>/dev/null || curl -fsS https://api.ipify.org)
RESOLVED=$(getent hosts "$TELEMETRY_HOST" | awk '{print $1}' | head -1 || true)
echo "  this box:  $MY_IP"
echo "  $TELEMETRY_HOST: ${RESOLVED:-(does not resolve)}"
if [ "$RESOLVED" != "$MY_IP" ]; then
  die "$TELEMETRY_HOST does not point at this machine.
  Add an A record for '$TELEMETRY_HOST' -> $MY_IP in Cloudflare, set to
  'DNS only' (grey cloud, NOT proxied — the proxy terminates TLS and the
  car's mutual-TLS handshake cannot survive that), then re-run this script."
fi

# --------------------------------------------------------------- packages
say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
wait_for_apt
apt-get update -qq || die "apt-get update failed — see $LOG"
wait_for_apt
apt-get install -y -qq docker.io certbot openssl curl jq \
  python3-zmq python3-requests || die "package install failed — see $LOG"
systemctl enable --now docker

# --------------------------------------------------------------- key pair
# The virtual key. Its public half goes in the repo and is served from the
# app's domain; its private half signs the telemetry configuration the car
# accepts, and must never leave this box. The repo is public — do not copy
# this file into it.
say "Partner / virtual key"
mkdir -p "$KEY_DIR"
chmod 700 "$KEY_DIR"
if [ -f "$KEY_DIR/private-key.pem" ]; then
  echo "  existing key kept (delete $KEY_DIR/private-key.pem to force a new one)"
else
  openssl ecparam -name prime256v1 -genkey -noout -out "$KEY_DIR/private-key.pem"
  chmod 600 "$KEY_DIR/private-key.pem"
  echo "  generated a new secp256r1 key pair"
fi
openssl ec -in "$KEY_DIR/private-key.pem" -pubout -out "$KEY_DIR/public-key.pem" 2>/dev/null
chmod 644 "$KEY_DIR/public-key.pem"

# ---------------------------------------------------------- certificate
# The car will not talk to a server whose certificate it cannot chain to a
# widely trusted CA, so this is Let's Encrypt rather than anything self-signed.
# fleet-telemetry holds 443, so the challenge runs on 80 — make sure the GCP
# firewall allows HTTP as well as HTTPS.
say "TLS certificate for $TELEMETRY_HOST"
if [ -f "/etc/letsencrypt/live/$TELEMETRY_HOST/fullchain.pem" ]; then
  echo "  existing certificate kept"
else
  EMAIL_ARGS=(--register-unsafely-without-email)
  [ -n "$LE_EMAIL" ] && EMAIL_ARGS=(-m "$LE_EMAIL")
  certbot certonly --standalone --non-interactive --agree-tos \
    "${EMAIL_ARGS[@]}" -d "$TELEMETRY_HOST" \
    || die "certbot failed. The usual cause is port 80 being closed:
  Compute Engine -> VM instances -> telemetry -> Edit -> Firewalls ->
  tick 'Allow HTTP traffic' -> Save, then re-run."
fi

# -------------------------------------------------------------- server
say "fleet-telemetry configuration"
mkdir -p "$CONF_DIR"
cat > "$CONF_DIR/config.json" <<EOF
{
  "host": "0.0.0.0",
  "port": 443,
  "log_level": "info",
  "namespace": "tesla",
  "transmit_decoded_records": true,
  "tls": {
    "server_cert": "/etc/letsencrypt/live/$TELEMETRY_HOST/fullchain.pem",
    "server_key": "/etc/letsencrypt/live/$TELEMETRY_HOST/privkey.pem"
  },
  "zmq": {
    "addr": "tcp://0.0.0.0:5284"
  },
  "records": {
    "V": ["zmq"],
    "alerts": ["logger"],
    "errors": ["logger"],
    "connectivity": ["logger"]
  }
}
EOF
# transmit_decoded_records is the reason the bridge is thirty lines instead of
# a protobuf toolchain: it makes the server publish JSON.

say "Starting fleet-telemetry ($FT_VERSION)"
docker pull -q "$FT_IMAGE" >/dev/null
docker rm -f fleet-telemetry >/dev/null 2>&1 || true

# The image ships its binary as CMD rather than ENTRYPOINT, so arguments
# passed to `docker run` REPLACE the command instead of being appended to it —
# docker then tries to exec a program named "-config=...". Read the binary out
# of the image and put it back in front, but only when there is no entrypoint
# to append to, so this keeps working if a later release adds one.
FT_ARGS=()
if [ "$(docker inspect -f '{{len .Config.Entrypoint}}' "$FT_IMAGE")" = "0" ]; then
  FT_ARGS+=("$(docker inspect -f '{{index .Config.Cmd 0}}' "$FT_IMAGE")")
fi
FT_ARGS+=("-config=$CONF_DIR/config.json")

# --network host so the container can hold 443 for the car and publish ZMQ on
# 5284 for the bridge without two layers of port mapping to reason about.
docker run -d --name fleet-telemetry --restart unless-stopped --network host \
  -v /etc/letsencrypt:/etc/letsencrypt:ro \
  -v "$CONF_DIR":"$CONF_DIR":ro \
  "$FT_IMAGE" "${FT_ARGS[@]}" >/dev/null

# A container that exits immediately is the same evidence as one that never
# started, and the reason is only in its logs. Say it here instead.
sleep 3
if [ "$(docker inspect -f '{{.State.Status}}' fleet-telemetry)" != "running" ]; then
  echo "--- fleet-telemetry logs ---"
  docker logs --tail 30 fleet-telemetry 2>&1 || true
  die "fleet-telemetry did not stay running (see above and $LOG)"
fi

# Certificates renew every 60 days; the server reads them once at start, so it
# has to be told. Without this the car silently stops connecting three months
# from now, which is exactly the kind of failure this project keeps meeting.
mkdir -p /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/restart-fleet-telemetry.sh <<'EOF'
#!/bin/sh
docker restart fleet-telemetry >/dev/null 2>&1 || true
EOF
chmod +x /etc/letsencrypt/renewal-hooks/deploy/restart-fleet-telemetry.sh

# -------------------------------------------------------------- bridge
say "Installing the ZMQ -> app bridge"
mkdir -p "$BRIDGE_DIR"
curl -fsSL "$RAW_BASE/bridge.py" -o "$BRIDGE_DIR/bridge.py"
chmod 755 "$BRIDGE_DIR/bridge.py"

# SYNC_KEY lives here rather than in the unit file so it is not world-readable
# in `systemctl cat`.
cat > /etc/tesla/bridge.env <<EOF
APP_URL=$APP_URL
SYNC_KEY=$SYNC_KEY
ZMQ_ADDR=tcp://127.0.0.1:5284
EOF
chmod 600 /etc/tesla/bridge.env

cat > /etc/systemd/system/tesla-bridge.service <<EOF
[Unit]
Description=Forward Tesla fleet telemetry records to the analyzer app
After=docker.service
Wants=docker.service

[Service]
EnvironmentFile=/etc/tesla/bridge.env
ExecStart=/usr/bin/python3 $BRIDGE_DIR/bridge.py
Restart=always
RestartSec=5
User=nobody

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now tesla-bridge >/dev/null
systemctl restart tesla-bridge

# -------------------------------------------------------------- report
say "Done"
# Also written to a file: in startup-script mode nobody sees this stdout,
# and the public key below is the one thing that has to leave this box.
cat > /etc/tesla/NEXT-STEPS.txt <<EOF

  fleet-telemetry : $(docker inspect -f '{{.State.Status}}' fleet-telemetry)
  bridge          : $(systemctl is-active tesla-bridge)
  listening on    : https://$TELEMETRY_HOST (443)
  forwarding to   : $APP_URL/api/telemetry

Next, and none of it can be done from this box:

  1. Copy the public key below into the repo, replacing
     app/static/well-known/com.tesla.3p.public-key.pem, and push. Render
     redeploys and serves it at $APP_URL/.well-known/appspecific/com.tesla.3p.public-key.pem
  2. Add $APP_URL as an Allowed Origin and
     $APP_URL/api/link/oauth/callback as an Allowed Redirect URI at
     developer.tesla.com
  3. Register the partner domain, then pair the virtual key to the car
  4. Run 02-configure-vehicle.sh to send the signed telemetry config

Until step 4 the car does not know this server exists, so an idle log here is
expected rather than a fault.

------------------------- PUBLIC KEY (safe to share) -------------------------
$(cat "$KEY_DIR/public-key.pem")
------------------------------------------------------------------------------

Useful afterwards:
  docker logs -f fleet-telemetry      # the car's connection attempts
  journalctl -u tesla-bridge -f       # what is being forwarded

EOF
cat /etc/tesla/NEXT-STEPS.txt
