import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from unittest import mock
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "open_usage_status.py"
SPEC = importlib.util.spec_from_file_location("open_usage_status", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class OpenUsageStatusTests(unittest.TestCase):
    def test_format_reset_time_today(self) -> None:
        now = datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(MODULE.format_reset_time("2026-03-08T15:20:00Z", now=now), "3pm")

    def test_format_reset_time_tomorrow(self) -> None:
        now = datetime(2026, 3, 8, 23, 0, tzinfo=timezone.utc)
        self.assertEqual(MODULE.format_reset_time("2026-03-09T09:10:00Z", now=now), "tmr 9am")

    def test_format_reset_time_future_date(self) -> None:
        now = datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(MODULE.format_reset_time("2026-03-18T15:20:00Z", now=now), "Mar18 3pm")

    def test_normalize_claude_usage(self) -> None:
        payload = {
            "five_hour": {"utilization": 24, "resets_at": "2026-03-08T15:00:00Z"},
            "seven_day": {"utilization": 61, "resets_at": "2026-03-12T02:00:00Z"},
        }
        self.assertEqual(
            MODULE.normalize_claude_usage(payload),
            {
                "provider": "claude",
                "session": {"pct": 24, "reset_at": "2026-03-08T15:00:00Z"},
                "weekly": {"pct": 61, "reset_at": "2026-03-12T02:00:00Z"},
            },
        )

    def test_normalize_claude_usage_accepts_missing_session_reset(self) -> None:
        payload = {
            "five_hour": {"utilization": 0, "resets_at": None},
            "seven_day": {"utilization": 17, "resets_at": "2026-03-14T06:00:00.507675+00:00"},
        }
        self.assertEqual(
            MODULE.normalize_claude_usage(payload),
            {
                "provider": "claude",
                "session": {"pct": 0, "reset_at": None},
                "weekly": {"pct": 17, "reset_at": "2026-03-14T06:00:00.507675+00:00"},
            },
        )

    def test_load_shared_claude_usage(self) -> None:
        payload = {
            "five_hour": {"utilization": 1, "resets_at": "2026-03-08T14:00:00Z"},
            "seven_day": {"utilization": 7, "resets_at": "2026-03-13T04:00:00Z"},
        }
        with (
            mock.patch.object(MODULE, "is_file_fresh", return_value=True),
            mock.patch.object(MODULE, "read_json_file", return_value=payload),
        ):
            self.assertEqual(
                MODULE.load_shared_claude_usage(),
                {
                    "provider": "claude",
                    "session": {"pct": 1, "reset_at": "2026-03-08T14:00:00Z"},
                    "weekly": {"pct": 7, "reset_at": "2026-03-13T04:00:00Z"},
                },
            )

    def test_load_shared_claude_usage_ignores_stale_cache(self) -> None:
        with mock.patch.object(MODULE, "is_file_fresh", return_value=False):
            self.assertIsNone(MODULE.load_shared_claude_usage())

    def test_load_shared_claude_usage_uses_dedicated_max_age(self) -> None:
        with mock.patch.object(MODULE, "is_file_fresh", return_value=False) as mocked:
            MODULE.load_shared_claude_usage()
            mocked.assert_called_once_with(MODULE.CLAUDE_SHARED_CACHE_PATH, MODULE.CLAUDE_SHARED_CACHE_MAX_AGE_SECONDS)

    def test_fetch_claude_status_falls_back_to_shared_cache_without_credentials(self) -> None:
        fallback = {
            "provider": "claude",
            "session": {"pct": 1, "reset_at": "2026-03-08T14:00:00Z"},
            "weekly": {"pct": 7, "reset_at": "2026-03-13T04:00:00Z"},
        }
        with (
            mock.patch.object(MODULE, "load_claude_credentials", return_value=None),
            mock.patch.object(MODULE, "load_shared_claude_usage", return_value=fallback),
        ):
            self.assertEqual(MODULE.fetch_claude_status(), fallback)

    def test_fetch_claude_status_result_marks_failed_live_fetch_when_using_shared_cache(self) -> None:
        fallback = {
            "provider": "claude",
            "session": {"pct": 1, "reset_at": "2026-03-08T14:00:00Z"},
            "weekly": {"pct": 7, "reset_at": "2026-03-13T04:00:00Z"},
        }
        with (
            mock.patch.object(
                MODULE,
                "load_claude_credentials",
                return_value={"payload": {"claudeAiOauth": {"accessToken": "token-123"}}},
            ),
            mock.patch.object(MODULE, "claude_needs_refresh", return_value=False),
            mock.patch.object(MODULE, "http_request", return_value={"status": 503, "headers": {}, "body": ""}),
            mock.patch.object(MODULE, "load_shared_claude_usage", return_value=fallback),
        ):
            self.assertEqual(
                MODULE.fetch_claude_status_result(),
                MODULE.FetchResult(fallback, failed=True),
            )

    def test_load_claude_credentials_supports_env_oauth_token(self) -> None:
        with mock.patch.dict(
            MODULE.os.environ,
            {
                "CLAUDE_CODE_OAUTH_TOKEN": "token-123",
                "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "refresh-456",
                "CLAUDE_CODE_OAUTH_SCOPES": "user:profile user:inference",
            },
            clear=False,
        ):
            self.assertEqual(
                MODULE.load_claude_credentials(),
                {
                    "source": "env",
                    "payload": {
                        "claudeAiOauth": {
                            "accessToken": "token-123",
                            "refreshToken": "refresh-456",
                            "scopes": ["user:profile", "user:inference"],
                        }
                    },
                },
            )

    def test_load_claude_credentials_supports_claude_json_state_file(self) -> None:
        payload = {"claudeAiOauth": {"accessToken": "token-123"}}

        def fake_read_json_file(path):
            if path == MODULE.CLAUDE_CREDENTIALS_PATH:
                return None
            if path == MODULE.CLAUDE_STATE_PATHS[0]:
                return payload
            return None

        with (
            mock.patch.dict(MODULE.os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": ""}, clear=False),
            mock.patch.object(MODULE, "read_json_file", side_effect=fake_read_json_file),
            mock.patch.object(MODULE, "keychain_read_json", return_value=None),
        ):
            self.assertEqual(
                MODULE.load_claude_credentials(),
                {"source": "file", "path": MODULE.CLAUDE_STATE_PATHS[0], "payload": payload},
            )

    def test_normalize_codex_usage_prefers_headers(self) -> None:
        payload = {
            "rate_limit": {
                "primary_window": {"used_percent": 6, "reset_at": 1772982000},
                "secondary_window": {"used_percent": 24, "reset_at": 1773241200},
            }
        }
        result = MODULE.normalize_codex_usage(
            payload,
            headers={
                "x-codex-primary-used-percent": "9",
                "x-codex-secondary-used-percent": "31",
            },
            now=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            result,
            {
                "provider": "codex",
                "session": {"pct": 9, "reset_at": "2026-03-08T15:00:00Z"},
                "weekly": {"pct": 31, "reset_at": "2026-03-11T15:00:00Z"},
            },
        )

    def test_format_short_reset_clock(self) -> None:
        now = datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(MODULE.format_short_reset_clock("2026-03-08T15:20:00Z", now=now), "3p")

    def test_format_short_reset_clock_handles_missing_reset(self) -> None:
        self.assertEqual(MODULE.format_short_reset_clock(None), "-")

    def test_format_days_until_reset(self) -> None:
        now = datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(MODULE.format_days_until_reset("2026-03-10T15:20:00Z", now=now), "3d")

    def test_format_days_until_reset_shows_zero_for_same_day_reset(self) -> None:
        now = datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(MODULE.format_days_until_reset("2026-03-08T13:00:00Z", now=now), "0d")

    def test_format_days_until_reset_handles_missing_reset(self) -> None:
        self.assertEqual(MODULE.format_days_until_reset(None), "-")

    def test_refresh_interval_seconds_defaults_to_fifteen_minutes(self) -> None:
        with mock.patch.dict(MODULE.os.environ, {}, clear=False):
            self.assertEqual(MODULE.refresh_interval_seconds(), 900)

    def test_refresh_interval_seconds_uses_positive_env_override(self) -> None:
        with mock.patch.dict(MODULE.os.environ, {"TMUX_OPEN_USAGE_REFRESH_INTERVAL_MINUTES": "12"}, clear=False):
            self.assertEqual(MODULE.refresh_interval_seconds(), 720)

    def test_refresh_interval_seconds_rejects_invalid_override(self) -> None:
        with mock.patch.dict(MODULE.os.environ, {"TMUX_OPEN_USAGE_REFRESH_INTERVAL_MINUTES": "0"}, clear=False):
            self.assertEqual(MODULE.refresh_interval_seconds(), 900)

    def test_provider_order_auto_detects_available_providers(self) -> None:
        with (
            mock.patch.dict(MODULE.os.environ, {}, clear=False),
            mock.patch.object(MODULE, "load_claude_credentials", return_value={"payload": {"claudeAiOauth": {"accessToken": "claude-token"}}}),
            mock.patch.object(MODULE, "load_codex_auth", return_value={"payload": {"tokens": {"access_token": "codex-token"}}}),
        ):
            self.assertEqual(MODULE.provider_order(), ["claude", "codex"])

    def test_provider_order_auto_detects_single_provider(self) -> None:
        with (
            mock.patch.dict(MODULE.os.environ, {}, clear=False),
            mock.patch.object(MODULE, "load_claude_credentials", return_value=None),
            mock.patch.object(MODULE, "load_codex_auth", return_value={"payload": {"tokens": {"access_token": "codex-token"}}}),
        ):
            self.assertEqual(MODULE.provider_order(), ["codex"])

    def test_provider_order_does_not_auto_detect_from_claude_shared_cache(self) -> None:
        with (
            mock.patch.dict(MODULE.os.environ, {}, clear=False),
            mock.patch.object(MODULE, "load_claude_credentials", return_value=None),
            mock.patch.object(MODULE, "load_codex_auth", return_value=None),
            mock.patch.object(MODULE, "load_shared_claude_usage") as mocked_shared_cache,
        ):
            self.assertEqual(MODULE.provider_order(), [])
            mocked_shared_cache.assert_not_called()

    def test_provider_order_accepts_single_provider(self) -> None:
        with mock.patch.dict(MODULE.os.environ, {"TMUX_OPEN_USAGE_PROVIDERS": "claude"}, clear=False):
            self.assertEqual(MODULE.provider_order(), ["claude"])

    def test_provider_order_accepts_custom_order(self) -> None:
        with mock.patch.dict(MODULE.os.environ, {"TMUX_OPEN_USAGE_PROVIDERS": "codex,claude"}, clear=False):
            self.assertEqual(MODULE.provider_order(), ["codex", "claude"])

    def test_provider_order_ignores_invalid_and_duplicate_names(self) -> None:
        with mock.patch.dict(
            MODULE.os.environ,
            {"TMUX_OPEN_USAGE_PROVIDERS": "codex,unknown,claude,codex"},
            clear=False,
        ):
            self.assertEqual(MODULE.provider_order(), ["codex", "claude"])

    def test_provider_order_returns_empty_when_no_valid_provider_is_configured(self) -> None:
        with mock.patch.dict(MODULE.os.environ, {"TMUX_OPEN_USAGE_PROVIDERS": "unknown"}, clear=False):
            self.assertEqual(MODULE.provider_order(), [])

    def test_main_has_provider_returns_success_when_provider_is_available(self) -> None:
        with mock.patch.object(MODULE, "provider_order", return_value=["codex"]):
            self.assertEqual(MODULE.main(["open_usage_status.py", "--has-provider"]), 0)

    def test_main_has_provider_returns_failure_when_no_provider_is_available(self) -> None:
        with mock.patch.object(MODULE, "provider_order", return_value=[]):
            self.assertEqual(MODULE.main(["open_usage_status.py", "--has-provider"]), 1)

    def test_join_status_parts(self) -> None:
        self.assertEqual(
            MODULE.join_status_parts(["82·1a/55·3d", "23·10p/90·5d"]),
            " 82·1a/55·3d  23·10p/90·5d",
        )

    def test_join_status_parts_with_single_provider_has_no_separator(self) -> None:
        self.assertEqual(MODULE.join_status_parts(["82·1a/55·3d"]), " 82·1a/55·3d")

    def test_render_status_line_uses_placeholder_for_missing_provider_data(self) -> None:
        with (
            mock.patch.object(MODULE, "provider_order", return_value=["claude", "codex"]),
            mock.patch.object(
                MODULE,
                "get_provider_status",
                side_effect=[
                    {"session": {"pct": 18, "reset_at": "2026-03-09T01:10:00Z"}, "weekly": {"pct": 45, "reset_at": "2026-03-11T09:00:00Z"}},
                    None,
                ],
            ),
            mock.patch.object(MODULE, "render_provider_segment", side_effect=["82·1a/55·3d", None]),
            mock.patch.object(MODULE, "provider_fetch_failed", return_value=False),
        ):
            self.assertEqual(
                MODULE.render_status_line(),
                " #[fg=#F5A623]82·1a/55·3d#[fg=#5c5c5c]  #[fg=#10A37F]-/-#[fg=#5c5c5c]",
            )

    def test_render_status_line_single_missing_provider_has_no_separator(self) -> None:
        with (
            mock.patch.object(MODULE, "provider_order", return_value=["claude"]),
            mock.patch.object(MODULE, "get_provider_status", return_value=None),
            mock.patch.object(MODULE, "provider_fetch_failed", return_value=False),
        ):
            self.assertEqual(
                MODULE.render_status_line(),
                " #[fg=#F5A623]-/-#[fg=#5c5c5c]",
            )

    def test_render_status_line_returns_empty_when_no_providers_are_configured(self) -> None:
        with mock.patch.object(MODULE, "provider_order", return_value=[]):
            self.assertEqual(MODULE.render_status_line(), "")

    def test_render_status_line_colors_failed_provider_text_red(self) -> None:
        with (
            mock.patch.object(MODULE, "provider_order", return_value=["claude", "codex"]),
            mock.patch.object(
                MODULE,
                "get_provider_status",
                side_effect=[
                    {"session": {"pct": 18, "reset_at": "2026-03-09T01:10:00Z"}, "weekly": {"pct": 45, "reset_at": "2026-03-11T09:00:00Z"}},
                    {"session": {"pct": 77, "reset_at": "2026-03-09T10:00:00Z"}, "weekly": {"pct": 10, "reset_at": "2026-03-15T09:00:00Z"}},
                ],
            ),
            mock.patch.object(MODULE, "render_provider_segment", side_effect=["82·1a/55·3d", "23·10p/90·5d"]),
            mock.patch.object(MODULE, "provider_fetch_failed", side_effect=[True, False]),
        ):
            self.assertEqual(
                MODULE.render_status_line(),
                " #[fg=red]82·1a/55·3d#[fg=#5c5c5c]  #[fg=#10A37F]23·10p/90·5d#[fg=#5c5c5c]",
            )

    def test_refresh_provider_cache_keeps_failure_flag_until_fresh_success(self) -> None:
        fallback = {
            "provider": "claude",
            "session": {"pct": 1, "reset_at": "2026-03-08T14:00:00Z"},
            "weekly": {"pct": 7, "reset_at": "2026-03-13T04:00:00Z"},
        }
        fresh = {
            "provider": "claude",
            "session": {"pct": 2, "reset_at": "2026-03-08T16:00:00Z"},
            "weekly": {"pct": 8, "reset_at": "2026-03-14T04:00:00Z"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch.dict(MODULE.os.environ, {"TMUX_OPEN_USAGE_CACHE_DIR": temp_dir}, clear=False),
                mock.patch.object(
                    MODULE,
                    "fetch_provider_result",
                    side_effect=[
                        MODULE.FetchResult(fallback, failed=True),
                        MODULE.FetchResult(fallback),
                        MODULE.FetchResult(fresh, fresh=True),
                    ],
                ),
            ):
                self.assertEqual(MODULE.refresh_provider_cache("claude"), 0)
                self.assertTrue(MODULE.provider_fetch_failed("claude"))

                self.assertEqual(MODULE.refresh_provider_cache("claude"), 0)
                self.assertTrue(MODULE.provider_fetch_failed("claude"))

                self.assertEqual(MODULE.refresh_provider_cache("claude"), 0)
                self.assertFalse(MODULE.provider_fetch_failed("claude"))
                self.assertEqual(MODULE.load_cached_status("claude"), fresh)

    def test_render_provider_segment(self) -> None:
        segment = MODULE.render_provider_segment(
            "codex",
            {
                "session": {"pct": 9, "reset_at": "2026-03-08T15:20:00Z"},
                "weekly": {"pct": 31, "reset_at": "2026-03-10T15:20:00Z"},
            },
            now=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(segment, "91·3p/69·3d")

    def test_render_provider_segment_handles_missing_session_reset(self) -> None:
        segment = MODULE.render_provider_segment(
            "claude",
            {
                "session": {"pct": 0, "reset_at": None},
                "weekly": {"pct": 17, "reset_at": "2026-03-14T06:00:00.507675+00:00"},
            },
            now=datetime(2026, 3, 9, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(segment, "100·-/83·6d")


if __name__ == "__main__":
    unittest.main()
