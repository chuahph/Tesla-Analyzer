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
# or, from anywhere:
#
#   curl -sL https://evperkm.xyz/car -o c.sh && sudo bash c.sh
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
  apt-get install -y -qq git >/dev/null || die "could not install git"

  # Go from upstream, not from apt. Ubuntu ships 1.22, vehicle-command needs
  # 1.23, and the Ubuntu build cannot fetch a newer toolchain for itself —
  # it fails with "toolchain not available", which reads like a network
  # problem rather than a packaging one.
  export PATH=/usr/local/go/bin:$PATH
  if ! go version 2>/dev/null | grep -qE 'go1\.(2[3-9]|[3-9][0-9])'; then
    GOVER=$(curl -fsSL 'https://go.dev/VERSION?m=text' | head -1)
    [ -n "$GOVER" ] || die "could not determine the current Go version"
    echo "  installing $GOVER"
    curl -fsSL "https://go.dev/dl/${GOVER}.linux-amd64.tar.gz" -o /tmp/go.tgz \
      || die "could not download $GOVER"
    rm -rf /usr/local/go && tar -C /usr/local -xzf /tmp/go.tgz && rm -f /tmp/go.tgz
  fi
  go version || die "Go is still not usable"

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
#
# A vehicle accepts only a limited number of telemetry configurations, so this
# list is written once and completely rather than grown a field at a time.
# What each group is for:
#
#   Boundaries   Gear, DriverSeatOccupied, DoorState. Occupancy says whether
#                anyone actually left, which is what separates an arrival from
#                a queue and what a door event only implies.
#   Energy       LifetimeEnergyUsedDrive is monotonic and traction-only, so a
#                lost record costs nothing and climate does not contaminate a
#                trip. LifetimeEnergyGainedRegen gives regen, which this app
#                has never had at all.
#   Charging     ChargerVoltage x ChargeAmps x ChargerPhases is wall power
#                measured by the car, against EnergyRemaining for pack energy
#                — wall-to-pack efficiency on every charge, instead of
#                photographing receipts.
#   Security     SentryMode is a state machine, not a switch: Off, Idle,
#                Armed, Aware, Panic, Quiet. Aware means the car noticed
#                something and Panic means the alarm went off — neither is
#                visible through vehicle_data, which reports a bare boolean.
#                At ten seconds an escalation is caught; at the old three
#                hundred, an entire incident could pass between readings.
#   Explanation  ModuleTempMin is the pack's own temperature, which is what
#                cold losses actually depend on rather than the outside air
#                the efficiency chart plots today. Tyre pressure is worth a
#                few percent and is currently invisible.
#
# Road gradient is missing and wanted: it is probably the largest unexplained
# term in per-trip Wh/km on this island. GradeEstimatePercent appears in the
# proto but Tesla's API rejects it as an unknown field, so either the name
# differs on this firmware or it is not configurable. Left out rather than
# left in to fail the whole request.
# --- The second set, sent only when asked for -------------------------------
#
# Run with TELEMETRY_V2=1 to add these:
#
#   curl -sL https://evperkm.xyz/car -o c.sh && sudo TELEMETRY_V2=1 bash c.sh
#
# LifetimeEnergyUsed is the reason this exists. Trip energy is currently the
# difference of two EnergyRemaining readings, and that field moves in steps of
# 0.02 kWh — which is +-8% on a half-kilowatt-hour trip and is the floor under
# every short-trip figure this project has produced. A monotonic lifetime
# counter has no such step, so the same trip measured both ways says how much
# of the disagreement is quantisation and how much is real.
#
# LifetimeEnergyUsedDrive, which the current set asks for, is marked
# "Semi-truck only" in Tesla's proto. It has never arrived and never will; it
# is replaced here rather than kept alongside.
#
# BMSState carries the pack's own notion of driving (BMSStateDrive). Trip
# boundaries are inferred today from Gear and speed, which has measured exact
# in simulation but is an inference; this is the car's own answer.
#
# Kept out of the default set deliberately. A field list is only changed
# between measurement runs, never in the middle of one — the point of the
# comparison is that the two sides differ in one thing at a time.
# --- The third set: what a break-in would look like ---------------------------
#
# Run with TELEMETRY_V3=1 (which includes everything V2 adds):
#
#   curl -sL https://evperkm.xyz/car -o c.sh && sudo TELEMETRY_V3=1 bash c.sh
#
# The four window fields are the reason. The app already alerts on a car being
# opened while parked and locked, and already writes a SecurityEvent row for
# it — and that code reads windows as well as doors. On the telemetry path it
# has been reading them as "unknown" since the day it was written, because
# they were never configured. A window lowered or broken is the classic way
# in, and it has been the one the stream could not see.
#
# PairedPhoneKeyAndKeyFobQty is the other one worth having. A key being ADDED
# to a car is how a stolen Tesla is prepared, and nothing else in 270 fields
# would show it.
#
# ChargePortDoorOpen and DriverSeatBelt are cheaper evidence of the same
# question: somebody opened the port, or somebody got in and buckled up. All
# of these change only when something happens, so they cost almost no stream.
EXTRA_FIELDS=""
DEFAULT_DRIVE_COUNTER='      "LifetimeEnergyUsedDrive":   {"interval_seconds": 30},'
# What was sent last time, unless this run says otherwise. Re-running this
# script is normal — after a certificate renewal, or because the car was
# asleep — and without this, a plain `bash c.sh` would quietly send the
# smallest set and take back whatever had been added. Fields would simply stop
# arriving, with nothing anywhere to say why.
LEVEL_FILE=/etc/tesla/telemetry-level
if [ -z "${TELEMETRY_V2:-}" ] && [ -z "${TELEMETRY_V3:-}" ] && [ -r "$LEVEL_FILE" ]; then
  case "$(cat "$LEVEL_FILE")" in
    3) TELEMETRY_V3=1; say "Re-sending the set this car already has (V3)" ;;
    2) TELEMETRY_V2=1; say "Re-sending the set this car already has (V2)" ;;
  esac
