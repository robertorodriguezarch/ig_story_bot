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
        print(f"Login failed: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return

    #  Loop only does fetching
    try:
        while True:
            try:
                print(
                    f"\n--- New polling cycle at {datetime.now().strftime('%Y-%m-%d %I:%M:%S %p')} ---"
                )
                run_once(cl, target_username, webhook_url, role_id)
            except Exception as exc:
                print(f"Loop error: {exc}")

            print("Sleeping for 90 seconds...\n")
            time.sleep(90)
    except KeyboardInterrupt:
        print("\nStopped by user.")


def run_once(
    cl: Client,
    target_username: str,
    webhook_url: str,
    role_id: str | None,
) -> None:
    try:
        user = cl.user_info_by_username_v1(target_username)
        stories = cl.user_stories(user.pk)
    except Exception as exc:
        print(f"Failed to fetch stories for @{target_username}: {exc}")
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


if __name__ == "__main__":
    run_loop()
