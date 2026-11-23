#!/usr/bin/env python3
"""Instagram Unfollow Checker (instagrapi edition).

Monitors a target Instagram account's followers once per day using a secondary
(burner) account via instagrapi's mobile private API, following official
instagrapi best practices:
  - Persistent device & session settings (data/session.json) via load_settings / dump_settings
  - Session validation via get_timeline_feed() with device UUID preservation (set_uuids)
  - Randomized per-request pacing (delay_range = [1, 3]) + startup jitter
  - Optional dedicated static proxy support (PROXY_URL)
  - ID-based follower diffing (immune to handle renames)
  - 20-hour desktop cooldown safeguard (data/last_run.txt)
  - Push notifications & health alerts via ntfy.sh and optional Discord webhooks
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import random
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None  # type: ignore[assignment]

import requests
from dotenv import load_dotenv
from instagrapi import Client
from instagrapi import exceptions as ig_exc

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
DATA_DIR = BASE_DIR / "data"
SESSION_FILE = DATA_DIR / "session.json"
STATE_FILE = DATA_DIR / "followers_state.json"
PENDING_FILE = DATA_DIR / "pending_unfollows.json"
LAST_RUN_FILE = DATA_DIR / "last_run.txt"
LOCK_FILE = DATA_DIR / "checker.lock"
LOG_FILE = DATA_DIR / "checker.log"

COOLDOWN_HOURS = 20
STARTUP_DELAY_MIN_SECONDS = 10
STARTUP_DELAY_MAX_SECONDS = 45
REQUEST_DELAY_RANGE = [1, 3]
PRIVATE_UNVERIFIED_MISS_THRESHOLD = 2
HTTP_TIMEOUT_SECONDS = 15

PLACEHOLDER_VALUES = {
    "",
    "your_burner_username",
    "your_burner_password",
    "target_account_username",
    "https://ntfy.sh/your-secret-ig-topic",
}

logger = logging.getLogger("ig_unfollow_checker")


@dataclass(frozen=True)
class Config:
    bot_username: str
    bot_password: str
    target_username: str
    ntfy_url: str
    discord_webhook_url: str
    proxy_url: str
    ntfy_token: str = ""


@dataclass(frozen=True)
class DiffResult:
    unfollowed: list[str]
    unfollowed_entries: list[tuple[str, str]]  # (user_id, username)
    new_followers: list[str]
    renamed: list[tuple[str, str, str]]  # (user_id, old_username, new_username)


@dataclass(frozen=True)
class VerificationResult:
    followers: dict[str, str]
    deactivated_ids: set[str]
    pending_unfollows: dict[str, dict[str, Any]]


def setup_logging() -> None:
    """Configure rotating file logging (data/checker.log) and stdout logging."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=2 * 1024 * 1024,  # 2 MiB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    # Keep instagrapi's internal logger quiet unless warning/error
    logging.getLogger("instagrapi").setLevel(logging.WARNING)
    logging.getLogger("public_request").setLevel(logging.WARNING)
    logging.getLogger("private_request").setLevel(logging.WARNING)


def is_placeholder(value: str) -> bool:
    """Return True if a config value is empty or still uses an example placeholder."""
    return value.strip() in PLACEHOLDER_VALUES


def load_config() -> Config:
    """Load configuration from .env in the project root."""
    load_dotenv(dotenv_path=ENV_FILE, override=False)

    return Config(
        bot_username=os.getenv("BOT_USERNAME", "").strip().lstrip("@"),
        bot_password=os.getenv("BOT_PASSWORD", "").strip(),
        target_username=os.getenv("TARGET_USERNAME", "").strip().lstrip("@"),
        ntfy_url=os.getenv("NTFY_URL", "").strip(),
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", "").strip(),
        proxy_url=os.getenv("PROXY_URL", "").strip(),
        ntfy_token=os.getenv("NTFY_TOKEN", "").strip(),
    )


def write_text_atomic(path: Path, content: str) -> None:
    """Write a text file atomically to avoid corruption on unexpected shutdown."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically with stable formatting."""
    serialized = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    write_text_atomic(path, serialized)


def restrict_file_permissions(path: Path) -> None:
    """Restrict sensitive files (like session.json) to owner read/write on POSIX."""
    if os.name == "posix" and path.exists():
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            logger.warning("Could not set 0600 permissions on %s: %s", path, exc)


def save_session_atomic(cl: Client) -> None:
    """Atomically dump instagrapi settings (cookies + device UUIDs) to data/session.json."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    settings = cl.get_settings()
    serialized = json.dumps(settings, indent=4, ensure_ascii=False) + "\n"
    write_text_atomic(SESSION_FILE, serialized)
    restrict_file_permissions(SESSION_FILE)


@contextlib.contextmanager
def acquire_run_lock() -> Iterator[bool]:
    """Acquire a non-blocking process lock (data/checker.lock) to prevent overlapping runs."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock_handle = open(LOCK_FILE, "a+", encoding="utf-8")
    locked = False
    try:
        if fcntl is not None:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                locked = False
        elif msvcrt is not None:
            try:
                lock_handle.seek(0)
                msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError:
                locked = False
        else:
            locked = True

        if locked:
            lock_handle.seek(0)
            lock_handle.truncate()
            lock_handle.write(f"{os.getpid()}\n")
            lock_handle.flush()

        yield locked
    finally:
        if locked:
            try:
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:
                    lock_handle.seek(0)
                    msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        lock_handle.close()