fi

# V3 includes V2: the sets are cumulative, so asking for the newer one never
# silently drops the older one's fields.
if [ "${TELEMETRY_V3:-0}" = "1" ]; then TELEMETRY_V2=1; fi
if [ "${TELEMETRY_V2:-0}" = "1" ]; then
  DEFAULT_DRIVE_COUNTER='      "LifetimeEnergyUsed":        {"interval_seconds": 30},'
  EXTRA_FIELDS='      "BMSState":                  {"interval_seconds": 10}'
  say "TELEMETRY_V2 set: LifetimeEnergyUsed replaces the Semi-only drive counter, BMSState added"
fi
if [ "${TELEMETRY_V3:-0}" = "1" ]; then
  EXTRA_FIELDS="$EXTRA_FIELDS,
      \"FdWindow\":                  {\"interval_seconds\": 10},
      \"FpWindow\":                  {\"interval_seconds\": 10},
      \"RdWindow\":                  {\"interval_seconds\": 10},
      \"RpWindow\":                  {\"interval_seconds\": 10},
      \"PairedPhoneKeyAndKeyFobQty\": {\"interval_seconds\": 300},
      \"ChargePortDoorOpen\":         {\"interval_seconds\": 60},
      \"DriverSeatBelt\":             {\"interval_seconds\": 30}"
  say "TELEMETRY_V3 set: four windows, paired-key count, charge port door, driver seat belt"
fi

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
      "Gear":                      {"interval_seconds": 5},
      "DriverSeatOccupied":        {"interval_seconds": 10},
      "DoorState":                 {"interval_seconds": 10},
      "VehicleSpeed":              {"interval_seconds": 10},
      "Odometer":                  {"interval_seconds": 30},
      "Location":                  {"interval_seconds": 30},
