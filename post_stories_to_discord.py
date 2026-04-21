from __future__ import annotations

import json
import os
import time
import traceback
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from instagrapi import Client

BASE_DIR = Path(__file__).resolve().parent
SESSION_FILE = BASE_DIR / "session.json"
SEEN_FILE = BASE_DIR / "seen_story_ids.json"
DOWNLOADS_DIR = BASE_DIR / "downloads"
ALERT_STATE_FILE = BASE_DIR / "alert_state.json"
ALERT_COOLDOWN_SECONDS = 60 * 60  # 1 hour
POLL_INTERVAL_SECONDS = 600  # 10 mintues

DOWNLOADS_DIR.mkdir(exist_ok=True)


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
    return dt.strftime("%m/%d/%y %-I:%M %p")


def post_story_to_discord(
    webhook_url: str,
    target_username: str,
    taken_at: datetime,
    media_path: Path,
    is_video: bool,
    role_id: str | None,
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
        "avatar_url": "https://scontent-atl3-2.cdninstagram.com/v/t51.82787-19/656284631_18095830574083910_6941561791975990136_n.jpg?efg=eyJ2ZW5jb2RlX3RhZyI6InByb2ZpbGVfcGljLmRqYW5nby4xMDgwLmMyIn0&_nc_ht=scontent-atl3-2.cdninstagram.com&_nc_cat=102&_nc_oc=Q6cZ2gGpQ_yms_whNywcUKaTnobqP0BTUnN1WG4riUMicc6ToAGVt0MWgDp5D8uUu6UMSsQufC2fdK-n_cwsfPPjEE4B&_nc_ohc=IIiI6qBajLoQ7kNvwEpkids&_nc_gid=HaO6iTmAm1hylxTMluQlyQ&edm=AP4sbd4BAAAA&ccb=7-5&oh=00_Af1hkO4BDAuc76-EnrZCccUx_Doeb08vt3EqtqMA5zn-yQ&oe=69DF16C1&_nc_sid=7a9f4b",
        "content": f"<@&{role_id}>" if role_id else "",
        "allowed_mentions": {"roles": [role_id]} if role_id else {},
        "embeds": [embed],
    }

    with media_path.open("rb") as f:
        files = {
            "file": (media_path.name, f),
            "payload_json": (None, json.dumps(payload)),
        }
        response = requests.post(webhook_url, files=files, timeout=60)
        response.raise_for_status()


def run_loop() -> None:
    load_dotenv()

    sessionid = os.getenv("IG_SESSIONID")
    target_username = os.getenv("TARGET_USERNAME")
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    role_id = os.getenv("DISCORD_IG_STORY_ROLE_ID")
    alert_webhook_url = os.getenv("DISCORD_ALERT_WEBHOOK_URL")

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

        time.sleep(POLL_INTERVAL_SECONDS)
        return

    #  Loop only does fetching
    try:
        while True:
            try:
                print(
                    f"\n--- New polling cycle at {datetime.now().strftime('%Y-%m-%d %I:%M:%S %p')} ---"
                )
                run_once(cl, target_username, webhook_url, role_id, alert_webhook_url)
            except Exception as exc:
                print(f"Loop error: {exc}")

            print(f"Sleeping for {POLL_INTERVAL_SECONDS} seconds...\n")
            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\nStopped by user.")


def run_once(
    cl: Client,
    target_username: str,
    webhook_url: str,
    role_id: str | None,
    alert_webhook_url: str | None,
) -> None:
    try:
        user = cl.user_info_by_username_v1(target_username)
        stories = cl.user_stories(user.pk)

        clear_alert("ig_challenge")
        clear_alert("ig_login_required")
        clear_alert("ig_generic_fetch_error")
        clear_alert("ig_rate_limited")
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
        elif "challengeresolve" in lowered or "challenge" in lowered:
            send_alert(
                alert_webhook_url,
                f"🚨 IG bot hit a challenge/checkpoint for @{target_username}.\nError: `{error_text[:1500]}`",
                "ig_challenge",
            )
        elif "login_required" in lowered:
            send_alert(
                alert_webhook_url,
                f"🚨 IG bot session is no longer valid for @{target_username}.\nError: `{error_text[:1500]}`",
                "ig_login_required",
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
        initialize_seen_ids_from_current_stories(stories)
        print(
            "First run detected. Current stories marked as seen; waiting for new ones."
        )
        return

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
    run_loop()
