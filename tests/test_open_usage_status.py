import importlib.util
from datetime import datetime, timezone
from pathlib import Path
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

    def test_format_days_until_reset(self) -> None:
        now = datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(MODULE.format_days_until_reset("2026-03-10T15:20:00Z", now=now), "3d")

    def test_refresh_interval_seconds_defaults_to_five_minutes(self) -> None:
        with mock.patch.dict(MODULE.os.environ, {}, clear=False):
            self.assertEqual(MODULE.refresh_interval_seconds(), 300)

    def test_refresh_interval_seconds_uses_positive_env_override(self) -> None:
        with mock.patch.dict(MODULE.os.environ, {"TMUX_OPEN_USAGE_REFRESH_INTERVAL_MINUTES": "12"}, clear=False):
            self.assertEqual(MODULE.refresh_interval_seconds(), 720)

    def test_refresh_interval_seconds_rejects_invalid_override(self) -> None:
        with mock.patch.dict(MODULE.os.environ, {"TMUX_OPEN_USAGE_REFRESH_INTERVAL_MINUTES": "0"}, clear=False):
            self.assertEqual(MODULE.refresh_interval_seconds(), 300)

    def test_provider_order_defaults_to_claude_then_codex(self) -> None:
        with mock.patch.dict(MODULE.os.environ, {}, clear=False):
            self.assertEqual(MODULE.provider_order(), ["claude", "codex"])

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

    def test_provider_order_falls_back_when_no_valid_provider_is_configured(self) -> None:
        with mock.patch.dict(MODULE.os.environ, {"TMUX_OPEN_USAGE_PROVIDERS": "unknown"}, clear=False):
            self.assertEqual(MODULE.provider_order(), ["claude", "codex"])

    def test_join_status_parts(self) -> None:
        self.assertEqual(
            MODULE.join_status_parts(["82%1a/55%3d", "23%10p/90%5d"]),
            "[82%1a/55%3d | 23%10p/90%5d]",
        )

    def test_join_status_parts_with_single_provider_has_no_separator(self) -> None:
        self.assertEqual(MODULE.join_status_parts(["82%1a/55%3d"]), "[82%1a/55%3d]")

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
            mock.patch.object(MODULE, "render_provider_segment", side_effect=["82%1a/55%3d", None]),
        ):
            self.assertEqual(MODULE.render_status_line(), "[82%1a/55%3d | -/-]")

    def test_render_status_line_single_missing_provider_has_no_separator(self) -> None:
        with (
            mock.patch.object(MODULE, "provider_order", return_value=["claude"]),
            mock.patch.object(MODULE, "get_provider_status", return_value=None),
        ):
            self.assertEqual(MODULE.render_status_line(), "[-/-]")

    def test_render_provider_segment(self) -> None:
        segment = MODULE.render_provider_segment(
            "codex",
            {
                "session": {"pct": 9, "reset_at": "2026-03-08T15:20:00Z"},
                "weekly": {"pct": 31, "reset_at": "2026-03-10T15:20:00Z"},
            },
            now=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(segment, "91%3p/69%3d")


if __name__ == "__main__":
    unittest.main()
