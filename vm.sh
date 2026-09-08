#!/usr/bin/env bash
# Short launcher for the telemetry VM setup, so the command can be TYPED
# into a browser SSH window on a phone. Google's SSH-in-browser will not
# reliably paste on iOS,
# which makes a ~130-character raw.githubusercontent URL a real obstacle
# rather than a cosmetic one.
#
#   curl -sL evperkm.xyz/vm -o v.sh && sudo bash v.sh
#
# Downloaded first rather than `sudo bash <(curl ...)`. sudo on Ubuntu 24.04
# closes file descriptors above stderr, so the process substitution is already
# gone by the time bash tries to open it — `/dev/fd/63: No such file or
# directory`. Piping into `sudo bash` instead would consume stdin, which the
# setup script needs for its prompts. A file costs one more word and keeps
# both.
#
# Fetched to a file and checked before running, rather than `bash <(curl ...)`.
# Process substitution hands bash whatever came back, and an empty body — a
# deploy still in flight, a redirect that did not resolve — is a valid empty
# script that runs silently and reports success. That failure looks exactly
# like the command having done nothing, which is precisely what it did.
set -euo pipefail
URL="https://raw.githubusercontent.com/chuahph/Tesla-Analyzer/main/scripts/telemetry/01-vm-setup.sh"
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