def read_last_run() -> datetime | None:
    """Read the last successful run UTC timestamp from data/last_run.txt."""
    if not LAST_RUN_FILE.exists():
        return None
    raw = LAST_RUN_FILE.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        logger.warning("Invalid timestamp in %s (%r); ignoring cooldown.", LAST_RUN_FILE, raw)
        return None


def record_last_run(now: datetime | None = None) -> None:
    """Persist the current UTC ISO-8601 timestamp to data/last_run.txt."""
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    write_text_atomic(LAST_RUN_FILE, timestamp + "\n")


def check_cooldown(force: bool) -> bool:
    """Return True if the check should proceed, False if within the 20h cooldown."""
    if force:
        logger.info("--force passed; bypassing %d-hour cooldown check.", COOLDOWN_HOURS)
        return True

    last_run = read_last_run()
    if last_run is None:
        return True

    elapsed = datetime.now(timezone.utc) - last_run
    cooldown = timedelta(hours=COOLDOWN_HOURS)
    if elapsed < cooldown:
        remaining = cooldown - elapsed
        elapsed_hours = elapsed.total_seconds() / 3600
        remaining_hours = remaining.total_seconds() / 3600
        logger.info(
            "Last successful run was %.1f hours ago (< %dh cooldown; %.1fh remaining). "
            "Skipping check cleanly. Use --force to override.",
            elapsed_hours,
            COOLDOWN_HOURS,
            remaining_hours,
        )
        return False

    return True


