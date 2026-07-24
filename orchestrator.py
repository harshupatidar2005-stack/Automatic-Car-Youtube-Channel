"""
orchestrator.py
----------------
The single entry point GitHub Actions calls on a schedule. Each run:

  1. Re-evaluates the niche if it's gone stale (monthly, see config).
  2. Refills the content queue if it's running low.
  3. Pops the next queued item, generates voiceover -> video -> thumbnail.
  4. Uploads it to YouTube, scheduled for the next natural slot.
  5. Logs everything to data/ so future runs know the channel's state.

This script assumes zero human input at run time -- every decision
(niche, topic, schedule slot) is made by the earlier modules.
"""

import json
import os
import sys

sys.path.append(os.path.abspath(__file__)
import config
from niche_research import choose_best_niche
from script_writer import refill_queue, _load_queue, _save_queue
from tts_voiceover import generate_voiceover
from video_assembler import assemble_video
from thumbnail_gen import generate_thumbnail
from youtube_uploader import upload_video, next_available_slot, _log_upload


MIN_QUEUE_SIZE = 2


def run_once():
    print("=== Step 1: niche check ===")
    niche_data = choose_best_niche()
    niche = niche_data["niche"]

    print("\n=== Step 2: content queue check ===")
    queue = _load_queue()
    if len(queue) < MIN_QUEUE_SIZE:
        print("Queue low, generating more content...")
        queue = refill_queue(niche)

    if not queue:
        print("No content available even after refill. Exiting.")
        return

    item = queue.pop(0)
    _save_queue(queue)
    print(f"\n=== Step 3: producing '{item['title']}' ({item['format']}) ===")

    work_dir = os.path.join(config.DATA_DIR, "work", item["id"])
    os.makedirs(work_dir, exist_ok=True)

    print("-- generating voiceover --")
    item = generate_voiceover(item, os.path.join(work_dir, "audio"))

    print("-- assembling video --")
    video_path = assemble_video(item, os.path.join(work_dir, "video"))

    print("-- generating thumbnail --")
    thumb_path = os.path.join(work_dir, "thumbnail.jpg")
    generate_thumbnail(video_path, item["title"], thumb_path, is_short=item["format"] == "short")

    print("-- uploading to YouTube --")
    slot = next_available_slot(is_short=item["format"] == "short")
    video_id = upload_video(video_path, thumb_path, item, slot)
    _log_upload(item, video_id, slot)

    print(f"\nDone. https://youtube.com/watch?v={video_id} scheduled for {slot.isoformat()}")


if __name__ == "__main__":
    run_once()
