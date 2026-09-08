#!/usr/bin/env bash
# Short launcher for the vehicle telemetry configuration, so the command can
# be TYPED into a browser SSH window on a phone — the same reason vm.sh
# exists. Google's SSH-in-browser will not reliably paste on iOS.
#
#   sudo bash <(curl -sL https://raw.githubusercontent.com/chuahph/Tesla-Analyzer/main/car.sh)
#
# Re-run this whenever the field list or its intervals change: the car keeps
# streaming the LAST config it accepted until a new signed one replaces it,
# so editing the script alone changes nothing on the road.
#
# Process substitution rather than a pipe: `curl | bash` hands the script to
# bash on stdin, which leaves the config script with nothing to read its
# prompts from.
set -euo pipefail
exec bash <(curl -fsSL \
  "https://raw.githubusercontent.com/chuahph/Tesla-Analyzer/main/scripts/telemetry/02-configure-vehicle.sh")
