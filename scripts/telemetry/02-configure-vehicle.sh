#!/usr/bin/env bash
#
# Tell the car to start streaming to this box.
#
#   sudo TELEMETRY_HOST=... APP_URL=... SYNC_KEY=... bash 02-configure-vehicle.sh
#
# or, with 01-vm-setup.sh already run, just:
#
#   sudo bash 02-configure-vehicle.sh
#
# (it reads the same values back out of /etc/tesla/bridge.env).
#
# The configuration has to be SIGNED by the virtual key paired to the vehicle,
# and that key never leaves this box — so the signing happens here, using
# Tesla's own vehicle-command proxy rather than anything hand-rolled. The
# access token comes the other way, fetched from the app for this one run and
# never written to disk.
set -euo pipefail

KEY_DIR="/etc/tesla/keys"
WORK="/run/tesla-config"      # tmpfs: the token must not survive a reboot
PROXY_PORT=4443
LOG="/var/log/tesla-configure.log"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo: sudo bash $0"
exec > >(tee -a "$LOG") 2>&1
echo "=== $(date -Is) configuring vehicle ==="

# ---------------------------------------------------------------- inputs
[ -f /etc/tesla/bridge.env ] && . /etc/tesla/bridge.env
TELEMETRY_HOST="${TELEMETRY_HOST:-telemetry.evperkm.xyz}"
[ -n "${APP_URL:-}" ] || die "APP_URL not set and /etc/tesla/bridge.env has none"
[ -n "${SYNC_KEY:-}" ] || die "SYNC_KEY not set and /etc/tesla/bridge.env has none"
[ -f "$KEY_DIR/private-key.pem" ] || die "no virtual key at $KEY_DIR — run 01-vm-setup.sh first"

mkdir -p "$WORK"; chmod 700 "$WORK"
# The token lives here for the length of this run and no longer. /run is
# tmpfs, so a reboot clears it even if the trap below never fires.
trap 'shred -u "$WORK"/* 2>/dev/null || rm -f "$WORK"/*; rmdir "$WORK" 2>/dev/null || true' EXIT

# ------------------------------------------------------------------ token
say "Asking the app for a token"
curl -fsS "$APP_URL/api/fleet-token?key=$SYNC_KEY" -o "$WORK/tok.json" \
  || die "could not fetch a token. A 401 means SYNC_KEY here does not match the app's."
VIN=$(jq -r '.vin // empty' "$WORK/tok.json")
BASE=$(jq -r '.base_url // empty' "$WORK/tok.json")
[ -n "$VIN" ] || die "the app returned no VIN — is a car linked?"
echo "  vehicle : $VIN"
echo "  api     : $BASE"

# ------------------------------------------------------------------ proxy
# Tesla's own proxy does the signing. Hand-rolling it is not an option worth
# considering: the configuration is signed with Schnorr over P-256, and a
# subtly wrong signature is rejected by the car with nothing to learn from.
say "Installing the signing proxy"
if ! command -v tesla-http-proxy >/dev/null; then
  apt-get install -y -qq golang-go git >/dev/null || die "could not install Go"

  # Linking Go on a 1 GB box is the one step here with any chance of running
  # out of memory, and an OOM during a build reads as an unexplained failure.
  if [ "$(free -m | awk '/Mem:/{print $2}')" -lt 2048 ] && [ ! -f /swapfile ]; then
    echo "  adding 2G of swap for the build"
    fallocate -l 2G /swapfile && chmod 600 /swapfile \
      && mkswap -q /swapfile && swapon /swapfile || true
  fi

  # Built from a clone rather than `go install ...@latest`: the module's
  # go.mod carries replace directives, and go install refuses those outright.
  # Inside the module they are honoured, so the same build works here.
  SRC=/usr/local/src/vehicle-command
  rm -rf "$SRC"
  git clone -q --depth 1 https://github.com/teslamotors/vehicle-command "$SRC" \
    || die "could not clone vehicle-command"
  (cd "$SRC" && go build -o /usr/local/bin/tesla-http-proxy ./cmd/tesla-http-proxy) \
    || die "could not build tesla-http-proxy (see $LOG)"