def chunk_text(text: str, limit: int = 1900) -> list[str]:
    """Split long notification bodies into Discord-safe chunks."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current_len + len(line) > limit and current:
            chunks.append("".join(current).rstrip())
            current = []
            current_len = 0
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("".join(current).rstrip())
    return chunks


def send_notifications(
    config: Config,
    title: str,
    message: str,
    priority: str = "default",
    tags: list[str] | None = None,
) -> tuple[bool, list[str]]:
    """Send notifications to NTFY_URL and optional DISCORD_WEBHOOK_URL."""
    statuses: list[str] = []
    any_success = False
    any_configured = False

    if not is_placeholder(config.ntfy_url):
        any_configured = True
        headers = {
            "Title": title.encode("ascii", errors="ignore").decode("ascii").strip() or "IG Checker",
            "Priority": priority,
        }
        if tags:
            headers["Tags"] = ",".join(tags)
        if config.ntfy_token and not is_placeholder(config.ntfy_token):
            headers["Authorization"] = f"Bearer {config.ntfy_token}"
        try:
            response = requests.post(
                config.ntfy_url,
                data=message.encode("utf-8"),
                headers=headers,
                timeout=HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            any_success = True
            msg = f"ntfy notification sent (HTTP {response.status_code})"
            logger.info(msg)
            statuses.append(msg)
        except requests.RequestException as exc:
            msg = f"Failed to send ntfy notification: {exc}"
            logger.error(msg)
            statuses.append(msg)
    else:
        statuses.append("NTFY_URL is unset or still using placeholder value; skipped.")

    if config.discord_webhook_url and not is_placeholder(config.discord_webhook_url):
        any_configured = True
        full_body = f"**{title}**\n{message}"
        try:
            for chunk in chunk_text(full_body):
                response = requests.post(
                    config.discord_webhook_url,
                    json={"content": chunk},
                    timeout=HTTP_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
            any_success = True
            msg = "Discord webhook notification sent"
            logger.info(msg)
            statuses.append(msg)
        except requests.RequestException as exc:
            msg = f"Failed to send Discord webhook notification: {exc}"
            logger.error(msg)
            statuses.append(msg)

    if not any_configured:
        logger.warning("No notification channel configured (set NTFY_URL in .env).")

    return any_success, statuses


def _noninteractive_challenge_handler(username: str, choice: Any = None) -> str:
    """Prevent unattended background runs from blocking on stdin during challenges."""
    raise ig_exc.ChallengeRequired(
        f"Interactive verification challenge ({choice or 'unknown'}) required for @{username}. "
        "Run `checker.py --init-session` in an interactive terminal to complete it."
    )


def create_client(config: Config, allow_interactive: bool) -> Client:
    """Create a configured instagrapi Client following official best practices."""
    cl = Client()
    # Official best practice: mimic human timing between requests
    cl.delay_range = list(REQUEST_DELAY_RANGE)

    # Official best practice: set one stable proxy before any network calls
    if config.proxy_url and not is_placeholder(config.proxy_url):
        cl.set_proxy(config.proxy_url)

    if not allow_interactive:
        cl.challenge_code_handler = _noninteractive_challenge_handler

    return cl


def _login_with_optional_2fa(
    cl: Client,
    username: str,
    password: str,
    allow_interactive: bool,
) -> None:
    """Perform cl.login(), interactively prompting for 2FA code if running in a TTY."""
    try:
        cl.login(username, password)
    except ig_exc.TwoFactorRequired:
        if allow_interactive and sys.stdin.isatty():
            code = input(f"Enter 2FA verification code for @{username}: ").strip()
            cl.login(username, password, verification_code=code)
        else:
            raise


def login_user(config: Config, allow_interactive: bool = False) -> Client:
    """Authenticate using instagrapi's official best-practice session reuse pattern.

    1. Load persisted settings (cookies + device UUIDs) from data/session.json if present.
    2. Validate session via cl.login() + lightweight cl.get_timeline_feed() smoke test.
    3. If the session expired, clear auth state while preserving exact device UUIDs
       (set_uuids) and re-login cleanly without changing the device fingerprint.
    4. Persist refreshed settings atomically back to data/session.json.
    """
    if is_placeholder(config.bot_username) or is_placeholder(config.bot_password):
        raise ValueError(
            "BOT_USERNAME and BOT_PASSWORD must be configured in .env before logging in."
        )

    cl = create_client(config, allow_interactive=allow_interactive)
    login_via_session = False

    if SESSION_FILE.exists():
        logger.info("Loading saved instagrapi session from %s...", SESSION_FILE)
        cl.load_settings(SESSION_FILE)

        try:
            cl.login(config.bot_username, config.bot_password)

            # Smoke-test the loaded session using a lightweight authenticated call
            try:
                cl.get_timeline_feed()
            except ig_exc.LoginRequired:
                logger.warning(
                    "Saved session expired during timeline smoke test; "
                    "re-logging in while preserving device UUIDs..."
                )
                old_session = cl.get_settings()
                cl.set_settings({})
                if "uuids" in old_session:
                    cl.set_uuids(old_session["uuids"])
                _login_with_optional_2fa(
                    cl,
                    config.bot_username,
                    config.bot_password,
                    allow_interactive=allow_interactive,
                )

            save_session_atomic(cl)
            logger.info("Authenticated @%s using persisted session.", config.bot_username)
            login_via_session = True
        except (
            ig_exc.ChallengeRequired,
            ig_exc.CaptchaChallengeRequired,
            ig_exc.ChallengeSelfieCaptcha,
            ig_exc.RecaptchaChallengeForm,
            ig_exc.TwoFactorRequired,
            ig_exc.FeedbackRequired,
            ig_exc.PleaseWaitFewMinutes,
            ig_exc.RateLimitError,
            ig_exc.SentryBlock,
            ig_exc.AccountSuspended,
            ig_exc.BadPassword,
            ig_exc.BadCredentials,
            ig_exc.ProxyAddressIsBlocked,
        ):
            # Do NOT hammer Instagram with a second password login on security/rate blocks
            raise
        except Exception as exc:
            logger.warning(
                "Could not reuse saved session (%s); falling back to password login "
                "with preserved device UUIDs.",
                exc,
            )
            old_session = cl.get_settings()
            cl = create_client(config, allow_interactive=allow_interactive)
            if "uuids" in old_session:
                cl.set_uuids(old_session["uuids"])

    if not login_via_session:
        logger.info("Logging into burner account @%s via instagrapi...", config.bot_username)
        _login_with_optional_2fa(
            cl,
            config.bot_username,
            config.bot_password,
            allow_interactive=allow_interactive,
        )
        save_session_atomic(cl)
        logger.info("Authenticated @%s and saved session to %s.", config.bot_username, SESSION_FILE)

    return cl


def load_followers_state() -> dict[str, str] | None:
    """Load baseline followers mapping {user_id: username} from data/followers_state.json."""
    if not STATE_FILE.exists():
        return None
    raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Expected JSON object in {STATE_FILE}, got {type(raw).__name__}")
    return {str(uid): str(username) for uid, username in raw.items()}


def load_pending_unfollows() -> dict[str, dict[str, Any]]:
    """Load unverifiable private accounts awaiting 2-run confirmation from data/pending_unfollows.json."""
    if not PENDING_FILE.exists():
        return {}
    try:
        raw = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read %s (%s); starting with empty pending map.", PENDING_FILE, exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for uid, entry in raw.items():
        if isinstance(entry, dict) and isinstance(entry.get("misses"), int):
            result[str(uid)] = entry
    return result


def save_pending_unfollows(pending: dict[str, dict[str, Any]]) -> None:
    """Persist pending unfollows atomically, removing the file when empty."""
    if pending:
        write_json_atomic(PENDING_FILE, pending)
    elif PENDING_FILE.exists():
        try:
            PENDING_FILE.unlink()
        except OSError:
            write_json_atomic(PENDING_FILE, {})


def diff_followers(previous: dict[str, str], current: dict[str, str]) -> DiffResult:
    """Compare follower snapshots strictly by user_id to prevent rename false positives."""
    previous_ids = set(previous.keys())
    current_ids = set(current.keys())

    unfollowed_ids = previous_ids - current_ids
    new_ids = current_ids - previous_ids
    retained_ids = previous_ids & current_ids

    unfollowed_entries = sorted(
        ((uid, previous[uid]) for uid in unfollowed_ids),
        key=lambda item: item[1].lower(),
    )
    unfollowed = [handle for _, handle in unfollowed_entries]
    new_followers = sorted((current[uid] for uid in new_ids), key=str.lower)
    renamed = sorted(
        (
            (uid, previous[uid], current[uid])
            for uid in retained_ids
            if previous[uid] != current[uid]
        ),
        key=lambda item: item[1].lower(),
    )
    return DiffResult(
        unfollowed=unfollowed,
        unfollowed_entries=unfollowed_entries,
        new_followers=new_followers,
        renamed=renamed,
    )


def fetch_current_followers(cl: Client, target_username: str) -> tuple[str, dict[str, str], int]:
    """Fetch current followers of target_username as (target_user_id, {user_id: username}, expected_count).

    Instagram's ranked v1 followers pagination frequently omits ~10-15% of users on a single
    pass. When v1 returns fewer accounts than the profile's reported follower_count, we also
    merge results from the private GraphQL followers endpoint to maximize completeness.
    """
    if is_placeholder(target_username):
        raise ValueError("TARGET_USERNAME must be configured in .env.")

    logger.info("Fetching profile metadata for @%s...", target_username)
    target_user = cl.user_info_by_username(target_username, use_cache=False)
    target_user_id = str(target_user.pk)
    expected_count = int(target_user.follower_count or 0)

    logger.info(
        "Fetching followers list for @%s (ID %s, reported count: %d)...",
        target_username,
        target_user_id,
        expected_count,
    )
    v1_users = cl.user_followers_v1(target_user_id, amount=0)
    followers: dict[str, str] = {str(user.pk): str(user.username) for user in v1_users}
    logger.info("Fetched %d/%d followers via v1 endpoint.", len(followers), expected_count)

    if (
        expected_count > 0
        and len(followers) < expected_count
        and hasattr(cl, "user_followers_private_gql")
    ):
        logger.info(
            "v1 list is short (%d/%d); merging private GraphQL followers list for completeness...",
            len(followers),
            expected_count,
        )
        try:
            gql_users = cl.user_followers_private_gql(target_user_id, amount=0)
            gql_unique = {str(user.pk): str(user.username) for user in gql_users}
            before = len(followers)
            followers.update(gql_unique)
            added = len(followers) - before
            logger.info(
                "Private GraphQL returned %d unique users (+%d newly recovered; total now %d/%d).",
                len(gql_unique),
                added,
                len(followers),
                expected_count,
            )
        except ig_exc.ClientError as exc:
            logger.warning(
                "Private GraphQL merge skipped (%s); continuing with %d v1 followers.",
                exc,
                len(followers),
            )

    logger.info("Fetched %d unique followers for @%s.", len(followers), target_username)
    return target_user_id, followers, expected_count


def _search_follower_by_id(cl: Client, target_user_id: str, uid: str, query: str) -> str | None:
    """Return the matched username if uid appears in search_followers(target_user_id, query)."""
    matches = cl.search_followers(target_user_id, query)
    for user in matches:
        if str(user.pk) == uid:
            return str(user.username)
    return None


def _is_in_user_following(
    cl: Client,
    candidate_uid: str,
    target_user_id: str,
    target_username: str,
) -> bool:
    """Return True if target_user_id appears in candidate_uid's following list."""
    matches = cl.search_following(candidate_uid, target_username)
    return any(str(user.pk) == target_user_id for user in matches)


