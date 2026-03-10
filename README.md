# tmux-open-usage

`tmux-open-usage` is a TPM plugin that adds Claude Code and Codex rate-limit usage to the right side of the tmux status bar.

Current support target: macOS.

It shows:

- remaining 5-hour quota with compact reset time
- remaining 7-day quota with compact days-to-reset
- both providers in one compact text segment on the right side of row 1

Example output:

```text
[82%1a/55%3d | 23%10p/90%5d]
```

Format:

```text
[session-left%session-reset/weekly-left%weekly-days-left | session-left%session-reset/weekly-left%weekly-days-left]
```

Example breakdown:

- by default, first block is Claude Code and second block is Codex
- `82%` means 82% of the current 5-hour quota is still left
- `1a` means the current 5-hour quota resets at 1am local time
- `55%` means 55% of the weekly quota is still left
- `3d` means the weekly quota resets within 3 days
- `23%` means 23% of the current 5-hour quota is still left
- `10p` means the current 5-hour quota resets at 10pm local time
- `90%` means 90% of the weekly quota is still left
- `5d` means the weekly quota resets within 5 days

## How it works

- TPM runs [`tmux-open-usage.tmux`](./tmux-open-usage.tmux), removes any old second-row plugin state left by earlier versions of this plugin, and injects the usage command into `status-right`.
- The status command uses a 5-minute cache in `~/Library/Caches/tmux-open-usage` by default.
- Claude Code data comes from the same OAuth usage endpoint your statusline script uses.
- If direct Claude auth lookup is unavailable, the plugin can fall back to a shared Claude usage cache at `/tmp/claude_usage_cache.json` when it is recent enough.
- Codex data comes from the ChatGPT CLI usage endpoint used in `openusage`.
- Reset times are rendered in the machine's local timezone.
- If Python is unavailable, the status segment shows `tmux-open-usage: install python3`.
- Each provider is rendered as `session-left%session-reset/weekly-left%weekly-days-left`, ordered as Claude Code first, Codex second.
- `3p` means `3pm`, `1a` means `1am`, and `3d` means the weekly reset is within the next 3 days.
- On macOS, the plugin can read and refresh auth from the same files/keychain entries used by Claude Code and Codex CLI.

If a provider cannot be read or refreshed, the plugin falls back to stale cache.
If a configured provider still cannot be rendered, its slot becomes `-/-`.
The plugin preserves the user's existing tmux status height, except for the exact old two-line plugin migration case.

## Config

Enable or disable the plugin completely, default `on`:

```tmux
set -g @tmux_open_usage_enabled 'on'
```

To disable it:

```tmux
set -g @tmux_open_usage_enabled 'off'
```

Supported falsey values are `0`, `off`, `no`, and `false`.

Refresh interval in minutes, default `5`:

```tmux
set -g @tmux_open_usage_refresh_interval_minutes '5'
```

Use any positive integer. Reload tmux after changing it.

Provider order and visibility, default `claude,codex`:

```tmux
set -g @tmux_open_usage_providers 'claude,codex'
```

Examples:

```tmux
set -g @tmux_open_usage_providers 'claude'
set -g @tmux_open_usage_providers 'codex'
set -g @tmux_open_usage_providers 'codex,claude'
```

Only `claude` and `codex` are recognized. Unknown names are ignored. If nothing valid remains, the plugin falls back to `claude,codex`.

## Install

Public TPM install after publishing:

```tmux
set -g @plugin 'genkio/tmux-open-usage'
```

Then reload tmux and install plugins:

```sh
tmux source-file ~/.tmux.conf
```

Inside tmux, press `prefix + I`.

Local checkout without TPM:

```tmux
if-shell '[ -x /absolute/path/to/tmux-open-usage/tmux-open-usage.tmux ]' \
  "run-shell '/absolute/path/to/tmux-open-usage/tmux-open-usage.tmux'"
```

Reload tmux:

```sh
tmux source-file ~/.tmux.conf
```

Local checkout with TPM also works once the repository has at least one commit:

```tmux
set -g @plugin '/absolute/path/to/tmux-open-usage'
```

## Dev

Run the tests:

```sh
python3 -m unittest discover -s tests
```

Run the renderer directly:

```sh
./scripts/open_usage_status.sh
```
