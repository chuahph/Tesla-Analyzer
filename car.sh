#!/usr/bin/env bash
# Short launcher for the vehicle telemetry configuration, so the command
# can be TYPED into a browser SSH window on a phone. Google's
# SSH-in-browser will not reliably paste on iOS,
# which makes a ~130-character raw.githubusercontent URL a real obstacle
# rather than a cosmetic one.
#
#   sudo bash <(curl -sL evperkm.xyz/car)
#
# Re-run this whenever the field list or its intervals change: the car keeps
# streaming the LAST config it accepted until a new signed one replaces it,
# so editing the script alone changes nothing on the road.
#
# Fetched to a file and checked before running, rather than `bash <(curl ...)`.
# Process substitution hands bash whatever came back, and an empty body — a
# deploy still in flight, a redirect that did not resolve — is a valid empty
# script that runs silently and reports success. That failure looks exactly
# like the command having done nothing, which is precisely what it did.
set -euo pipefail
URL="https://raw.githubusercontent.com/chuahph/Tesla-Analyzer/main/scripts/telemetry/02-configure-vehicle.sh"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
if ! curl -fsSL "$URL" -o "$TMP"; then
  echo "ERROR: could not fetch $URL" >&2
  exit 1
fi
# A shell script that does not start with a shebang is not a shell script; it
# is an error page, or nothing at all.
if [ ! -s "$TMP" ] || ! head -1 "$TMP" | grep -q '^#!'; then
  echo "ERROR: $URL did not return a script. First line was:" >&2
  head -1 "$TMP" >&2
  exit 1
fi
exec bash "$TMP"
