#!/usr/bin/env bash

set -u

hook_alerts=${COPILOT_NOTIFY_HOOK_ALERTS:-}
if [[ -z "$hook_alerts" ]]; then
  config=${COPILOT_HOME:-"$HOME/.copilot"}/copilot-agent-notify.json
  if [[ -f "$config" ]]; then
    if ! command -v python3 >/dev/null 2>&1; then
      printf 'copilot-notify: Python 3 is required to read %s.\n' "$config" >&2
      exit 1
    fi
    if ! hook_alerts=$(python3 -c '
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        config = json.load(stream)
    if not isinstance(config, dict):
        raise ValueError("expected a JSON object")
    value = config.get("hook_alerts", True)
    if not isinstance(value, bool):
        raise ValueError("hook_alerts must be true or false")
    print("1" if value else "0")
except (OSError, ValueError, TypeError) as error:
    raise SystemExit("copilot-notify: invalid hook config: " + str(error))
' "$config"); then
      exit 1
    fi
  else
    hook_alerts=1
  fi
fi

case "$hook_alerts" in
  0) exit 0 ;;
  1) ;;
  *)
    printf 'copilot-notify: COPILOT_NOTIFY_HOOK_ALERTS must be 0 or 1.\n' >&2
    exit 1
    ;;
esac

payload=$(cat)

hook_event=${1:-agentStop}
if [[ "$hook_event" == "subagentStop" ]]; then
  case "${COPILOT_NOTIFY_SUBAGENTS:-0}" in
    0) exit 0 ;;
    1) ;;
    *)
      printf 'copilot-notify: COPILOT_NOTIFY_SUBAGENTS must be 0 or 1.\n' >&2
      exit 1
      ;;
  esac
fi

app_name="GitHub Copilot CLI"
session_id=""
event_type=""
event_title=""
event_message=""
cwd=""
agent_id=""
agent_name=""
agent_display_name=""
agent_type=""
transcript_path=""

if command -v python3 >/dev/null 2>&1; then
  mapfile -t payload_fields < <(
    python3 -c '
import json
import sys

payload = json.load(sys.stdin)
for key in (
    "sessionId",
    "notification_type",
    "title",
    "message",
    "cwd",
    "agentId",
    "agentName",
    "agentDisplayName",
    "agentType",
    "transcriptPath",
):
    print(str(payload.get(key, "")).replace("\r", " ").replace("\n", " "))
' <<<"$payload" 2>/dev/null
  )
  session_id=${payload_fields[0]:-}
  event_type=${payload_fields[1]:-}
  event_title=${payload_fields[2]:-}
  event_message=${payload_fields[3]:-}
  cwd=${payload_fields[4]:-}
  agent_id=${payload_fields[5]:-}
  agent_name=${payload_fields[6]:-}
  agent_display_name=${payload_fields[7]:-}
  agent_type=${payload_fields[8]:-}
  transcript_path=${payload_fields[9]:-}