def verify_candidate_unfollowers(
    cl: Client,
    target_user_id: str,
    target_username: str,
    previous_followers: dict[str, str],
    current_followers: dict[str, str],
    expected_count: int,
    pending_unfollows: dict[str, dict[str, Any]] | None = None,
) -> VerificationResult:
    """Double-check missing follower IDs bidirectionally to eliminate Instagram API omissions.

    Verification layers for each candidate missing from the paginated follower list:
      1. Forward search: search_followers(target_user_id, old_username)
      2. Handle lookup + forward search if renamed: user_info_v1(uid) -> search_followers(new_username)
         (also detects deleted/deactivated accounts via UserNotFound)
      3. Reverse search: search_following(uid, target_username) — catches accounts that Instagram
         omits from the target's followers/ endpoint AND forward search_followers index.
      4. 2-run confirmation for unverifiable private accounts: if a private account cannot be
         reverse-checked (PrivateAccount), require PRIVATE_UNVERIFIED_MISS_THRESHOLD consecutive
         misses before confirming an unfollow.

    Returns a VerificationResult with restored followers, deactivated IDs, and updated pending map.
    """
    updated_pending: dict[str, dict[str, Any]] = {
        uid: dict(entry)
        for uid, entry in (pending_unfollows or {}).items()
        if uid not in current_followers and uid in previous_followers
    }

    candidate_ids = sorted(
        set(previous_followers.keys()) - set(current_followers.keys()),
        key=lambda uid: previous_followers[uid].lower(),
    )
    if not candidate_ids:
        return VerificationResult(
            followers=dict(current_followers),
            deactivated_ids=set(),
            pending_unfollows={},
        )

    # Guard against severe API truncation (e.g. >25% missing while reported follower_count is intact)
    max_verify = max(30, int(len(previous_followers) * 0.25))
    if (
        len(previous_followers) >= 20
        and len(candidate_ids) > max_verify
        and expected_count > 0
        and len(current_followers) < expected_count * 0.75
    ):
        raise ig_exc.ClientError(
            f"Fetched only {len(current_followers)}/{expected_count} followers "
            f"({len(candidate_ids)} missing vs baseline of {len(previous_followers)}). "
            "Instagram likely truncated pagination; aborting to avoid false unfollow alerts."
        )

    logger.info(
        "Verifying %d candidate unfollower(s) via forward & reverse search to rule out Instagram omissions...",
        len(candidate_ids),
    )
    verified_followers = dict(current_followers)
    deactivated_ids: set[str] = set()
    restored_count = 0
    deferred_count = 0

    fatal_errors = (
        ig_exc.LoginRequired,
        ig_exc.ClientLoginRequired,
        ig_exc.ChallengeRequired,
        ig_exc.FeedbackRequired,
        ig_exc.PleaseWaitFewMinutes,
        ig_exc.RateLimitError,
    )

    for uid in candidate_ids:
        old_username = previous_followers[uid]
        try:
            # Layer 1: Forward search on target's followers list by old handle
            matched_username = _search_follower_by_id(cl, target_user_id, uid, old_username)
            if matched_username is not None:
                verified_followers[uid] = matched_username
                updated_pending.pop(uid, None)
                restored_count += 1
                logger.info(
                    "False unfollow prevented: @%s (ID %s) is STILL following (omitted by pagination).",
                    matched_username,
                    uid,
                )
                continue

            # Layer 2: Look up current profile info (detects deleted/deactivated accounts & renames)
            latest_username = old_username
            user_info_failed = False
            try:
                user_info = cl.user_info_v1(uid)
                fetched_handle = str(user_info.username or "").strip()
                if fetched_handle:
                    latest_username = fetched_handle
            except ig_exc.UserNotFound:
                # Account deleted or temporarily deactivated -> confirmed gone immediately
                deactivated_ids.add(uid)
                updated_pending.pop(uid, None)
                logger.info(
                    "Account @%s (ID %s) not found (deactivated or deleted).",
                    old_username,
                    uid,
                )
                continue
            except fatal_errors:
                raise
            except ig_exc.ClientError as exc:
                user_info_failed = True
                logger.warning(
                    "Could not look up current handle for ID %s (@%s): %s",
                    uid,
                    old_username,
                    exc,
                )

            if latest_username.lower() != old_username.lower():
                matched_renamed = _search_follower_by_id(cl, target_user_id, uid, latest_username)
                if matched_renamed is not None:
                    verified_followers[uid] = matched_renamed
                    updated_pending.pop(uid, None)
                    restored_count += 1
                    logger.info(
                        "False unfollow prevented (renamed + omitted by pagination): "
                        "ID %s @%s -> @%s is STILL following.",
                        uid,
                        old_username,
                        matched_renamed,
                    )
                    continue

            # Layer 3: Reverse search on the candidate's own following list for target_username
            try:
                if _is_in_user_following(cl, uid, target_user_id, target_username):
                    verified_followers[uid] = latest_username
                    updated_pending.pop(uid, None)
                    restored_count += 1
                    logger.info(
                        "False unfollow prevented (verified via reverse search_following): "
                        "@%s (ID %s) STILL follows @%s.",
                        latest_username,
                        uid,
                        target_username,
                    )
                    continue
                # Public (or burner-followed) account cleanly returned [] -> confirmed unfollow
                updated_pending.pop(uid, None)
            except (ig_exc.PrivateAccount, ig_exc.ClientForbiddenError):
                # Cannot inspect private account's following list.
                if user_info_failed:
                    # We could not even verify if they renamed -> preserve conservatively
                    verified_followers[uid] = old_username
                    restored_count += 1
                    logger.warning(
                        "Preserving private account @%s (ID %s) because handle lookup failed transiently.",
                        old_username,
                        uid,
                    )
                    continue

                # Layer 4: Require 2 consecutive misses for unverifiable private accounts
                prev_entry = updated_pending.get(uid)
                misses = (int(prev_entry["misses"]) + 1) if prev_entry else 1
                if misses < PRIVATE_UNVERIFIED_MISS_THRESHOLD:
                    updated_pending[uid] = {
                        "username": latest_username,
                        "misses": misses,
                        "first_missed": (
                            prev_entry["first_missed"]
                            if prev_entry and "first_missed" in prev_entry
                            else datetime.now(timezone.utc).isoformat()
                        ),
                    }
                    verified_followers[uid] = latest_username
                    deferred_count += 1
                    logger.info(
                        "Private account @%s (ID %s) could not be reverse-verified (miss %d/%d); "
                        "deferring unfollow alert until confirmed on next run.",
                        latest_username,
                        uid,
                        misses,
                        PRIVATE_UNVERIFIED_MISS_THRESHOLD,
                    )
                    continue

                # Missed on PRIVATE_UNVERIFIED_MISS_THRESHOLD consecutive runs -> confirmed unfollow
                updated_pending.pop(uid, None)
                logger.info(
                    "Private account @%s (ID %s) missed %d consecutive runs; confirming unfollow.",
                    latest_username,
                    uid,
                    misses,
                )
            except ig_exc.UserNotFound:
                deactivated_ids.add(uid)
                updated_pending.pop(uid, None)
            except fatal_errors:
                raise
            except ig_exc.ClientError as exc:
                # Transient reverse-lookup error: preserve conservatively to avoid false alert
                verified_followers[uid] = latest_username
                restored_count += 1
                logger.warning(
                    "Reverse search_following failed transiently for @%s (ID %s): %s; "
                    "preserving in state to avoid false alert.",
                    latest_username,
                    uid,
                    exc,
                )

        except fatal_errors:
            raise
        except ig_exc.ClientError as exc:
            # Conservative fallback: on transient lookup errors, preserve user to avoid false alert
            verified_followers[uid] = old_username
            restored_count += 1
            logger.warning(
                "Verification lookup failed for @%s (ID %s): %s; preserving in state to avoid false alert.",
                old_username,
                uid,
                exc,
            )

    if restored_count:
        logger.info(
            "Restored %d still-following account(s) omitted by Instagram.",
            restored_count,
        )
    if deferred_count:
        logger.info(
            "Deferred %d unverifiable private account(s) pending second consecutive miss.",
            deferred_count,
        )
    return VerificationResult(
        followers=verified_followers,
        deactivated_ids=deactivated_ids,
        pending_unfollows=updated_pending,
    )