$DEFAULT_DRIVE_COUNTER
      "LifetimeEnergyGainedRegen": {"interval_seconds": 60},
      "EnergyRemaining":           {"interval_seconds": 60},
      "Soc":                       {"interval_seconds": 60},
      "RatedRange":                {"interval_seconds": 300},
      "DetailedChargeState":       {"interval_seconds": 30},
      "ChargePortLatch":           {"interval_seconds": 60},
      "ACChargingPower":           {"interval_seconds": 60},
      "ACChargingEnergyIn":        {"interval_seconds": 60},
      "DCChargingEnergyIn":        {"interval_seconds": 60},
      "ChargerVoltage":            {"interval_seconds": 60},
      "ChargeAmps":                {"interval_seconds": 60},
      "ChargerPhases":             {"interval_seconds": 60},
      "ChargeLimitSoc":            {"interval_seconds": 300},
      "HvacPower":                 {"interval_seconds": 60},
      "ModuleTempMin":             {"interval_seconds": 300},
      "InsideTemp":                {"interval_seconds": 300},
      "OutsideTemp":               {"interval_seconds": 300},
      "SentryMode":                {"interval_seconds": 10},
      "CenterDisplay":             {"interval_seconds": 30},
      "Locked":                    {"interval_seconds": 300},
      "TpmsPressureFl":            {"interval_seconds": 3600},
      "TpmsPressureFr":            {"interval_seconds": 3600},
      "TpmsPressureRl":            {"interval_seconds": 3600},
      "TpmsPressureRr":            {"interval_seconds": 3600}${EXTRA_FIELDS:+,
$EXTRA_FIELDS}
    }
  }
}
EOF

# Tesla validates the field list and reports only the FIRST name it does not
# recognise, so a list written against the proto can need several attempts —
# and every attempt is a person running this again. Drop what it rejects and
# retry instead. Not silent: what was dropped is listed at the end, because a
# field quietly missing is how an analysis ends up explaining nothing.
say "Sending it to the car"
DROPPED=""
for attempt in $(seq 1 12); do
  HTTP=$(curl -sS --cacert "$WORK/proxy-cert.pem" -o "$WORK/resp.json" -w '%{http_code}' \
    -X POST "https://localhost:$PROXY_PORT/api/1/vehicles/fleet_telemetry_config" \
    -H "Authorization: Bearer $(jq -r .access_token "$WORK/tok.json")" \
    -H 'Content-Type: application/json' \
    --data-binary @"$WORK/config.json") || true
  UNKNOWN=$(jq -r '.error // ""' "$WORK/resp.json" 2>/dev/null \
            | sed -n 's/^Unknown field \([A-Za-z0-9_]*\).*/\1/p')
  [ -n "$UNKNOWN" ] || break
  echo "  this car does not accept '$UNKNOWN' — dropping it and retrying"
  DROPPED="$DROPPED $UNKNOWN"
  jq --arg f "$UNKNOWN" 'del(.config.fields[$f])' "$WORK/config.json" \
    > "$WORK/config.next" && mv "$WORK/config.next" "$WORK/config.json"
done
echo "  HTTP $HTTP"
jq . "$WORK/resp.json" 2>/dev/null || cat "$WORK/resp.json"
[ -n "$DROPPED" ] && echo "  fields this car rejected:$DROPPED"
echo "  fields sent: $(jq '.config.fields | length' "$WORK/config.json")"

# Accepted is not the same as applied. Tesla stores the configuration and
# delivers it when the car next connects, so a config sent to a sleeping or
# out-of-coverage car reports success here and reaches the vehicle hours
# later. Its own synced flag is the only thing that says it arrived.
if [ "${HTTP:0:1}" = "2" ]; then
  say "Checking whether the car has it yet"
  curl -sS "$BASE/api/1/vehicles/$VIN/fleet_telemetry_config" \
    -H "Authorization: Bearer $(jq -r .access_token "$WORK/tok.json")" \
    -o "$WORK/state.json" 2>/dev/null || true
  SYNCED=$(jq -r '.response.synced // "unknown"' "$WORK/state.json" 2>/dev/null)
  echo "  synced: $SYNCED"
  [ "$SYNCED" = "true" ] || echo "  (not yet — the car applies it when it next wakes)"
fi

case "$HTTP" in
  2*) # Remember what the car now has, so the next run cannot take it back.
      # Written only on a request Tesla accepted: a failed send has changed
      # nothing on the car, and must not change what the next run believes.
      printf '%s\n' "$([ "${TELEMETRY_V3:-0}" = 1 ] && echo 3 \
                      || { [ "${TELEMETRY_V2:-0}" = 1 ] && echo 2 || echo 1; })" \
        > /etc/tesla/telemetry-level
      say "Accepted"
      cat <<EOF

The car has the configuration. It connects when it next wakes, so an idle
log until then is expected rather than a fault.

Confirm the car has taken it (synced turns true once it wakes):
  $APP_URL/api/telemetry/recent      # new fields appearing is the proof

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