else
  session_id=$(printf '%s' "$payload" |
    sed -n 's/.*"sessionId"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  event_type=$(printf '%s' "$payload" |
    sed -n 's/.*"notification_type"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  cwd=$(printf '%s' "$payload" |
    sed -n 's/.*"cwd"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  agent_id=$(printf '%s' "$payload" |
    sed -n 's/.*"agentId"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  agent_name=$(printf '%s' "$payload" |
    sed -n 's/.*"agentName"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  transcript_path=$(printf '%s' "$payload" |
    sed -n 's/.*"transcriptPath"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
fi

# Child agentStop events share the root transcript; subagentStop owns their alert.
if [[ "$hook_event" == "agentStop" && -n "$transcript_path" ]]; then
  if ! IFS= read -r transcript_header < "$transcript_path"; then
    printf 'copilot-notify: cannot read transcript session header.\n' >&2
    exit 1
  fi
  if command -v python3 >/dev/null 2>&1; then
    root_session_id=$(python3 -c '
import json
import sys

event = json.load(sys.stdin)
if event.get("type") != "session.start" or not event.get("data", {}).get("sessionId"):
    sys.exit("copilot-notify: invalid transcript session header.")
print(event["data"]["sessionId"])
' <<<"$transcript_header") || exit 1
  else
    root_event_type=$(printf '%s' "$transcript_header" |
      sed -n 's/.*"type"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
    root_session_id=$(printf '%s' "$transcript_header" |
      sed -n 's/.*"sessionId"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
    if [[ "$root_event_type" != "session.start" || -z "$root_session_id" ]]; then
      printf 'copilot-notify: invalid transcript session header.\n' >&2
      exit 1
    fi
  fi
  if [[ -z "$session_id" ]]; then
    printf 'copilot-notify: agentStop payload is missing sessionId.\n' >&2
    exit 1
  fi
  [[ "$session_id" == "$root_session_id" ]] || exit 0
fi

copilot_home=${COPILOT_HOME:-"$HOME/.copilot"}
workspace_file="$copilot_home/session-state/$session_id/workspace.yaml"
session_title=""

if [[ -n "$session_id" && -r "$workspace_file" ]]; then
  session_title=$(sed -n 's/^name: //p' "$workspace_file" | head -n 1)
  if [[ "$session_title" == \'*\' ]]; then
    session_title=${session_title:1:${#session_title}-2}
    session_title=${session_title//\'\'/\'}
  elif [[ "$session_title" == \"*\" ]]; then
    if command -v python3 >/dev/null 2>&1; then
      session_title=$(python3 -c 'import json, sys; print(json.loads(sys.argv[1]))' \
        "$session_title" 2>/dev/null || true)
    else
      session_title=${session_title:1:${#session_title}-2}
    fi
  fi
fi

session_title=${session_title:-"Untitled session"}
session_title=${session_title//$'\r'/ }
session_title=${session_title//$'\n'/ }
if ((${#session_title} > 64)); then
  session_title="${session_title:0:61}..."
fi

if grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then
  platform="WSL:${WSL_DISTRO_NAME:-Linux}"
else
  platform=$(uname -s)
fi

short_id=${session_id:0:8}
title="[$platform] $session_title"
[[ -n "$short_id" ]] && title="$title [#$short_id]"
notification_tag=${short_id:-copilot}

if [[ "$hook_event" == "subagentStop" ]]; then
  agent_label=${agent_display_name:-${agent_name:-${agent_type:-Subagent}}}
  short_agent_id=${agent_id:0:8}
  action="Subagent complete: $agent_label"
  [[ -n "$short_agent_id" ]] && action="$action [#$short_agent_id]"
  notification_tag=${short_agent_id:-$notification_tag}
else
  case "$event_type" in
    permission_prompt)
    action=${event_title:-"Permission needed"}
    ;;
    elicitation_dialog)
    action=${event_title:-"Input needed"}
    ;;
    *)
    action="Agent finished responding"
    ;;
  esac
fi

message="$action."
[[ -n "$cwd" ]] && message="$message"$'\n'"Go to: $cwd"

notify_wsl() {
  local notifier_dir notifier
  local launcher=/init
  notifier_dir=$(cd -- "$(dirname "$0")/../windows-helper/bin" && pwd) || return $?
  notifier="$notifier_dir/copilot-notify.exe"
  # Explicit interop still works when WSL's automatic .exe handler is unavailable.
  if [[ -x "$launcher" ]]; then
    # WSL's binfmt "P" convention requires the executable path and preserved argv[0].
    "$launcher" "$notifier" "$notifier" "$title" "$message" "$notification_tag" >/dev/null || return $?
  else
    "$notifier" "$title" "$message" "$notification_tag" >/dev/null || return $?
  fi
  bash "$(dirname "$0")/bell.sh"
}

if [[ "$(uname -s)" == "Darwin" ]] && command -v osascript >/dev/null 2>&1; then
  osascript - "$title" "$message" >/dev/null 2>&1 <<'APPLESCRIPT' || true
on run argv
  display notification (item 2 of argv) with title (item 1 of argv)
end run
APPLESCRIPT
elif grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then
  notify_wsl || exit $?
elif command -v notify-send >/dev/null 2>&1; then
  notify-send --app-name="$app_name" "$title" "$message" \
    >/dev/null 2>&1 || printf '\a' >&2
else
  printf '\a' >&2
fi

exit 0