def classify_instagram_error(exc: Exception) -> tuple[str, str]:
    """Return (alert_title, actionable_message) for instagrapi exceptions."""
    detail = str(exc).strip() or repr(exc)
    lower_detail = detail.lower()

    if isinstance(
        exc,
        (
            ig_exc.ChallengeRequired,
            ig_exc.CaptchaChallengeRequired,
            ig_exc.ChallengeSelfieCaptcha,
            ig_exc.RecaptchaChallengeForm,
            ig_exc.ChallengeError,
            ig_exc.SelectContactPointRecoveryForm,
            ig_exc.SubmitPhoneNumberForm,
            ig_exc.AccountContactPointRequired,
        ),
    ) or "challenge" in lower_detail or "checkpoint" in lower_detail:
        return (
            "IG Checker Alert: Checkpoint / Challenge Required",
            "Instagram triggered a security challenge or captcha on your burner account.\n"
            "Action needed: Log into the burner account in the Instagram app/browser to clear "
            "any verification prompt, then run `python checker.py --init-session` interactively.\n\n"
            f"Details: {detail}",
        )

    if isinstance(exc, ig_exc.TwoFactorRequired):
        return (
            "IG Checker Alert: Two-Factor Auth Required",
            "The burner account requires a 2FA verification code.\n"
            "Action needed: Run `python checker.py --init-session` interactively in a terminal.\n\n"
            f"Details: {detail}",
        )

    if isinstance(exc, (ig_exc.FeedbackRequired,)) or "feedback_required" in lower_detail:
        return (
            "IG Checker Alert: Temporary Soft Block (feedback_required)",
            "Instagram temporarily soft-blocked actions on your burner account or IP.\n"
            "Action needed: Open Instagram in your normal browser/app, dismiss any automated-behavior "
            "notice, disable VPNs if active, and wait 24-48 hours before retrying.\n\n"
            f"Details: {detail}",
        )

    if isinstance(exc, (ig_exc.PleaseWaitFewMinutes, ig_exc.RateLimitError, ig_exc.ClientThrottledError)):
        return (
            "IG Checker Alert: Rate Limited",
            "Instagram rate-limited the burner account.\n"
            "Action needed: Wait 24 hours before retrying and avoid running --force repeatedly.\n\n"
            f"Details: {detail}",
        )

    if isinstance(exc, (ig_exc.SentryBlock, ig_exc.AccountSuspended)):
        return (
            "IG Checker Alert: Account Restricted / Suspended",
            "Instagram restricted or suspended the burner account.\n"
            "Action needed: Check the burner account status in the Instagram app/browser.\n\n"
            f"Details: {detail}",
        )

    if isinstance(exc, (ig_exc.BadPassword, ig_exc.BadCredentials)):
        return (
            "IG Checker Alert: Invalid Burner Credentials",
            "Instagram rejected BOT_USERNAME / BOT_PASSWORD from .env.\n"
            "Action needed: Verify credentials in .env and run `python checker.py --init-session`.\n\n"
            f"Details: {detail}",
        )

    if isinstance(exc, (ig_exc.LoginRequired, ig_exc.ClientLoginRequired, ig_exc.ReloginAttemptExceeded)):
        return (
            "IG Checker Alert: Session Expired (Login Required)",
            "The saved instagrapi session is no longer valid.\n"
            "Action needed: Run `python checker.py --init-session` to refresh data/session.json.\n\n"
            f"Details: {detail}",
        )

    if isinstance(exc, ig_exc.ProxyAddressIsBlocked):
        return (
            "IG Checker Alert: Proxy Address Blocked",
            "Instagram blocked the IP/proxy configured in PROXY_URL.\n"
            "Action needed: Switch to a clean residential IP/proxy and retry.\n\n"
            f"Details: {detail}",
        )

    if isinstance(exc, ig_exc.PrivateAccount):
        return (
            "IG Checker Alert: Target Profile Is Private",
            "The burner account cannot view followers because the target profile is private "
            "and not followed by the burner account.\n\n"
            f"Details: {detail}",
        )

    if isinstance(exc, ig_exc.UserNotFound):
        return (
            "IG Checker Alert: Target Profile Not Found",
            "TARGET_USERNAME could not be found on Instagram.\n"
            "Action needed: Verify TARGET_USERNAME in .env.\n\n"
            f"Details: {detail}",
        )

    if isinstance(exc, (ig_exc.ClientConnectionError, ig_exc.ClientRequestTimeout, ig_exc.ClientError)):
        return (
            "IG Checker Alert: Instagram Connection / API Error",
            "instagrapi encountered a connection or API error while talking to Instagram.\n\n"
            f"Details: {detail}",
        )

    return (
        f"IG Checker Alert: {type(exc).__name__}",
        f"An unexpected error occurred:\n\n{detail}",
    )


