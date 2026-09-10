#!/usr/bin/env bash
set -u

[[ -n "${ZELLIJ_SESSION_NAME:-}" && -n "${ZELLIJ_PANE_ID:-}" ]] || exit 0
[[ "${COPILOT_NOTIFY_ICONS:-1}" != "0" ]] || exit 0
if ! command -v python3 >/dev/null 2>&1; then
  printf 'copilot-notify-icons: Python 3.9+ is required; install it or set COPILOT_NOTIFY_ICONS=0.\n' >&2
  exit 1
fi
exec python3 "$(dirname "$0")/activity.py" hook "${1:-sessionStart}"
