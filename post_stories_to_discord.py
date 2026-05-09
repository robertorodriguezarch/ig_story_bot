from __future__ import annotations

import json
import os
import time
import traceback
import requests

from dotenv import load_dotenv
from instagrapi import Client
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

SESSION_FILE = BASE_DIR / "session.json"
SEEN_FILE = BASE_DIR / "seen_story_ids.json"
DOWNLOADS_DIR = BASE_DIR / "downloads"
ALERT_STATE_FILE = BASE_DIR / "alert_state.json"
TRIGGER_FILE = BASE_DIR / "trigger_story_now"
ALERT_COOLDOWN_SECONDS = 60 * 60  # 1 hour

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", 900))
POST_EXISTING_ON_FIRST_RUN = (
    os.getenv("POST_EXISTING_ON_FIRST_RUN", "false").lower() == "true"
)
RUN_ONCE = os.getenv("RUN_ONCE", "false").lower() == "true"

print(
    f"Config loaded: RUN_ONCE={RUN_ONCE},"
    f"POLL_INTERVAL_SECONDS={POLL_INTERVAL_SECONDS},"
    f"POST_EXISTING_ON_FIRST_RUN={POST_EXISTING_ON_FIRST_RUN}"
)

DOWNLOADS_DIR.mkdir(exist_ok=True)


class HardInstagramStop(Exception):
    """Raised when Instagram requires human intervention."""

    pass


def build_client() -> Client:
    cl = Client()
    cl.delay_range = [2, 5]
    return cl


def login_with_sessionid(sessionid: str) -> Client:
    cl = build_client()
    cl.login_by_sessionid(sessionid)
    cl.dump_settings(str(SESSION_FILE))
    return cl


def load_seen_ids() -> set[str]:
    if not SEEN_FILE.exists():
        return set()
    try:
        data = json.loads(SEEN_FILE.read_text())
        return {str(x) for x in data}
    except Exception:
        return set()


def save_seen_ids(seen_ids: set[str]) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen_ids), indent=2))


