#!/usr/bin/env bash

SCRIPT_PATH="${BASH_SOURCE[0]}"
SCRIPT_DIR="${SCRIPT_PATH%/*}"
if [[ "$SCRIPT_DIR" == "$SCRIPT_PATH" ]]; then
  SCRIPT_DIR="."
fi

CURRENT_DIR="$(CDPATH= cd -- "$SCRIPT_DIR" && pwd)"
REFRESH_OPTION='@tmux_open_usage_refresh_interval_minutes'
PROVIDERS_OPTION='@tmux_open_usage_providers'

refresh_minutes="$(tmux show-option -gqv "$REFRESH_OPTION" 2>/dev/null || true)"
if [[ "$refresh_minutes" =~ ^[1-9][0-9]*$ ]]; then
  export TMUX_OPEN_USAGE_REFRESH_INTERVAL_MINUTES="$refresh_minutes"
fi

providers="$(tmux show-option -gqv "$PROVIDERS_OPTION" 2>/dev/null || true)"
if [[ -n "$providers" ]]; then
  export TMUX_OPEN_USAGE_PROVIDERS="$providers"
fi

if command -v python3 >/dev/null 2>&1; then
  exec python3 "$CURRENT_DIR/open_usage_status.py" "$@"
fi

if command -v python >/dev/null 2>&1 && python -c 'import sys; raise SystemExit(0 if sys.version_info[0] >= 3 else 1)' >/dev/null 2>&1; then
  exec python "$CURRENT_DIR/open_usage_status.py" "$@"
fi

printf '%s' 'tmux-open-usage: install python3'
