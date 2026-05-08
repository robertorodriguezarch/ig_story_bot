from __future__ import annotations

import json
import os
import time
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from instagrapi import Client


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

SESSION_FILE = BASE_DIR / "post_session.json"
SEEN_POSTS_FILE = BASE_DIR / "seen_post_ids.json"
DOWNLOADS_DIR = BASE_DIR / "post_downloads"
ALERT_STATE_FILE = BASE_DIR / "post_alert_state.json"

DOWNLOADS_DIR.mkdir(exist_ok=True)

ALERT_COOLDOWN_SECONDS = 60 * 60
POST_FETCH_AMOUNT = int(os.getenv("POST_FETCH_AMOUNT", 5))


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


def load_seen_post_ids() -> set[str]:
    if not SEEN_POSTS_FILE.exists():
        return set()

    try:
        data = json.loads(SEEN_POSTS_FILE.read_text())
        return {str(x) for x in data}
    except Exception:
        return set()


def save_seen_post_ids(seen_ids: set[str]) -> None:
    SEEN_POSTS_FILE.write_text(json.dumps(sorted(seen_ids), indent=2))


def format_post_time(dt: datetime | None) -> str:
    if not dt:
        return "Unknown time"

    local_dt = dt.astimezone(ZoneInfo("America/New_York"))
    return local_dt.strftime("%m/%d/%y %-I:%M %p")


