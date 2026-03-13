#!/usr/bin/env bash

SCRIPT_PATH="${BASH_SOURCE[0]}"
SCRIPT_DIR="${SCRIPT_PATH%/*}"
if [[ "$SCRIPT_DIR" == "$SCRIPT_PATH" ]]; then
  SCRIPT_DIR="."
fi

CURRENT_DIR="$(CDPATH= cd -- "$SCRIPT_DIR" && pwd)"
ENABLED_OPTION='@tmux_open_usage_enabled'
PROVIDERS_OPTION='@tmux_open_usage_providers'
RAW_STATUS_COMMAND="#($CURRENT_DIR/scripts/open_usage_status.sh)"
STATUS_COMMAND="#[fg=#5c5c5c]$RAW_STATUS_COMMAND#[default]"
OLD_SECOND_ROW_FORMAT="#[align=left,none,fg=#5c5c5c]$RAW_STATUS_COMMAND#[default]"

trim_whitespace() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

lowercase() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

has_configured_provider() {
  local raw="$1"
  local item trimmed
  local IFS=','
  local -a items=()

  read -r -a items <<< "$raw"
  for item in "${items[@]}"; do
    trimmed="$(trim_whitespace "$(lowercase "$item")")"
    case "$trimmed" in
      claude|codex)
        return 0
        ;;
    esac
  done

  return 1
}

status_right_value="$(tmux show-option -gqv status-right)"
clean_status_right="$(trim_whitespace "${status_right_value//$STATUS_COMMAND/}")"
clean_status_right="$(trim_whitespace "${clean_status_right//$RAW_STATUS_COMMAND/}")"

if [[ "$clean_status_right" != "$status_right_value" ]]; then
  tmux set-option -gq status-right "$clean_status_right"
fi

second_row_format="$(tmux show-option -gqv 'status-format[1]')"
status_value="$(tmux show-option -gqv status)"
if [[ "$second_row_format" == "$OLD_SECOND_ROW_FORMAT" ]]; then
  tmux set-option -gq 'status-format[1]' ''
  if [[ "$status_value" == "2" ]]; then
    tmux set-option -gq status on
  fi
fi

enabled_value="$(tmux show-option -gqv "$ENABLED_OPTION")"
case "$(lowercase "$enabled_value")" in
  ""|1|on|yes|true)
    ;;
  0|off|no|false)
    exit 0
    ;;
esac

providers_value="$(tmux show-option -gqv "$PROVIDERS_OPTION")"
if ! has_configured_provider "$providers_value"; then
  exit 0
fi

status_right_value="$(tmux show-option -gqv status-right)"
if [[ "$status_right_value" != *"$RAW_STATUS_COMMAND"* ]]; then
  if [[ -n "$status_right_value" ]]; then
    tmux set-option -gq status-right "$status_right_value $STATUS_COMMAND"
  else
    tmux set-option -gq status-right "$STATUS_COMMAND"
  fi
fi
