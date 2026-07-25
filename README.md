# tmux-open-usage

`tmux-open-usage` is a TPM plugin that can add Claude Code and Codex rate-limit usage to the right side of the tmux status bar.

Current support target: macOS.

By default, the plugin auto-detects supported providers from real auth state. If no supported provider is authenticated, it renders nothing.

When at least one provider is active, it shows, for each provider:

- remaining 5-hour session quota with compact reset time
- the selected providers in one compact text segment on the right side of row 1

By default only the current 5-hour session is shown. Set `@tmux_open_usage_view` to `all` to also show the 7-day weekly quota, or `@tmux_open_usage_claude_view` / `@tmux_open_usage_codex_view` to pick a view per provider.

Example output:

```text
 82·1a 23·10p
```

Format:

```text
 provider-segment  provider-segment
```

With one provider configured, it renders as:

```text
 provider-segment
```

Each provider segment is colored to distinguish it at a glance: Claude Code is orange, Codex is green.

Example breakdown:

- in this example, the first block is Claude Code and second block is Codex
- `82` means 82% of the current 5-hour quota is still left
- `1a` means the current 5-hour quota resets at 1am local time
- `23` means 23% of the current 5-hour quota is still left
- `10p` means the current 5-hour quota resets at 10pm local time

With `@tmux_open_usage_view 'all'`, each segment gains a `/weekly-left·weekly-days-left` suffix, for example `82·1a/55·3d`, where `55` means 55% of the weekly quota is still left and `3d` means the weekly quota resets within 3 days.

## How it works

- TPM runs [`tmux-open-usage.tmux`](./tmux-open-usage.tmux), removes any old second-row plugin state left by earlier versions of this plugin, and injects the usage command into `status-right` when at least one provider is configured or auto-detected.
- The status command uses a 15-minute cache in `~/Library/Caches/tmux-open-usage` by default.
- Claude Code data comes from the same OAuth usage endpoint your statusline script uses.
- If direct Claude auth lookup is unavailable, the plugin can fall back to a shared Claude usage cache at `/tmp/claude_usage_cache.json` when it is recent enough.
- Codex data comes from the ChatGPT CLI usage endpoint used in `openusage`.
- When Codex returns only a weekly window, the default session view falls back to that available quota and renders its reset in days, for example `83·6d`.
- Auto-detection only uses real auth sources such as OAuth tokens, auth files, or keychain entries. Generic config files do not enable a provider by default.
- Auto-detection runs when the plugin loads or tmux config is reloaded. If no provider is available at that moment, the segment is not attached to `status-right`.
- Once attached, provider selection is evaluated again each time the status command runs. If you log into Claude Code or Codex after tmux is already running and the segment was not attached yet, reload tmux config to attach it.
- Reset times are rendered in the machine's local timezone.
- If Python is unavailable, the status segment shows `tmux-open-usage: install python3`.
- By default each provider is rendered as `session-left·session-reset`, in the order you configure. With `@tmux_open_usage_view 'all'`, it renders as `session-left·session-reset/weekly-left·weekly-days-left`.
- `3p` means `3pm`, `1a` means `1am`, and `3d` means the weekly reset is within the next 3 days.
- On macOS, the plugin can read and refresh auth from the same files/keychain entries used by Claude Code and Codex CLI.

If a provider cannot be read or refreshed, the plugin falls back to stale cache.
If the latest fetch for a provider fails, that provider's text fades to grey until a later fetch for that same provider succeeds.
If a provider responds with `Retry-After`, the plugin waits for that backoff to expire before fetching it again.
If a configured provider still cannot be rendered, its slot becomes `-` (or `-/-` when `@tmux_open_usage_view` is `all`).
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

Refresh interval in minutes, default `15`:

```tmux
set -g @tmux_open_usage_refresh_interval_minutes '15'
```

Use any positive integer. Reload tmux after changing it.

Provider order and visibility. If unset, the plugin auto-detects authenticated providers in the default order `claude,codex`:

```tmux
set -g @tmux_open_usage_providers 'claude,codex'
```

Examples:

```tmux
set -g @tmux_open_usage_providers 'claude'
set -g @tmux_open_usage_providers 'codex'
set -g @tmux_open_usage_providers 'codex,claude'
```

Only `claude` and `codex` are recognized. Unknown names are ignored. When this option is set, it overrides auto-detection. If it is unset and no authenticated providers are found, the plugin renders nothing.

Which usage windows to show, default `session`:

```tmux
set -g @tmux_open_usage_view 'session'
```

- `session` shows only the current 5-hour session quota, for example `82·1a`.
- `all` also appends the 7-day weekly quota, for example `82·1a/55·3d`.

Unknown values fall back to `session`. Reload tmux after changing it.

Per-provider view override, unset by default:

```tmux
set -g @tmux_open_usage_claude_view 'all'
set -g @tmux_open_usage_codex_view 'session'
```

- Each option takes the same values as `@tmux_open_usage_view` and wins over it for that provider only.
- Unknown or empty values fall back to `@tmux_open_usage_view`.
- The gap between segments widens to two spaces as soon as any provider renders the `all` view.

Show Claude's weekly Fable model quota next to the session number, default `off`:

```tmux
set -g @tmux_open_usage_claude_fable 'on'
```

- Fable is a weekly limit, so it is rendered next to whichever number represents the week.
- In the `session` view, the session-left number gains a `/fable-left` suffix, for example `82·1a` becomes `82/94·1a`, where `94` means 94% of the weekly Fable quota is still left.
- In the `all` view, the weekly-left number gains a `(fable-left)` suffix instead, for example `82·1a/55·3d` becomes `82·1a/55(94)·3d`.
- This only affects the Claude segment; Codex is unchanged.
- If the API does not report a Fable limit, the suffix is omitted.

Supported truthy values are `1`, `on`, `yes`, and `true`. Reload tmux after changing it.

## Install

Public TPM install after publishing:

```tmux
set -g @plugin 'genkio/tmux-open-usage'
```

If you are already logged into Claude Code or Codex, you can stop there and let the plugin auto-detect providers.

If you log into Claude Code or Codex later, reload tmux config so the status segment gets attached:

```sh
tmux source-file ~/.tmux.conf
```

If you want Codex only, the minimal config is:

```tmux
set -g @plugin 'genkio/tmux-open-usage'
set -g @tmux_open_usage_providers 'codex'
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
