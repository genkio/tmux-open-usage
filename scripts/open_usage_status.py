#!/usr/bin/env python3

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple
from urllib import error, parse, request

LOCK_TTL_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 8
DEFAULT_REFRESH_INTERVAL_MINUTES = 15
CLAUDE_SHARED_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60

CLAUDE_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
CLAUDE_STATE_PATHS = [
    Path.home() / ".claude.json",
    Path.home() / ".claude-local-oauth.json",
    Path.home() / ".claude-staging-oauth.json",
    Path.home() / ".claude-custom-oauth.json",
]
CLAUDE_SHARED_CACHE_PATH = Path("/tmp/claude_usage_cache.json")
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_REFRESH_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_SCOPES = "user:profile user:inference user:sessions:claude_code user:mcp_servers"
CLAUDE_REFRESH_BUFFER_MS = 5 * 60 * 1000
CLAUDE_BETA_HEADER = "oauth-2025-04-20"

CODEX_AUTH_FILE = "auth.json"
CODEX_KEYCHAIN_SERVICE = "Codex Auth"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_REFRESH_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_REFRESH_AGE_SECONDS = 8 * 24 * 60 * 60

PROVIDERS = ("claude", "codex")
MISSING_PROVIDER_SEGMENT = "-/-"
STATUS_LINE_FG = "#5c5c5c"
FAILED_PROVIDER_FG = STATUS_LINE_FG
PROVIDER_FG = {
    "claude": "#FF9500",
    "codex": "#10A37F",
}


class FetchResult(NamedTuple):
    data: dict[str, Any] | None = None
    fresh: bool = False
    failed: bool = False


def refresh_interval_seconds() -> int:
    raw = os.environ.get("TMUX_OPEN_USAGE_REFRESH_INTERVAL_MINUTES", "").strip()
    if not raw:
        return DEFAULT_REFRESH_INTERVAL_MINUTES * 60
    try:
        minutes = int(raw)
    except ValueError:
        return DEFAULT_REFRESH_INTERVAL_MINUTES * 60
    if minutes <= 0:
        return DEFAULT_REFRESH_INTERVAL_MINUTES * 60
    return minutes * 60


def provider_order() -> list[str]:
    raw = os.environ.get("TMUX_OPEN_USAGE_PROVIDERS", "").strip()
    if not raw:
        ordered: list[str] = []
        if load_claude_credentials():
            ordered.append("claude")
        if load_codex_auth():
            ordered.append("codex")
        return ordered

    ordered: list[str] = []
    seen: set[str] = set()
    for item in raw.split(","):
        provider = item.strip().lower()
        if provider not in PROVIDERS or provider in seen:
            continue
        ordered.append(provider)
        seen.add(provider)

    if not ordered:
        return []
    return ordered