def download_file(url: str, out_path: Path) -> None:
    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with out_path.open("wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)


def build_post_url(username: str, code: str | None) -> str:
    if code:
        return f"https://www.instagram.com/p/{code}/"
    return f"https://www.instagram.com/{username}/"


def trim_caption(caption: str | None, max_len: int = 900) -> str:
    if not caption:
        return ""

    caption = caption.strip()
    if len(caption) <= max_len:
        return caption

    return caption[: max_len - 3].rstrip() + "..."


def get_first_image_url(media) -> str | None:
    """
    Best-effort image extraction.

    Handles:
    - normal image post
    - carousel first image
    - video thumbnail fallback
    """

    # Carousel / album
    resources = getattr(media, "resources", None)
    if resources:
        first = resources[0]
        thumb = getattr(first, "thumbnail_url", None)
        if thumb:
            return str(thumb)

    # Single image or video thumbnail
    thumb = getattr(media, "thumbnail_url", None)
    if thumb:
        return str(thumb)

    return None


def post_ig_post_to_discord(
    webhook_url: str,
    target_username: str,
    media,
    image_path: Path | None,
    role_id: str | None,
    avatar_url: str | None,
) -> None:
    post_pk = str(media.pk)
    caption = trim_caption(getattr(media, "caption_text", None))
    code = getattr(media, "code", None)
    post_url = build_post_url(target_username, code)
    taken_at = getattr(media, "taken_at", None)

    description_parts = []

    if caption:
        description_parts.append(caption)

    description_parts.append(f"[Open Instagram post]({post_url})")

    embed = {
        "author": {
            "name": f"@{target_username}",
            "url": f"https://instagram.com/{target_username}",
        },
        "description": "\n\n".join(description_parts),
        "footer": {"text": format_post_time(taken_at)},
        "color": 0xE1306C,
    }

    if image_path:
        embed["image"] = {"url": f"attachment://{image_path.name}"}

    payload = {
        "username": f"@{target_username}",
        "content": f"<@&{role_id}>" if role_id else "",
        "allowed_mentions": {"roles": [role_id]} if role_id else {},
        "embeds": [embed],
    }

    if avatar_url:
        payload["avatar_url"] = avatar_url

    if image_path:
        with image_path.open("rb") as f:
            files = {
                "file": (image_path.name, f),
                "payload_json": (None, json.dumps(payload)),
            }
            response = requests.post(webhook_url, files=files, timeout=60)
            response.raise_for_status()
    else:
        response = requests.post(webhook_url, json=payload, timeout=60)
        response.raise_for_status()

    print(f"Posted IG post {post_pk}")


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


def send_alert(alert_webhook_url: str | None, message: str, alert_key: str) -> None:
    if not alert_webhook_url:
        return

    if not should_send_alert(alert_key):
        return

    payload = {
        "username": "IG Post Bot Alerts",
        "content": message,
    }

    try:
        response = requests.post(alert_webhook_url, json=payload, timeout=30)
        response.raise_for_status()
        print(f"Sent alert: {alert_key}")
    except Exception as exc:
        print(f"Failed to send alert {alert_key}: {exc}")


def classify_and_alert_fetch_error(
    error_text: str,
    target_username: str,
    alert_webhook_url: str | None,
) -> None:
    lowered = error_text.lower()

    if "429" in lowered or "too many 429" in lowered:
        send_alert(
            alert_webhook_url,
            f"⚠️ IG post bot is being rate-limited for @{target_username}.\nError: `{error_text[:1500]}`",
            "ig_post_rate_limited",
        )
        return

    if (
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
            f"🚨 IG post bot hit a challenge/checkpoint for @{target_username}. Bot stopped.\nError: `{error_text[:1500]}`",
            "ig_post_challenge",
        )
        raise HardInstagramStop("Challenge/checkpoint detected; post bot stopped.")

    if "login_required" in lowered:
        send_alert(
            alert_webhook_url,
            f"🚨 IG post bot session is no longer valid for @{target_username}. Bot stopped.\nError: `{error_text[:1500]}`",
            "ig_post_login_required",
        )
        raise HardInstagramStop("login_required detected; post bot stopped.")

    send_alert(
        alert_webhook_url,
        f"⚠️ IG post bot failed for @{target_username}.\nError: `{error_text[:1500]}`",
        "ig_post_generic_error",
    )


def run_once() -> None:
    sessionid = os.getenv("IG_SESSIONID")
    target_username = os.getenv("TARGET_USERNAME")
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    role_id = os.getenv("DISCORD_IG_STORY_ROLE_ID")
    alert_webhook_url = os.getenv("DISCORD_ALERT_WEBHOOK_URL")
    avatar_url = os.getenv("DISCORD_AVATAR_URL")

    print(f"Config loaded: POST_FETCH_AMOUNT={POST_FETCH_AMOUNT}")
    print(f"Session ID loaded: {bool(sessionid)}")
    print(f"Session ID preview: {sessionid[:12]}..." if sessionid else "No session ID")

    if not sessionid or not target_username or not webhook_url:
        raise RuntimeError(
            "Missing IG_SESSIONID, TARGET_USERNAME, or DISCORD_WEBHOOK_URL in .env"
        )

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
            f"🚨 IG post bot failed to login. Bot stopped.\nError: `{error_text[:1500]}`",
            "ig_post_login_failure",
        )

        raise HardInstagramStop("Login failed; human intervention required.")

    try:
        print(f"Fetching recent posts for @{target_username}...")
        user = cl.user_info_by_username_v1(target_username)
        medias = cl.user_medias(user.pk, amount=POST_FETCH_AMOUNT)
    except Exception as exc:
        error_text = str(exc)
        print(f"Failed to fetch posts for @{target_username}: {error_text}")
        classify_and_alert_fetch_error(error_text, target_username, alert_webhook_url)
        return

    if not medias:
        print("No recent posts found.")
        return

    seen_ids = load_seen_post_ids()
    new_count = 0

    # Oldest first, so Discord order is natural if multiple are unseen
    for media in reversed(medias):
        media_pk = str(media.pk)

        if media_pk in seen_ids:
            continue

        image_url = get_first_image_url(media)
        image_path = None

        if image_url:
            image_path = DOWNLOADS_DIR / f"{media_pk}.jpg"
            try:
                download_file(image_url, image_path)
            except Exception as exc:
                print(f"Failed to download thumbnail for post {media_pk}: {exc}")
                image_path = None

        try:
            post_ig_post_to_discord(
                webhook_url=webhook_url,
                target_username=target_username,
                media=media,
                image_path=image_path,
                role_id=role_id,
                avatar_url=avatar_url,
            )
            seen_ids.add(media_pk)
            new_count += 1
            time.sleep(2)
        except Exception as exc:
            print(f"Failed to post IG post {media_pk} to Discord: {exc}")

    save_seen_post_ids(seen_ids)
    print(f"Done. New IG posts posted: {new_count}")


if __name__ == "__main__":
    try:
        run_once()
    except HardInstagramStop as exc:
        print(f"Post bot stopped safely: {exc}")
        raise SystemExit(0)
