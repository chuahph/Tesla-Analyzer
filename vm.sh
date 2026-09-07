#!/usr/bin/env bash
# Short launcher for the telemetry VM setup, so the command can be TYPED into
# a browser SSH window on a phone. Google's SSH-in-browser will not reliably
# paste on iOS, which makes a 130-character raw.githubusercontent URL a real
# obstacle rather than a cosmetic one.
#
#   sudo bash <(curl -sL https://raw.githubusercontent.com/chuahph/Tesla-Analyzer/main/vm.sh)
#
# Process substitution rather than a pipe: `curl | bash` hands the script to
# bash on stdin, which leaves the setup script with nothing to read its
# prompts from.
set -euo pipefail
exec bash <(curl -fsSL \
  "https://raw.githubusercontent.com/chuahph/Tesla-Analyzer/main/scripts/telemetry/01-vm-setup.sh")