def cache_dir() -> Path:
    override = os.environ.get("TMUX_OPEN_USAGE_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Caches" / "tmux-open-usage"


def cache_path(provider: str) -> Path:
    return cache_dir() / f"{provider}.json"


def lock_path(provider: str) -> Path:
    return cache_dir() / f"{provider}.lock"


def failure_path(provider: str) -> Path:
    return cache_dir() / f"{provider}.failed"


def parse_json_blob(text: str | bytes | None) -> Any:
    if text is None:
        return None
    if isinstance(text, bytes):
        raw = text.decode("utf-8", errors="replace")
    else:
        raw = str(text)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    candidate = raw.strip()
    if candidate.startswith(("0x", "0X")):
        candidate = candidate[2:]
    if not candidate or len(candidate) % 2 != 0:
        return None
    if any(ch not in "0123456789abcdefABCDEF" for ch in candidate):
        return None

    try:
        decoded = bytes.fromhex(candidate).decode("utf-8")
    except ValueError:
        return None

    try:
        return json.loads(decoded)
    except json.JSONDecodeError:
        return None


def atomic_write_text(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        if mode is not None:
            os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def read_json_file(path: Path) -> Any:
    try:
        return parse_json_blob(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def is_file_fresh(path: Path, ttl_seconds: int) -> bool:
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    return age < ttl_seconds


def keychain_read_json(service: str) -> Any:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return parse_json_blob(result.stdout.strip())


def keychain_write_json(service: str, payload: Any) -> None:
    value = json.dumps(payload, separators=(",", ":"))
    subprocess.run(
        [
            "security",
            "add-generic-password",
            "-U",
            "-a",
            os.environ.get("USER", "tmux-open-usage"),
            "-s",
            service,
            "-w",
            value,
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def parse_oauth_scopes(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    scopes = [scope for scope in raw.replace(",", " ").split() if scope]
    return scopes or None


def http_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> dict[str, Any]:
    req = request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return {
                "status": response.getcode(),
                "headers": {key.lower(): value for key, value in response.headers.items()},
                "body": response.read().decode("utf-8", errors="replace"),
            }
    except error.HTTPError as exc:
        return {
            "status": exc.code,
            "headers": {key.lower(): value for key, value in exc.headers.items()},
            "body": exc.read().decode("utf-8", errors="replace"),
        }
    except error.URLError:
        return {"status": 0, "headers": {}, "body": ""}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def read_int(value: Any) -> int | None:
    try:
        parsed = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return parsed


def is_auth_status(status: int) -> bool:
    return status in (401, 403)


def load_claude_credentials() -> dict[str, Any] | None:
    env_access_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env_access_token:
        oauth: dict[str, Any] = {"accessToken": env_access_token}
        env_refresh_token = os.environ.get("CLAUDE_CODE_OAUTH_REFRESH_TOKEN")
        if env_refresh_token:
            oauth["refreshToken"] = env_refresh_token
        env_scopes = parse_oauth_scopes(os.environ.get("CLAUDE_CODE_OAUTH_SCOPES"))
        if env_scopes:
            oauth["scopes"] = env_scopes
        return {"source": "env", "payload": {"claudeAiOauth": oauth}}

    file_payload = read_json_file(CLAUDE_CREDENTIALS_PATH)
    if isinstance(file_payload, dict):
        oauth = file_payload.get("claudeAiOauth")
        if isinstance(oauth, dict) and oauth.get("accessToken"):
            return {"source": "file", "path": CLAUDE_CREDENTIALS_PATH, "payload": file_payload}

    for state_path in CLAUDE_STATE_PATHS:
        state_payload = read_json_file(state_path)
        if not isinstance(state_payload, dict):
            continue
        oauth = state_payload.get("claudeAiOauth")
        if isinstance(oauth, dict) and oauth.get("accessToken"):
            return {"source": "file", "path": state_path, "payload": state_payload}

    keychain_payload = keychain_read_json(CLAUDE_KEYCHAIN_SERVICE)
    if isinstance(keychain_payload, dict):
        oauth = keychain_payload.get("claudeAiOauth")
        if isinstance(oauth, dict) and oauth.get("accessToken"):
            return {"source": "keychain", "service": CLAUDE_KEYCHAIN_SERVICE, "payload": keychain_payload}

    return None


def save_claude_credentials(state: dict[str, Any]) -> None:
    payload = state["payload"]
    if state["source"] == "env":
        return
    if state["source"] == "file":
        atomic_write_text(Path(state["path"]), json.dumps(payload, separators=(",", ":")), mode=0o600)
        return
    if state["source"] == "keychain":
        keychain_write_json(state["service"], payload)


def claude_needs_refresh(oauth: dict[str, Any]) -> bool:
    expires_at = oauth.get("expiresAt")
    if not isinstance(expires_at, (int, float)):
        return False
    return int(time.time() * 1000) >= int(expires_at) - CLAUDE_REFRESH_BUFFER_MS


def refresh_claude_access_token(state: dict[str, Any]) -> str | None:
    oauth = state["payload"].get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    refresh_token = oauth.get("refreshToken")
    if not refresh_token:
        return None

    response = http_request(
        "POST",
        CLAUDE_REFRESH_URL,
        headers={"Content-Type": "application/json"},
        body=json.dumps(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLAUDE_CLIENT_ID,
                "scope": CLAUDE_SCOPES,
            }
        ).encode("utf-8"),
    )
    if response["status"] < 200 or response["status"] >= 300:
        return None

    body = parse_json_blob(response["body"])
    if not isinstance(body, dict):
        return None

    access_token = body.get("access_token")
    if not access_token:
        return None

    oauth["accessToken"] = access_token
    if body.get("refresh_token"):
        oauth["refreshToken"] = body["refresh_token"]
    expires_in = body.get("expires_in")
    if isinstance(expires_in, (int, float)):
        oauth["expiresAt"] = int(time.time() * 1000) + int(expires_in) * 1000

    try:
        save_claude_credentials(state)
    except Exception:
        pass
    return access_token


def normalize_claude_usage(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None

    session = data.get("five_hour")
    weekly = data.get("seven_day")
    if not isinstance(session, dict) or not isinstance(weekly, dict):
        return None

    session_pct = read_int(session.get("utilization"))
    weekly_pct = read_int(weekly.get("utilization"))
    session_reset = session.get("resets_at")
    weekly_reset = weekly.get("resets_at")
    if session_pct is None or weekly_pct is None:
        return None
    if session_reset is not None and parse_iso_datetime(session_reset) is None:
        session_reset = None
    if weekly_reset is not None and parse_iso_datetime(weekly_reset) is None:
        weekly_reset = None
    if session_reset is None and weekly_reset is None:
        return None

    return {
        "provider": "claude",
        "session": {"pct": session_pct, "reset_at": session_reset},
        "weekly": {"pct": weekly_pct, "reset_at": weekly_reset},
    }


def load_shared_claude_usage() -> dict[str, Any] | None:
    if not is_file_fresh(CLAUDE_SHARED_CACHE_PATH, CLAUDE_SHARED_CACHE_MAX_AGE_SECONDS):
        return None
    return normalize_claude_usage(read_json_file(CLAUDE_SHARED_CACHE_PATH))


def fetch_claude_status_result() -> FetchResult:
    state = load_claude_credentials()
    if not state:
        return FetchResult(load_shared_claude_usage())

    oauth = state["payload"]["claudeAiOauth"]
    access_token = oauth.get("accessToken")
    if not access_token:
        return FetchResult()

    if claude_needs_refresh(oauth):
        refreshed = refresh_claude_access_token(state)
        if refreshed:
            access_token = refreshed

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "anthropic-beta": CLAUDE_BETA_HEADER,
    }

    response = http_request("GET", CLAUDE_USAGE_URL, headers=headers)
    if is_auth_status(response["status"]):
        refreshed = refresh_claude_access_token(state)
        if not refreshed:
            return FetchResult(load_shared_claude_usage(), failed=True)
        headers["Authorization"] = f"Bearer {refreshed}"
        response = http_request("GET", CLAUDE_USAGE_URL, headers=headers)

    if response["status"] < 200 or response["status"] >= 300:
        return FetchResult(load_shared_claude_usage(), failed=True)

    normalized = normalize_claude_usage(parse_json_blob(response["body"]))
    if normalized:
        return FetchResult(normalized, fresh=True)
    return FetchResult(load_shared_claude_usage(), failed=True)


def fetch_claude_status() -> dict[str, Any] | None:
    return fetch_claude_status_result().data


def resolve_codex_auth_paths() -> list[Path]:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return [Path(codex_home).expanduser() / CODEX_AUTH_FILE]
    return [
        Path.home() / ".config" / "codex" / CODEX_AUTH_FILE,
        Path.home() / ".codex" / CODEX_AUTH_FILE,
    ]


def load_codex_auth() -> dict[str, Any] | None:
    for auth_path in resolve_codex_auth_paths():
        payload = read_json_file(auth_path)
        if not isinstance(payload, dict):
            continue
        tokens = payload.get("tokens")
        if isinstance(tokens, dict) and tokens.get("access_token"):
            return {"source": "file", "path": auth_path, "payload": payload}

    keychain_payload = keychain_read_json(CODEX_KEYCHAIN_SERVICE)
    if isinstance(keychain_payload, dict):
        tokens = keychain_payload.get("tokens")
        if isinstance(tokens, dict) and tokens.get("access_token"):
            return {"source": "keychain", "service": CODEX_KEYCHAIN_SERVICE, "payload": keychain_payload}

    return None


def save_codex_auth(state: dict[str, Any]) -> None:
    payload = state["payload"]
    if state["source"] == "file":
        atomic_write_text(Path(state["path"]), json.dumps(payload, indent=2) + "\n", mode=0o600)
        return
    if state["source"] == "keychain":
        keychain_write_json(state["service"], payload)


def codex_needs_refresh(payload: dict[str, Any]) -> bool:
    last_refresh = parse_iso_datetime(payload.get("last_refresh"))
    if last_refresh is None:
        return True
    return (now_utc() - last_refresh.astimezone(timezone.utc)).total_seconds() > CODEX_REFRESH_AGE_SECONDS


def refresh_codex_access_token(state: dict[str, Any]) -> str | None:
    tokens = state["payload"].get("tokens")
    if not isinstance(tokens, dict):
        return None
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        return None

    response = http_request(
        "POST",
        CODEX_REFRESH_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=parse.urlencode(
            {
                "grant_type": "refresh_token",
                "client_id": CODEX_CLIENT_ID,
                "refresh_token": refresh_token,
            }
        ).encode("utf-8"),
    )
    if response["status"] < 200 or response["status"] >= 300:
        return None

    body = parse_json_blob(response["body"])
    if not isinstance(body, dict) or not body.get("access_token"):
        return None

    tokens["access_token"] = body["access_token"]
    if body.get("refresh_token"):
        tokens["refresh_token"] = body["refresh_token"]
    if body.get("id_token"):
        tokens["id_token"] = body["id_token"]
    state["payload"]["last_refresh"] = to_iso_utc(now_utc())

    try:
        save_codex_auth(state)
    except Exception:
        pass
    return tokens["access_token"]


def codex_reset_iso(window: Any, now: datetime) -> str | None:
    if not isinstance(window, dict):
        return None
    reset_at = window.get("reset_at")
    if isinstance(reset_at, (int, float)):
        return to_iso_utc(datetime.fromtimestamp(reset_at, tz=timezone.utc))
    reset_after = window.get("reset_after_seconds")
    if isinstance(reset_after, (int, float)):
        return to_iso_utc(now + timedelta(seconds=int(reset_after)))
    return None


def normalize_codex_usage(data: Any, headers: dict[str, str] | None = None, now: datetime | None = None) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None

    headers = headers or {}
    now = now or now_utc()
    rate_limit = data.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return None

    primary = rate_limit.get("primary_window")
    secondary = rate_limit.get("secondary_window")
    if not isinstance(primary, dict) or not isinstance(secondary, dict):
        return None

    session_pct = read_int(headers.get("x-codex-primary-used-percent"))
    if session_pct is None:
        session_pct = read_int(primary.get("used_percent"))
    weekly_pct = read_int(headers.get("x-codex-secondary-used-percent"))
    if weekly_pct is None:
        weekly_pct = read_int(secondary.get("used_percent"))

    session_reset = codex_reset_iso(primary, now)
    weekly_reset = codex_reset_iso(secondary, now)

    if session_pct is None or weekly_pct is None or not session_reset or not weekly_reset:
        return None

    return {
        "provider": "codex",
        "session": {"pct": session_pct, "reset_at": session_reset},
        "weekly": {"pct": weekly_pct, "reset_at": weekly_reset},
    }


def fetch_codex_status_result() -> FetchResult:
    state = load_codex_auth()
    if not state:
        return FetchResult()

    payload = state["payload"]
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        return FetchResult()

    access_token = tokens.get("access_token")
    if not access_token:
        return FetchResult()

    if codex_needs_refresh(payload):
        refreshed = refresh_codex_access_token(state)
        if refreshed:
            access_token = refreshed

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "tmux-open-usage",
    }
    account_id = tokens.get("account_id")
    if account_id:
        headers["ChatGPT-Account-Id"] = str(account_id)

    response = http_request("GET", CODEX_USAGE_URL, headers=headers)
    if is_auth_status(response["status"]):
        refreshed = refresh_codex_access_token(state)
        if not refreshed:
            return FetchResult(failed=True)
        headers["Authorization"] = f"Bearer {refreshed}"
        response = http_request("GET", CODEX_USAGE_URL, headers=headers)

    if response["status"] < 200 or response["status"] >= 300:
        return FetchResult(failed=True)

    normalized = normalize_codex_usage(parse_json_blob(response["body"]), response["headers"], now_utc())
    if normalized:
        return FetchResult(normalized, fresh=True)
    return FetchResult(failed=True)


def fetch_codex_status() -> dict[str, Any] | None:
    return fetch_codex_status_result().data


def fetch_provider_result(provider: str) -> FetchResult:
    if provider == "claude":
        return fetch_claude_status_result()
    if provider == "codex":
        return fetch_codex_status_result()
    return FetchResult()


def load_cached_status(provider: str) -> dict[str, Any] | None:
    data = read_json_file(cache_path(provider))
    if isinstance(data, dict):
        return data
    return None


def cache_is_fresh(provider: str) -> bool:
    return is_file_fresh(cache_path(provider), refresh_interval_seconds())


def lock_is_active(provider: str) -> bool:
    return is_file_fresh(lock_path(provider), LOCK_TTL_SECONDS)


def write_lock(provider: str) -> None:
    atomic_write_text(lock_path(provider), str(int(time.time())))


def clear_lock(provider: str) -> None:
    lock_path(provider).unlink(missing_ok=True)


def write_cached_status(provider: str, data: dict[str, Any]) -> None:
    atomic_write_text(cache_path(provider), json.dumps(data, separators=(",", ":")))


def mark_fetch_failure(provider: str) -> None:
    atomic_write_text(failure_path(provider), str(int(time.time())))


def clear_fetch_failure(provider: str) -> None:
    failure_path(provider).unlink(missing_ok=True)


def provider_fetch_failed(provider: str) -> bool:
    return failure_path(provider).exists()


def refresh_provider_cache(provider: str) -> int:
    write_lock(provider)
    try:
        result = fetch_provider_result(provider)
        if result.data:
            write_cached_status(provider, result.data)
        if result.fresh:
            clear_fetch_failure(provider)
        elif result.failed:
            mark_fetch_failure(provider)
        if result.data:
            return 0
        return 1
    finally:
        clear_lock(provider)


def refresh_in_background(provider: str) -> None:
    if lock_is_active(provider):
        return
    write_lock(provider)
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--refresh", provider],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def get_provider_status(provider: str) -> dict[str, Any] | None:
    cached = load_cached_status(provider)
    if cached and cache_is_fresh(provider):
        return cached
    if cached:
        refresh_in_background(provider)
        return cached

    if refresh_provider_cache(provider) == 0:
        return load_cached_status(provider)
    return None


def format_compact_hour(value: datetime) -> str:
    rounded = value + timedelta(minutes=30)
    rounded = rounded.replace(minute=0, second=0, microsecond=0)
    hour = rounded.strftime("%I").lstrip("0") or "12"
    return f"{hour}{rounded.strftime('%p').lower()}"


def format_reset_time(reset_at: Any, now: datetime | None = None) -> str:
    target = parse_iso_datetime(reset_at)
    if target is None:
        return "?"

    if now is None:
        current = datetime.now().astimezone()
    elif now.tzinfo is None:
        current = now.replace(tzinfo=timezone.utc)
    else:
        current = now
    local_target = target.astimezone(current.tzinfo)
    day_diff = (local_target.date() - current.date()).days
    time_text = format_compact_hour(local_target)

    if day_diff == 0:
        return time_text
    if day_diff == 1:
        return f"tmr {time_text}"
    if 1 < day_diff < 7:
        return f"{local_target.strftime('%a')} {time_text}"
    return f"{local_target.strftime('%b')}{local_target.day} {time_text}"


def clamp_percent(value: int | None) -> int | None:
    if value is None:
        return None
    return max(0, min(100, value))


def remaining_percent(used_percent: int | None) -> int | None:
    clamped = clamp_percent(used_percent)
    if clamped is None:
        return None
    return 100 - clamped


def to_local_time(value: Any, now: datetime | None = None) -> datetime | None:
    target = parse_iso_datetime(value)
    if target is None:
        return None
    if now is None:
        current = datetime.now().astimezone()
    elif now.tzinfo is None:
        current = now.replace(tzinfo=timezone.utc)
    else:
        current = now
    return target.astimezone(current.tzinfo)


def format_short_reset_clock(reset_at: Any, now: datetime | None = None) -> str:
    local_target = to_local_time(reset_at, now=now)
    if local_target is None:
        return "-"
    rounded = local_target + timedelta(minutes=30)
    rounded = rounded.replace(minute=0, second=0, microsecond=0)
    hour = rounded.strftime("%I").lstrip("0") or "12"
    suffix = "a" if rounded.strftime("%p") == "AM" else "p"
    return f"{hour}{suffix}"


def format_days_until_reset(reset_at: Any, now: datetime | None = None) -> str:
    local_target = to_local_time(reset_at, now=now)
    if local_target is None:
        return "-"
    if now is None:
        current = datetime.now(local_target.tzinfo)
    elif now.tzinfo is None:
        current = now.replace(tzinfo=timezone.utc).astimezone(local_target.tzinfo)
    else:
        current = now.astimezone(local_target.tzinfo)

    seconds_left = (local_target - current).total_seconds()
    if seconds_left <= 0:
        return "0d"
    if local_target.date() == current.date():
        return "0d"
    return f"{math.ceil(seconds_left / 86400)}d"


def render_provider_segment(provider: str, data: dict[str, Any], now: datetime | None = None) -> str | None:
    session = data.get("session")
    weekly = data.get("weekly")
    if not isinstance(session, dict) or not isinstance(weekly, dict):
        return None

    session_left = remaining_percent(read_int(session.get("pct")))
    weekly_left = remaining_percent(read_int(weekly.get("pct")))
    session_reset = session.get("reset_at")
    weekly_reset = weekly.get("reset_at")
    if session_left is None or weekly_left is None:
        return None

    return (
        f"{session_left}·{format_short_reset_clock(session_reset, now=now)}"
        f"/{weekly_left}·{format_days_until_reset(weekly_reset, now=now)}"
    )


def style_provider_part(provider: str, part: str) -> str:
    if provider_fetch_failed(provider):
        color = FAILED_PROVIDER_FG
    else:
        color = PROVIDER_FG.get(provider, STATUS_LINE_FG)
    return f"#[fg={color}]{part}#[fg={STATUS_LINE_FG}]"


def join_status_parts(parts: list[str]) -> str:
    if not parts:
        return ""
    return " " + "  ".join(parts)


def render_status_line() -> str:
    parts: list[str] = []
    for provider in provider_order():
        data = get_provider_status(provider)
        if not data:
            parts.append(style_provider_part(provider, MISSING_PROVIDER_SEGMENT))
            continue
        part = render_provider_segment(provider, data)
        parts.append(style_provider_part(provider, part if part else MISSING_PROVIDER_SEGMENT))
    return join_status_parts(parts)


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[1] == "--has-provider":
        return 0 if provider_order() else 1

    if len(argv) == 3 and argv[1] == "--refresh" and argv[2] in PROVIDERS:
        return refresh_provider_cache(argv[2])

    try:
        sys.stdout.write(render_status_line())
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
