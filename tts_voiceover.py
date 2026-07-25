"""
tts_voiceover.py
----------------
Converts a content item's scenes into narrated audio, completely free,
using edge-tts (Microsoft Edge's neural voices, no API key required).

Produces one mp3 per scene + measures duration so video_assembler.py can
match footage length to speech length exactly.

Robustness notes (each fixes a failure that actually occurred):
  * Duration measurement no longer assumes `ffprobe` is installed -- it goes
    through media.probe_duration(), which falls back to ffmpeg.
  * edge-tts talks to a Microsoft endpoint that intermittently drops
    connections and rate-limits; every scene is retried with backoff, and a
    silent-but-correctly-timed track is used as a last resort so one flaky
    request can't abandon a half-produced video.
  * Empty/whitespace narration used to produce a 0-byte mp3 that broke the
    assembler downstream; those scenes are now skipped explicitly.
"""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

import edge_tts

from media import probe_duration, run_ffmpeg

VOICE = os.environ.get("TTS_VOICE", "en-US-GuyNeural")
MAX_TTS_RETRIES = 4
# Rough spoken-word rate, used only to time the silent fallback track.
WORDS_PER_SECOND = 2.6


async def _synthesize(text: str, out_path: str, voice: str = VOICE):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(out_path)


def _synthesize_with_retry(text: str, out_path: str, voice: str = VOICE) -> bool:
    """Synthesize one scene, retrying transient network/rate-limit errors."""
    for attempt in range(MAX_TTS_RETRIES):
        try:
            asyncio.run(_synthesize(text, out_path, voice))
            if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
                return True
            raise RuntimeError("edge-tts returned an empty audio file")
        except Exception as exc:
            wait = 3 * (attempt + 1)
            if attempt == MAX_TTS_RETRIES - 1:
                print(f"  [TTS failed after {MAX_TTS_RETRIES} attempts: "
                      f"{type(exc).__name__}: {exc}]")
                return False
            print(f"  [TTS attempt {attempt + 1} failed ({type(exc).__name__}), "
                  f"retrying in {wait}s]")
            time.sleep(wait)
    return False


def _make_silent_track(text: str, out_path: str) -> float:
    """Timed silence, so a TTS outage degrades instead of aborting the run."""
    words = max(1, len(text.split()))
    duration = max(1.5, round(words / WORDS_PER_SECOND, 2))
    run_ffmpeg(["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo",
                "-t", f"{duration:.3f}", "-c:a", "libmp3lame", "-q:a", "5",
                out_path],
               description="silent fallback track")
    return duration


def generate_voiceover(content_item: dict, work_dir: str) -> dict:
    """
    Mutates content_item's scenes in place to add `audio_path` and
    `duration_seconds` for each scene. Returns the updated content_item.
    """
    os.makedirs(work_dir, exist_ok=True)
    scenes = content_item.get("scenes") or []
    if not scenes:
        raise ValueError("content item has no scenes to narrate")

    usable = []
    for i, scene in enumerate(scenes):
        narration = " ".join((scene.get("narration") or "").split())
        if not narration:
            print(f"  scene {i}: empty narration, skipping")
            continue

        out_path = os.path.join(work_dir, f"scene_{i:03d}.mp3")
        if _synthesize_with_retry(narration, out_path):
            try:
                duration = probe_duration(out_path)
            except Exception as exc:
                print(f"  scene {i}: could not measure duration ({exc}), using silence")
                duration = _make_silent_track(narration, out_path)
        else:
            duration = _make_silent_track(narration, out_path)

        scene["narration"] = narration
        scene["audio_path"] = out_path
        scene["duration_seconds"] = duration
        usable.append(scene)
        print(f"  scene {i}: {duration:.1f}s -> {out_path}")

    if not usable:
        raise RuntimeError("no narratable scenes in this content item")

    content_item["scenes"] = usable
    total = sum(s["duration_seconds"] for s in usable)
    content_item["total_duration_seconds"] = round(total, 2)
    print(f"  total narration: {total / 60:.1f} min across {len(usable)} scenes")
    return content_item


if __name__ == "__main__":
    item_path = sys.argv[1]
    with open(item_path) as f:
        item = json.load(f)
    work_dir = os.path.join(config.DATA_DIR, "audio", item["id"])
    updated = generate_voiceover(item, work_dir)
    with open(item_path, "w") as f:
        json.dump(updated, f, indent=2)