def download_file(url: str, out_path: Path) -> None:
    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with out_path.open("wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)


def format_story_time(dt: datetime) -> str:
    # Convert to Eastern Time (adjust if needed)
    local_dt = dt.astimezone(ZoneInfo("America/New_York"))
    return local_dt.strftime("%m/%d/%y %-I:%M %p")


def post_story_to_discord(
    webhook_url: str,
    target_username: str,
    taken_at: datetime,
    media_path: Path,
    is_video: bool,
    role_id: str | None,
    avatar_url: str | None,
) -> None:
    footer_text = format_story_time(taken_at)

    embed = {
        "author": {
            "name": f"@{target_username}",
            "url": f"https://instagram.com/{target_username}",
        },
        "footer": {"text": footer_text},
        "color": 0xE1306C,  # Instagram pink
    }

    if is_video:
        pass
    else:
        embed["image"] = {"url": f"attachment://{media_path.name}"}

    payload = {
        "username": f"@{target_username}",
        "content": f"<@&{role_id}>" if role_id else "",
        "allowed_mentions": {"roles": [role_id]} if role_id else {},
        "embeds": [embed],
    }

    if avatar_url:
        payload["avatar_url"] = avatar_url

    with media_path.open("rb") as f:
        files = {
            "file": (media_path.name, f),
            "payload_json": (None, json.dumps(payload)),
        }
        response = requests.post(webhook_url, files=files, timeout=60)
        response.raise_for_status()


def sleep_with_manual_trigger(seconds: int) -> None:
    """
    Sleeps in short chunks so the bot can be manually woken up without
    restarting the service.

    To trigger an immediate story check: touch /home/pi/ig_story_bot/trigger_story_now
    """

    print(f"Sleeping for up to {seconds} seconds...")

    slept = 0
    check_every = 5

    while slept < seconds:
        if TRIGGER_FILE.exists():
            try:
                TRIGGER_FILE.unlink()
            except FileNotFoundError:
                pass

            print("Manual trigger detected. Waking up early for a story check.")
            return

        time.sleep(check_every)
        slept += check_every


def run_loop() -> None:

    sessionid = os.getenv("IG_SESSIONID")
    target_username = os.getenv("TARGET_USERNAME")
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    role_id = os.getenv("DISCORD_IG_STORY_ROLE_ID")
    alert_webhook_url = os.getenv("DISCORD_ALERT_WEBHOOK_URL")
    avatar_url = os.getenv("DISCORD_AVATAR_URL")

    print(f"Session ID loaded: {bool(sessionid)}")
    print(f"Session ID preview: {sessionid[:12]}..." if sessionid else "No session ID")

    if not sessionid or not target_username or not webhook_url:
        raise RuntimeError(
            "Missing IG_SESSIONID, TARGET_USERNAME, or DISCORD_WEBHOOK_URL in .env"
        )

    # LOGIN ONCE HERE
    try:
        cl = login_with_sessionid(sessionid)
        me = cl.account_info()
        print(f"Logged in as: @{me.username}")
    except Exception as exc:
        error_text = str(exc)
        print(f"Login failed: {type(exc).__name__}: {error_text}")
        traceback.print_exc()

        send_alert(
            alert_webhook_url,
            f"🚨 IG bot failed to login.\nError: `{error_text[:1500]}`",
            "ig_login_failure",
        )

        raise HardInstagramStop("Login failed; human intervention required.")

    #  Loop only does fetching
    try:
        while True:
            try:
                print(
                    f"\n--- New polling cycle at {datetime.now().strftime('%Y-%m-%d %I:%M:%S %p')} ---"
                )
                run_once(
                    cl,
                    target_username,
                    webhook_url,
                    role_id,
                    alert_webhook_url,
                    avatar_url,
                )

            except HardInstagramStop as exc:
                print(f"Hard stop: {exc}")
                raise

            except Exception as exc:
                print(f"Loop error: {exc}")

            if RUN_ONCE:
                print("RUN_ONCE=true, exiting after one polling cycle.")
                return

            sleep_with_manual_trigger(POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\nStopped by user.")


def run_once(
    cl: Client,
    target_username: str,
    webhook_url: str,
    role_id: str | None,
    alert_webhook_url: str | None,
    avatar_url: str | None,
) -> None:
    try:
        user = cl.user_info_by_username_v1(target_username)
        stories = cl.user_stories(user.pk)

        clear_alert("ig_challenge")
        clear_alert("ig_login_required")
        clear_alert("ig_generic_fetch_error")
        clear_alert("ig_rate_limited")
        clear_alert("ig_bad_response")
    except Exception as exc:
        error_text = str(exc)
        print(f"Failed to fetch stories for @{target_username}: {error_text}")

        lowered = error_text.lower()

        if "429" in lowered or "too many 429" in lowered:
            send_alert(
                alert_webhook_url,
                f"⚠️ IG bot is being rate-limited for @{target_username}.\nError: `{error_text[:1500]}`",
                "ig_rate_limited",
            )
        elif (
            "challengeresolve" in lowered
            or "challenge" in lowered
            or "checkpoint" in lowered
            or "manual verification required" in lowered
            or "ufac" in lowered
            or "exceeded 30 redirects" in lowered
            or "too many redirects" in lowered
        ):
            send_alert(
                alert_webhook_url,
                f"🚨 IG bot hit a challenge/checkpoint for @{target_username}. Bot stopped.\nError: `{error_text[:1500]}`",
                "ig_challenge",
            )
            raise HardInstagramStop("Challenge/checkpoint detected; bot stopped.")
        elif "login_required" in lowered:
            send_alert(
                alert_webhook_url,
                f"🚨 IG bot session is no longer valid for @{target_username}. Bot stopped.\nError: `{error_text[:1500]}`",
                "ig_login_required",
            )
            raise HardInstagramStop("login_required detected; bot stopped.")
        elif "expecting value" in lowered or "jsondecodeerror" in lowered:
            send_alert(
                alert_webhook_url,
                f"⚠️ IG bot received non-JSON/empty response for @{target_username},\nError: '{error_text[:1500]}'",
                "ig_bad_response",
            )
        else:
            send_alert(
                alert_webhook_url,
                f"⚠️ IG bot fetch failed for @{target_username}.\nError: `{error_text[:1500]}`",
                "ig_generic_fetch_error",
            )

        return

    if not stories:
        print("No active stories right now.")
        return

    if not SEEN_FILE.exists():
        if POST_EXISTING_ON_FIRST_RUN:
            print("First run: posting existing stories.")
            seen_ids = set()
        else:
            initialize_seen_ids_from_current_stories(stories)
            print(
                "First run detected. Current stories marked as seen; waiting for new ones."
            )
            return
    else:
        seen_ids = load_seen_ids()

    new_story_count = 0

    for story in stories:
        story_pk = str(story.pk)

        if story_pk in seen_ids:
            continue

        is_video = story.media_type == 2
        media_url = story.video_url if is_video else story.thumbnail_url

        if not media_url:
            print(f"Skipping {story_pk}: no media URL")
            continue

        ext = ".mp4" if is_video else ".jpg"
        media_path = DOWNLOADS_DIR / f"{story_pk}{ext}"

        try:
            download_file(media_url, media_path)
            post_story_to_discord(
                webhook_url=webhook_url,
                target_username=target_username,
                taken_at=story.taken_at,
                media_path=media_path,
                is_video=is_video,
                role_id=role_id,
                avatar_url=avatar_url,
            )
            seen_ids.add(story_pk)
            new_story_count += 1
            print(f"Posted story {story_pk}")
            time.sleep(2)
        except Exception as exc:
            print(f"Failed to process {story_pk}: {exc}")

    save_seen_ids(seen_ids)
    print(f"Done. New stories posted: {new_story_count}")


def initialize_seen_ids_from_current_stories(stories) -> None:
    seen_ids = {str(story.pk) for story in stories}
    save_seen_ids(seen_ids)
    print(f"Initialized seen_story_ids.json with {len(seen_ids)} current stories.")


def load_alert_state() -> dict:
    if not ALERT_STATE_FILE.exists():
        return {}
    try:
        return json.loads(ALERT_STATE_FILE.read_text())
    except Exception:
        return {}


def save_alert_state(state: dict) -> None:
    ALERT_STATE_FILE.write_text(json.dumps(state, indent=2))


def should_send_alert(alert_key: str) -> bool:
    state = load_alert_state()
    last_sent = state.get(alert_key, 0)
    now = time.time()

    if now - last_sent >= ALERT_COOLDOWN_SECONDS:
        state[alert_key] = now
        save_alert_state(state)
        return True

    return False


def clear_alert(alert_key: str) -> None:
    state = load_alert_state()
    if alert_key in state:
        del state[alert_key]
        save_alert_state(state)


def send_alert(alert_webhook_url: str | None, message: str, alert_key: str) -> None:
    if not alert_webhook_url:
        return

    if not should_send_alert(alert_key):
        return

    payload = {
        "username": "IG Bot Alerts",
        "content": message,
    }

    try:
        response = requests.post(alert_webhook_url, json=payload, timeout=30)
        response.raise_for_status()
        print(f"Sent alert: {alert_key}")
    except Exception as exc:
        print(f"Failed to send alert {alert_key}: {exc}")


if __name__ == "__main__":
    try:
        run_loop()
    except HardInstagramStop as exc:
        print(f"Bot stopped safely: {exc}")
        raise SystemExit(0)