def handle_init_session(config: Config) -> int:
    """Handle the --init-session interactive login & session persistence workflow."""
    with acquire_run_lock() as acquired:
        if not acquired:
            logger.warning("Another checker.py instance is already running; aborting --init-session.")
            return 1
        try:
            login_user(config, allow_interactive=True)
            logger.info(
                "Session initialized and verified. Regular runs will reuse %s.",
                SESSION_FILE,
            )
            return 0
        except ig_exc.ClientError as exc:
            title, body = classify_instagram_error(exc)
            logger.error("%s | %s", title, exc)
            send_notifications(config, title, body, priority="high", tags=["warning"])
            return 1
        except Exception as exc:
            logger.exception("Failed to initialize session: %s", exc)
            return 1


def handle_dry_run(config: Config) -> int:
    """Validate config, writable paths, diff logic, and notifications without hitting Instagram."""
    logger.info("=== Starting --dry-run verification (no Instagram requests will be made) ===")

    logger.info(".env file present: %s (%s)", ENV_FILE.exists(), ENV_FILE)
    config_checks = {
        "BOT_USERNAME": not is_placeholder(config.bot_username),
        "BOT_PASSWORD": not is_placeholder(config.bot_password),
        "TARGET_USERNAME": not is_placeholder(config.target_username),
        "NTFY_URL": not is_placeholder(config.ntfy_url),
        "NTFY_TOKEN (optional)": bool(config.ntfy_token and not is_placeholder(config.ntfy_token)),
        "DISCORD_WEBHOOK_URL (optional)": bool(
            config.discord_webhook_url and not is_placeholder(config.discord_webhook_url)
        ),
        "PROXY_URL (optional)": bool(config.proxy_url and not is_placeholder(config.proxy_url)),
    }
    for key, ok in config_checks.items():
        status = "configured" if ok else "NOT configured (placeholder or blank)"
        logger.info("  Config %-32s: %s", key, status)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    probe_file = DATA_DIR / ".dry_run_probe"
    try:
        write_text_atomic(probe_file, "ok\n")
        probe_file.unlink()
        logger.info("  Data directory writable          : YES (%s)", DATA_DIR)
    except OSError as exc:
        logger.error("  Data directory writable          : NO (%s)", exc)
        return 1

    # Verify instagrapi client initialization locally (no network calls)
    cl = create_client(config, allow_interactive=False)
    logger.info(
        "  instagrapi client                : OK (delay_range=%s)",
        cl.delay_range,
    )
    logger.info("  Log file                         : %s", LOG_FILE)
    logger.info("  Session file exists              : %s (%s)", SESSION_FILE.exists(), SESSION_FILE)

    state = load_followers_state()
    if state is None:
        logger.info("  Followers baseline               : none yet (%s)", STATE_FILE)
    else:
        logger.info("  Followers baseline               : %d followers (%s)", len(state), STATE_FILE)

    pending = load_pending_unfollows()
    logger.info("  Pending private unfollows        : %d", len(pending))

    last_run = read_last_run()
    if last_run is None:
        logger.info("  Last successful run              : never (%s)", LAST_RUN_FILE)
    else:
        elapsed_h = (datetime.now(timezone.utc) - last_run).total_seconds() / 3600
        logger.info(
            "  Last successful run              : %s (%.1fh ago; cooldown=%dh)",
            last_run.isoformat(),
            elapsed_h,
            COOLDOWN_HOURS,
        )

    # Verify ID-based diffing logic (rename is updated without triggering false unfollow)
    sample_prev = {"1001": "alice_old", "1002": "bob", "1003": "charlie"}
    sample_curr = {"1001": "alice_new", "1002": "bob", "1004": "diana"}
    sample_diff = diff_followers(sample_prev, sample_curr)
    assert sample_diff.unfollowed == ["charlie"], f"Unexpected unfollowed: {sample_diff.unfollowed}"
    assert sample_diff.new_followers == ["diana"], f"Unexpected new: {sample_diff.new_followers}"
    assert sample_diff.renamed == [("1001", "alice_old", "alice_new")], (
        f"Unexpected renamed: {sample_diff.renamed}"
    )
    logger.info("  ID-based diff self-test          : PASSED (rename ignored, unfollow detected)")

    target_label = config.target_username if not is_placeholder(config.target_username) else "target"
    test_title = "IG Unfollow Checker - Dry Run Test"
    test_body = (
        f"Dry run succeeded on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (instagrapi).\n"
        f"Target: @{target_label}\n"
        f"Session cached: {SESSION_FILE.exists()}\n"
        "No requests were sent to Instagram."
    )

    if is_placeholder(config.ntfy_url) and is_placeholder(config.discord_webhook_url):
        logger.warning(
            "  Notification delivery test       : SKIPPED live POST "
            "(NTFY_URL is still a placeholder in .env)."
        )
    else:
        ok, statuses = send_notifications(
            config,
            title=test_title,
            message=test_body,
            priority="default",
            tags=["white_check_mark"],
        )
        for line in statuses:
            logger.info("  Notification status              : %s", line)
        if not ok:
            logger.error("Dry run notification delivery failed.")
            return 1

    logger.info("=== Dry run completed successfully ===")
    return 0


