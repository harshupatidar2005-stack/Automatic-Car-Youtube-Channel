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

Robustness notes (each fixes a failure that actually occurred):
  * The item was popped and the queue saved BEFORE production; any failure
    downstream destroyed that script permanently. The item is now only
    removed from the queue after a successful upload, and failed items are
    quarantined instead of silently vanishing.
  * A crash left gigabytes of intermediate video in data/work; that directory
    is cleaned up on every exit path.
  * There was no --dry-run, so the pipeline could not be validated end-to-end
    without publishing to a real channel.
  * Exit codes were always 0, so a failing scheduled run looked green in the
    Actions tab.
"""

import argparse
import json
import os
import shutil
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from niche_research import choose_best_niche
from script_writer import _load_queue, _save_queue, refill_queue
from thumbnail_gen import generate_thumbnail
from tts_voiceover import generate_voiceover
from video_assembler import assemble_video
from youtube_uploader import _log_upload, next_available_slot, upload_video

MIN_QUEUE_SIZE = 2
FAILED_DIR = os.path.join(config.DATA_DIR, "failed")


def _quarantine(item: dict, error: str):
    """Keep a failed script for inspection rather than losing it silently."""
    os.makedirs(FAILED_DIR, exist_ok=True)
    path = os.path.join(FAILED_DIR, f"{item.get('id', 'unknown')}.json")
    payload = dict(item)
    payload["failure"] = error
    try:
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"Quarantined failed item -> {path}")
    except OSError as exc:
        print(f"[could not quarantine item: {exc}]")


def _cleanup(work_dir: str):
    if os.path.isdir(work_dir):
        shutil.rmtree(work_dir, ignore_errors=True)


def run_once(dry_run: bool = False, force_niche: bool = False) -> int:
    print("=== Step 1: niche check ===")
    niche_data = choose_best_niche(force=force_niche)
    niche = niche_data["niche"]
    print(f"Niche: {niche}")

    print("\n=== Step 2: content queue check ===")
    queue = _load_queue()
    print(f"Queue holds {len(queue)} item(s)")
    if len(queue) < MIN_QUEUE_SIZE:
        print("Queue low, generating more content...")
        queue = refill_queue(niche)

    if not queue:
        print("No content available even after refill. Exiting.")
        return 1

    # Peek, don't pop: the item stays queued until it is actually published,
    # so a mid-pipeline crash cannot destroy a generated script.
    item = queue[0]
    fmt = item.get("format", "longform")
    print(f"\n=== Step 3: producing '{item.get('title')}' ({fmt}) ===")

    work_dir = os.path.join(config.DATA_DIR, "work", str(item.get("id")))
    os.makedirs(work_dir, exist_ok=True)

    try:
        print("-- generating voiceover --")
        item = generate_voiceover(item, os.path.join(work_dir, "audio"))

        print("-- assembling video --")
        video_path = assemble_video(item, os.path.join(work_dir, "video"))

        print("-- generating thumbnail --")
        thumb_path = os.path.join(work_dir, "thumbnail.jpg")
        generate_thumbnail(video_path, item.get("title", ""), thumb_path,
                           is_short=fmt == "short")

        slot = next_available_slot(is_short=fmt == "short")

        if dry_run:
            size_mb = os.path.getsize(video_path) / (1024 * 1024)
            print("\n-- DRY RUN: skipping upload --")
            print(f"   video     : {video_path} ({size_mb:.1f}MB)")
            print(f"   thumbnail : {thumb_path}")
            print(f"   would publish at: {slot.isoformat()}")
            # Preserve artifacts for inspection in a dry run.
            keep = os.path.join(config.DATA_DIR, "dry_run", str(item.get("id")))
            os.makedirs(os.path.dirname(keep), exist_ok=True)
            shutil.rmtree(keep, ignore_errors=True)
            shutil.copytree(work_dir, keep)
            print(f"   artifacts copied to: {keep}")
            return 0

        print("-- uploading to YouTube --")
        video_id = upload_video(video_path, thumb_path, item, slot)
        _log_upload(item, video_id, slot)

        # Only now is it safe to drop the item from the queue.
        remaining = _load_queue()
        remaining = [q for q in remaining if q.get("id") != item.get("id")]
        _save_queue(remaining)

        print(f"\nDone. https://youtube.com/watch?v={video_id} "
              f"scheduled for {slot.isoformat()}")
        return 0

    except Exception as exc:
        print(f"\n!! Production failed: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        # Drop the poisoned item so the next scheduled run isn't stuck on it.
        remaining = _load_queue()
        remaining = [q for q in remaining if q.get("id") != item.get("id")]
        _save_queue(remaining)
        _quarantine(item, f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        if not dry_run:
            _cleanup(work_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one content production cycle.")
    parser.add_argument("--dry-run", action="store_true",
                        help="produce the video but do not upload to YouTube")
    parser.add_argument("--force-niche", action="store_true",
                        help="re-run niche research even if the current pick is fresh")
    args = parser.parse_args()
    return run_once(dry_run=args.dry_run, force_niche=args.force_niche)


if __name__ == "__main__":
    sys.exit(main())
