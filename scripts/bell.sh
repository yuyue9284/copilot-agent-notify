#!/usr/bin/env bash
set -u

# Hook stdout/stderr are pipes. Find a terminal only in our own process ancestry.
pid=$$
while [[ "$pid" -gt 1 ]]; do
  for fd in 1 2 0; do
    target=$(readlink "/proc/$pid/fd/$fd" 2>/dev/null) || continue
    case "$target" in
      /dev/pts/[0-9]*|/dev/tty[0-9]*)
        if [[ -c "$target" && -w "$target" ]]; then
          printf '\a' > "$target"
          exit $?
        fi
        ;;
    esac
  done
  parent=""
  while read -r key value rest; do
    if [[ "$key" == "PPid:" ]]; then
      parent=$value
      break
    fi
  done < "/proc/$pid/status" || break
  [[ "$parent" =~ ^[0-9]+$ && "$parent" != "$pid" ]] || break
  pid=$parent
done
printf 'copilot-notify: no ancestor terminal available; bell skipped.\n' >&2