fi
command -v tesla-http-proxy >/dev/null || die "tesla-http-proxy is still not on PATH"

# The proxy serves HTTPS locally; this certificate is only ever presented to
# curl on this machine, so it is self-signed and pinned below with --cacert.
if [ ! -f "$WORK/proxy-cert.pem" ]; then
  openssl req -x509 -nodes -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
    -keyout "$WORK/proxy-key.pem" -out "$WORK/proxy-cert.pem" -days 1 \
    -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
    2>/dev/null
fi

say "Starting the proxy"
tesla-http-proxy -port "$PROXY_PORT" -host localhost \
  -tls-key "$WORK/proxy-key.pem" -cert "$WORK/proxy-cert.pem" \
  -key-file "$KEY_DIR/private-key.pem" -verbose &
PROXY_PID=$!
trap 'kill $PROXY_PID 2>/dev/null || true; shred -u "$WORK"/* 2>/dev/null || rm -f "$WORK"/*' EXIT
for _ in $(seq 1 20); do
  curl -sk "https://localhost:$PROXY_PORT/" >/dev/null 2>&1 && break
  sleep 1
done
kill -0 $PROXY_PID 2>/dev/null || die "the proxy exited on startup (see $LOG)"

# ----------------------------------------------------------------- config
# Intervals are MINIMUMS, and the car only sends a field when it changes — so
# a generous interval costs nothing on a parked car and still catches a
# departure. Gear is the one that must be tight: it is the signal the polling
# loop could never see in time, and the entire reason for this migration.
say "Building the configuration"
CA=$(sed ':a;N;$!ba;s/\n/\\n/g' "/etc/letsencrypt/live/$TELEMETRY_HOST/chain.pem")
cat > "$WORK/config.json" <<EOF
{
  "vins": ["$VIN"],
  "config": {
    "hostname": "$TELEMETRY_HOST",
    "port": 443,
    "ca": "$CA",
    "fields": {
      "Gear":                 {"interval_seconds": 10},
      "VehicleSpeed":         {"interval_seconds": 30},
      "Location":             {"interval_seconds": 60},
      "Odometer":             {"interval_seconds": 60},
      "Soc":                  {"interval_seconds": 60},
      "EnergyRemaining":      {"interval_seconds": 60},
      "RatedRange":           {"interval_seconds": 300},
      "DetailedChargeState":  {"interval_seconds": 60},
      "ACChargingPower":      {"interval_seconds": 60},
      "ACChargingEnergyIn":   {"interval_seconds": 60},
      "DCChargingEnergyIn":   {"interval_seconds": 60},
      "OutsideTemp":          {"interval_seconds": 300},
      "SentryMode":           {"interval_seconds": 300},
      "Locked":               {"interval_seconds": 300},
      "DoorState":            {"interval_seconds": 300}
    }
  }
}
EOF

say "Sending it to the car"
HTTP=$(curl -sS --cacert "$WORK/proxy-cert.pem" -o "$WORK/resp.json" -w '%{http_code}' \
  -X POST "https://localhost:$PROXY_PORT/api/1/vehicles/fleet_telemetry_config" \
  -H "Authorization: Bearer $(jq -r .access_token "$WORK/tok.json")" \
  -H 'Content-Type: application/json' \
  --data-binary @"$WORK/config.json") || true
echo "  HTTP $HTTP"
jq . "$WORK/resp.json" 2>/dev/null || cat "$WORK/resp.json"

case "$HTTP" in
  2*) say "Accepted"
      cat <<EOF

The car has the configuration. It connects when it next wakes, so an idle
log until then is expected rather than a fault.

Watch it arrive:
  docker logs -f fleet-telemetry     # the car's connection
  journalctl -u tesla-bridge -f      # records being forwarded

Then read what it actually sent:
  $APP_URL/api/telemetry/recent

Nothing in the app derives anything from those records yet — units come
first, from real values.
EOF
      ;;
  412) die "412: the vehicle rejected the configuration. Usually the virtual
  key is not paired, or the CA does not match the certificate the server
  presents. Check https://tesla.com/_ak/${APP_URL#https://}" ;;
  *)  die "the configuration was not accepted (see above and $LOG)" ;;
esac