def run_check(config: Config, force: bool) -> int:
    """Run the daily Instagram follower check."""
    with acquire_run_lock() as acquired:
        if not acquired:
            logger.info("Another checker.py instance is already running; exiting cleanly.")
            return 0

        if not check_cooldown(force=force):
            return 0

        delay = random.uniform(STARTUP_DELAY_MIN_SECONDS, STARTUP_DELAY_MAX_SECONDS)
        logger.info(
            "Applying randomized startup delay of %.1f seconds before contacting Instagram...",
            delay,
        )
        time.sleep(delay)

        try:
            cl = login_user(config, allow_interactive=sys.stdin.isatty())
            target_user_id, current_followers, expected_count = fetch_current_followers(
                cl, config.target_username
            )

            previous_followers = load_followers_state()

            # Safeguard against silent empty responses when Instagram glitches
            if previous_followers and not current_followers and expected_count > 0:
                raise ig_exc.ClientError(
                    f"Instagram returned 0 followers even though @{config.target_username} "
                    f"reports {expected_count} followers. Aborting to prevent false unfollow alerts."
                )

            if previous_followers is None:
                # Persist any refreshed session cookies/settings after a healthy run
                save_session_atomic(cl)
                write_json_atomic(STATE_FILE, current_followers)
                save_pending_unfollows({})
                record_last_run()
                logger.info(
                    "Initialized baseline for @%s with %d followers in %s.",
                    config.target_username,
                    len(current_followers),
                    STATE_FILE,
                )
                return 0

            # Double-check every candidate unfollower bidirectionally (forward search_followers
            # + reverse search_following + 2-run confirmation for unverifiable private accounts).
            verification = verify_candidate_unfollowers(
                cl,
                target_user_id=target_user_id,
                target_username=config.target_username,
                previous_followers=previous_followers,
                current_followers=current_followers,
                expected_count=expected_count,
                pending_unfollows=load_pending_unfollows(),
            )
            current_followers = verification.followers

            # Persist any refreshed session cookies/settings after a healthy run
            save_session_atomic(cl)

            diff = diff_followers(previous_followers, current_followers)

            for uid, old_handle, new_handle in diff.renamed:
                logger.info(
                    "Follower changed handle (ID %s): @%s -> @%s (updated without unfollow alert)",
                    uid,
                    old_handle,
                    new_handle,
                )

            write_json_atomic(STATE_FILE, current_followers)
            save_pending_unfollows(verification.pending_unfollows)
            record_last_run()

            if diff.unfollowed_entries:
                formatted_lines = [
                    (
                        f"- @{handle} (deactivated/deleted)"
                        if uid in verification.deactivated_ids
                        else f"- @{handle}"
                    )
                    for uid, handle in diff.unfollowed_entries
                ]
                handles_list = "\n".join(formatted_lines)
                log_summary = ", ".join(
                    (
                        f"@{handle} [deactivated/deleted]"
                        if uid in verification.deactivated_ids
                        else f"@{handle}"
                    )
                    for uid, handle in diff.unfollowed_entries
                )
                logger.info(
                    "Detected %d unfollower(s) on @%s: %s",
                    len(diff.unfollowed_entries),
                    config.target_username,
                    log_summary,
                )
                title = (
                    f"IG Unfollow Alert (@{config.target_username}): "
                    f"{len(diff.unfollowed_entries)} unfollowed"
                )
                body = (
                    f"Unfollowers detected ({len(diff.unfollowed_entries)}):\n"
                    f"{handles_list}\n\n"
                    f"Current followers: {len(current_followers)} "
                    f"(+{len(diff.new_followers)} new, {len(diff.renamed)} renamed)"
                )
                send_notifications(config, title, body, priority="default", tags=["eyes"])
            else:
                logger.info(
                    "No unfollowers detected for @%s (total: %d, new: %d, renamed: %d, pending: %d).",
                    config.target_username,
                    len(current_followers),
                    len(diff.new_followers),
                    len(diff.renamed),
                    len(verification.pending_unfollows),
                )

            return 0

        except ig_exc.ClientError as exc:
            title, body = classify_instagram_error(exc)
            logger.error("%s | %s", title, exc)
            send_notifications(config, title, body, priority="high", tags=["warning"])
            return 1
        except Exception as exc:
            logger.exception("Unexpected error during follower check: %s", exc)
            send_notifications(
                config,
                title="IG Checker Alert: Unexpected Error",
                message=f"An unexpected error occurred in checker.py:\n\n{type(exc).__name__}: {exc}",
                priority="high",
                tags=["warning"],
            )
            return 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Daily Instagram unfollow checker using instagrapi with persistent session settings."
    )
    parser.add_argument(
        "--init-session",
        action="store_true",
        help="Log into BOT_USERNAME and save persistent device/session settings to data/session.json.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=f"Bypass the {COOLDOWN_HOURS}-hour cooldown check in data/last_run.txt.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Verify config, paths, instagrapi client, diff logic, and notifications without contacting Instagram.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = parse_args(argv)
    config = load_config()

    if args.dry_run:
        return handle_dry_run(config)
    if args.init_session:
        return handle_init_session(config)
    return run_check(config, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
