"""
tts_voiceover.py
----------------
Converts a content item's scenes into narrated audio, completely free,
using edge-tts (Microsoft Edge's neural voices, no API key required).

Produces one mp3 per scene + measures duration so video_assembler.py can
match footage length to speech length exactly.
"""

import asyncio
import json
import os
import subprocess
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

import edge_tts

VOICE = "en-US-GuyNeural"   # change to any edge-tts voice; run `edge-tts --list-voices` to browse


async def _synthesize(text: str, out_path: str, voice: str = VOICE):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(out_path)


def _get_duration_seconds(audio_path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
        capture_output=True, text=True
    )
    return float(result.stdout.strip())


def generate_voiceover(content_item: dict, work_dir: str) -> dict:
    """
    Mutates content_item's scenes in place to add `audio_path` and
    `duration_seconds` for each scene. Returns the updated content_item.
    """
    os.makedirs(work_dir, exist_ok=True)
    for i, scene in enumerate(content_item["scenes"]):
        out_path = os.path.join(work_dir, f"scene_{i:03d}.mp3")
        asyncio.run(_synthesize(scene["narration"], out_path))
        scene["audio_path"] = out_path
        scene["duration_seconds"] = _get_duration_seconds(out_path)
        print(f"  scene {i}: {scene['duration_seconds']:.1f}s -> {out_path}")
    return content_item


if __name__ == "__main__":
    # Manual test: python tts_voiceover.py path/to/content_item.json
    item_path = sys.argv[1]
    with open(item_path) as f:
        item = json.load(f)
    work_dir = os.path.join(config.DATA_DIR, "audio", item["id"])
    updated = generate_voiceover(item, work_dir)
    with open(item_path, "w") as f:
        json.dump(updated, f, indent=2)
