"""
youtube_uploader.py
--------------------
Uploads a finished video + thumbnail to YouTube via the (free) YouTube Data
API v3, using a one-time-obtained OAuth refresh token (see
get_refresh_token.py) so no browser login is needed on every run.

Handles both Shorts and long-form: Shorts get '#Shorts' appended and are
scheduled multiple times/day; long-form is scheduled a few times/week.
Videos are uploaded as private with a future publishAt time so a whole
day's/week's content can be produced in one batch run and still drip out
on a natural cadence.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def _get_client():
    creds = Credentials(
        token=None,
        refresh_token=config.YT_REFRESH_TOKEN,
        client_id=config.YT_CLIENT_ID,
        client_secret=config.YT_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    return build("youtube", "v3", credentials=creds)


def upload_video(video_path: str, thumbnail_path: str, content_item: dict,
                  publish_at_utc: datetime) -> str:
    youtube = _get_client()
    is_short = content_item["format"] == "short"

    title = content_item["title"]
    description = content_item["description"]
    tags = content_item["tags"]

    if is_short and "#shorts" not in title.lower() and "#shorts" not in description.lower():
        description = description.rstrip() + "\n\n#Shorts"

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags[:500],
            "categoryId": "27" if "education" in content_item["niche"].lower() else "24",
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": publish_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    print(f"Uploading '{title}' (publish at {publish_at_utc.isoformat()})...")
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"  upload progress: {int(status.progress() * 100)}%")

    video_id = response["id"]
    print(f"Uploaded. Video ID: {video_id}")

    if thumbnail_path and os.path.exists(thumbnail_path):
        youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(thumbnail_path)).execute()
        print("Custom thumbnail set.")

    return video_id


def _log_upload(content_item: dict, video_id: str, publish_at: datetime):
    log = []
    if os.path.exists(config.UPLOAD_LOG_FILE):
        with open(config.UPLOAD_LOG_FILE) as f:
            log = json.load(f)
    log.append({
        "content_id": content_item["id"],
        "video_id": video_id,
        "title": content_item["title"],
        "format": content_item["format"],
        "niche": content_item["niche"],
        "publish_at": publish_at.isoformat(),
        "uploaded_at": datetime.utcnow().isoformat(),
    })
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(config.UPLOAD_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)


def next_available_slot(is_short: bool) -> datetime:
    """Very simple scheduler: next Shorts slot within today/tomorrow at spaced
    hours, or next long-form slot on the next configured weekday. Extend this
    with real analytics-based best-time-to-post data once the channel has history."""
    now = datetime.now(timezone.utc)
    if is_short:
        slot_hours = [13, 17, 21]  # spaced through the day, UTC -- tune to your audience later
        for h in slot_hours:
            candidate = now.replace(hour=h, minute=0, second=0, microsecond=0)
            if candidate > now:
                return candidate
        return (now + timedelta(days=1)).replace(hour=slot_hours[0], minute=0, second=0, microsecond=0)
    else:
        # next Mon/Wed/Fri at 16:00 UTC
        target_weekdays = [0, 2, 4]
        for offset in range(1, 8):
            candidate = now + timedelta(days=offset)
            if candidate.weekday() in target_weekdays:
                return candidate.replace(hour=16, minute=0, second=0, microsecond=0)
        return now + timedelta(days=1)


if __name__ == "__main__":
    item_path, video_path, thumb_path = sys.argv[1], sys.argv[2], sys.argv[3]
    with open(item_path) as f:
        item = json.load(f)
    slot = next_available_slot(is_short=item["format"] == "short")
    vid = upload_video(video_path, thumb_path, item, slot)
    _log_upload(item, vid, slot)
