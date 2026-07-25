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

Robustness notes (each fixes a failure that actually occurred):
  * `tags[:500]` sliced the LIST to 500 entries, but YouTube's limit is 500
    *characters* across all tags -- oversized tag sets were rejected with a
    400 invalidTags. Tags are now packed to the real character budget.
  * next_available_slot() only looked at the clock, so two runs in the same
    window scheduled two videos at the identical minute (and a run after the
    last slot of the day collided again tomorrow). Slots are now reconciled
    against the upload log so each video gets a distinct, future time.
  * publishAt requires an RFC-3339 UTC timestamp; a naive datetime silently
    scheduled at the wrong absolute time. Times are normalised to UTC.
  * Resumable uploads got no retry, so one dropped chunk lost the whole video.
  * Setting a custom thumbnail fails on channels without that feature
    enabled; that must not fail an otherwise successful upload.
  * The uploader must not run at all without credentials -- it used to build
    a client with empty strings and fail deep inside googleapiclient.
"""

import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

# YouTube Data API hard limits
MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_CHARS = 5000
MAX_TAGS_CHARS = 500
MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024

SHORT_SLOT_HOURS = [13, 17, 21]      # UTC
LONGFORM_SLOT_WEEKDAYS = [0, 2, 4]   # Mon/Wed/Fri
LONGFORM_SLOT_HOUR = 16

RETRYABLE_STATUS = {500, 502, 503, 504}


class UploadError(RuntimeError):
    pass


def _require_credentials():
    missing = [name for name, value in (
        ("YOUTUBE_CLIENT_ID", config.YT_CLIENT_ID),
        ("YOUTUBE_CLIENT_SECRET", config.YT_CLIENT_SECRET),
        ("YOUTUBE_REFRESH_TOKEN", config.YT_REFRESH_TOKEN),
    ) if not value]
    if missing:
        raise UploadError(
            "Missing YouTube credentials: " + ", ".join(missing) +
            ". Run get_refresh_token.py once locally, then add them as GitHub "
            "Actions secrets (Settings -> Secrets and variables -> Actions)."
        )


def _get_client():
    _require_credentials()
    creds = Credentials(
        token=None,
        refresh_token=config.YT_REFRESH_TOKEN,
        client_id=config.YT_CLIENT_ID,
        client_secret=config.YT_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _pack_tags(tags, budget: int = MAX_TAGS_CHARS):
    """Fit tags inside YouTube's 500-CHARACTER total budget.

    A tag containing a space is counted by YouTube as quoted (+2 chars),
    and each tag costs one separator character.
    """
    packed, used = [], 0
    for tag in tags or []:
        tag = " ".join(str(tag).split()).strip("#")
        if not tag:
            continue
        cost = len(tag) + (2 if " " in tag else 0) + 1
        if used + cost > budget:
            continue
        packed.append(tag)
        used += cost
    return packed


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _load_log():
    if os.path.exists(config.UPLOAD_LOG_FILE):
        try:
            with open(config.UPLOAD_LOG_FILE) as f:
                log = json.load(f)
            if isinstance(log, list):
                return log
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[upload_log.json unreadable ({exc}), starting fresh]")
    return []


def _scheduled_times() -> set:
    """Every publishAt already claimed by a previous run."""
    claimed = set()
    for entry in _load_log():
        raw = entry.get("publish_at")
        if not raw:
            continue
        try:
            claimed.add(_to_utc(datetime.fromisoformat(str(raw).replace("Z", "+00:00"))))
        except (ValueError, TypeError):
            continue
    return claimed


# The cron hours that trigger this pipeline (see .github/workflows/automation.yml).
# Each trigger is mapped to its own publish slot by position, so N runs per day
# produce N distinct publish times with no shared state.
RUN_TRIGGER_HOURS = [6, 12, 19]


def _preferred_slot_for_run(now: datetime) -> int:
    """Give this run its own Shorts slot, derived purely from the clock.

    Runs are matched to slots *by index*: the 1st trigger of the day takes the
    1st slot, the 2nd takes the 2nd, and so on. Matching by proximity instead
    would map both the 06:00 and 12:00 runs onto the 13:00 slot, publishing
    two videos at the same minute.
    """
    # Which trigger of the day is this? (nearest scheduled trigger at or before now)
    index = 0
    for i, hour in enumerate(RUN_TRIGGER_HOURS):
        if now.hour >= hour:
            index = i
    # Manual/off-schedule runs before the first trigger start at slot 0.
    if now.hour < RUN_TRIGGER_HOURS[0]:
        index = 0
    return SHORT_SLOT_HOURS[index % len(SHORT_SLOT_HOURS)]


def next_available_slot(is_short: bool, now: datetime | None = None) -> datetime:
    """Next free publishing slot for this run.

    Two mechanisms keep runs from colliding, deliberately in this order:

      1. Deterministic slot assignment from the run's own clock. This works
         with NO shared state, which matters because data/upload_log.json is
         only persisted if the Actions job can push back to the repo -- if
         that push is blocked (read-only GITHUB_TOKEN), every run would
         otherwise see an empty log and pick the same "next" slot, publishing
         several videos at the identical minute.
      2. The upload log, when it IS available, to skip slots already taken
         (e.g. after a manual re-run in the same window).

    Extend this with real analytics-based best-time-to-post data once the
    channel has history.
    """
    now = _to_utc(now or datetime.now(timezone.utc))
    claimed = _scheduled_times()
    # publishAt must be comfortably in the future or YouTube rejects it.
    earliest = now + timedelta(minutes=20)

    if is_short:
        # Start from this run's own slot, then walk forward.
        preferred = _preferred_slot_for_run(now)
        start = SHORT_SLOT_HOURS.index(preferred)
        ordered = SHORT_SLOT_HOURS[start:] + SHORT_SLOT_HOURS[:start]
        for day in range(0, 14):
            base = (now + timedelta(days=day)).replace(minute=0, second=0, microsecond=0)
            for offset, hour in enumerate(ordered):
                # Slots that wrapped past midnight belong to the following day.
                candidate = base.replace(hour=hour)
                if day == 0 and hour < preferred:
                    candidate += timedelta(days=1)
                if candidate >= earliest and candidate not in claimed:
                    return candidate
    else:
        for day in range(0, 28):
            candidate = (now + timedelta(days=day)).replace(
                hour=LONGFORM_SLOT_HOUR, minute=0, second=0, microsecond=0)
            if (candidate.weekday() in LONGFORM_SLOT_WEEKDAYS
                    and candidate >= earliest and candidate not in claimed):
                return candidate

    return earliest + timedelta(hours=1)


def _build_body(content_item: dict, publish_at_utc: datetime) -> dict:
    is_short = content_item.get("format") == "short"
    title = " ".join(str(content_item.get("title", "")).split())
    description = str(content_item.get("description", "")).strip()

    if is_short and "#shorts" not in title.lower() and "#shorts" not in description.lower():
        description = (description + "\n\n#Shorts").strip()

    tags = _pack_tags(content_item.get("tags"))
    niche = str(content_item.get("niche", "")).lower()
    education_like = any(w in niche for w in
                         ("education", "science", "history", "psychology",
                          "facts", "space", "astronomy", "geography"))

    return {
        "snippet": {
            "title": title[:MAX_TITLE_CHARS] or "Untitled",
            "description": description[:MAX_DESCRIPTION_CHARS],
            "tags": tags,
            "categoryId": "27" if education_like else "24",
            "defaultLanguage": "en",
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": _to_utc(publish_at_utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "selfDeclaredMadeForKids": False,
        },
    }


def _set_thumbnail(youtube, video_id: str, thumbnail_path: str):
    if not thumbnail_path or not os.path.exists(thumbnail_path):
        return
    if os.path.getsize(thumbnail_path) > MAX_THUMBNAIL_BYTES:
        print("  [thumbnail exceeds YouTube's 2MB limit, skipping]")
        return
    try:
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(thumbnail_path, mimetype="image/jpeg"),
        ).execute()
        print("Custom thumbnail set.")
    except HttpError as exc:
        # Unverified channels can't set custom thumbnails -- not fatal.
        print(f"  [could not set custom thumbnail: {exc.resp.status} "
              f"{exc.reason}. The video is still uploaded.]")


def upload_video(video_path: str, thumbnail_path: str, content_item: dict,
                 publish_at_utc: datetime) -> str:
    if not os.path.exists(video_path):
        raise UploadError(f"video file not found: {video_path}")

    youtube = _get_client()
    body = _build_body(content_item, publish_at_utc)

    media = MediaFileUpload(video_path, chunksize=4 * 1024 * 1024,
                            resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    size_mb = os.path.getsize(video_path) / (1024 * 1024)
    print(f"Uploading '{body['snippet']['title']}' ({size_mb:.1f}MB, "
          f"publish at {body['status']['publishAt']})...")

    response, errors = None, 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                print(f"  upload progress: {int(status.progress() * 100)}%")
        except HttpError as exc:
            if exc.resp.status in RETRYABLE_STATUS and errors < 5:
                errors += 1
                wait = min(60, (2 ** errors) + random.random())
                print(f"  [transient {exc.resp.status} from YouTube, "
                      f"retrying chunk in {wait:.1f}s ({errors}/5)]")
                time.sleep(wait)
                continue
            if exc.resp.status == 403 and "quota" in str(exc).lower():
                raise UploadError(
                    "YouTube daily upload quota exhausted (each upload costs "
                    "1600 of 10000 units). Try again after the quota resets."
                ) from exc
            raise UploadError(f"YouTube rejected the upload: {exc}") from exc
        except (ConnectionError, OSError) as exc:
            if errors < 5:
                errors += 1
                wait = min(60, 2 ** errors)
                print(f"  [network error ({type(exc).__name__}), retrying in {wait}s]")
                time.sleep(wait)
                continue
            raise UploadError(f"upload failed after retries: {exc}") from exc
        except RefreshError as exc:
            raise UploadError(
                "OAuth refresh token is invalid or revoked. Re-run "
                "get_refresh_token.py and update YOUTUBE_REFRESH_TOKEN."
            ) from exc

    video_id = response["id"]
    print(f"Uploaded. Video ID: {video_id}")
    _set_thumbnail(youtube, video_id, thumbnail_path)
    return video_id


def _log_upload(content_item: dict, video_id: str, publish_at: datetime):
    log = _load_log()
    log.append({
        "content_id": content_item.get("id"),
        "video_id": video_id,
        "title": content_item.get("title"),
        "format": content_item.get("format"),
        "niche": content_item.get("niche"),
        "publish_at": _to_utc(publish_at).isoformat(),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "url": f"https://youtube.com/watch?v={video_id}",
    })
    os.makedirs(config.DATA_DIR, exist_ok=True)
    tmp_path = config.UPLOAD_LOG_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(log, f, indent=2)
    os.replace(tmp_path, config.UPLOAD_LOG_FILE)  # atomic


if __name__ == "__main__":
    item_path, video_path, thumb_path = sys.argv[1], sys.argv[2], sys.argv[3]
    with open(item_path) as f:
        item = json.load(f)
    slot = next_available_slot(is_short=item.get("format") == "short")
    vid = upload_video(video_path, thumb_path, item, slot)
    _log_upload(item, vid, slot)
