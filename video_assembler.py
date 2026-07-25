"""
video_assembler.py
-------------------
Turns a content_item (with narration audio already generated per scene) into
a finished mp4: fetches matching free stock footage per scene from Pexels
(fallback: Pixabay), fits each clip to its scene's audio duration, overlays
captions, concatenates everything, and muxes in the voiceover track.

Free stack: Pexels/Pixabay stock video APIs + ffmpeg (both free).

Robustness notes (each fixes a failure that actually occurred):
  * Captions are rendered with Pillow and composited via `overlay` instead of
    ffmpeg's `drawtext`. drawtext is absent from many ffmpeg builds, and its
    escaping rules corrupt the apostrophes/colons/percent signs that show up
    in nearly every narration line.
  * Every scene is normalised to identical fps / SAR / timescale / audio
    layout, because `concat -c copy` silently produces non-monotonic DTS and
    a wrong total duration when the source clips differ (stock clips vary
    between 24, 25, 30 and 60 fps).
  * Scene render is a single ffmpeg pass (fit + caption + audio mux) rather
    than four chained passes writing three intermediate files per scene.
  * Stock-footage lookups retry, rotate through result candidates, and fall
    back to a generated gradient card, so one dead CDN link cannot abort a run.
"""

import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import requests

from media import MediaError, render_caption_png, run_ffmpeg

# Scene clips are all normalised to this so concat -c copy is exact.
TARGET_FPS = 30
AUDIO_RATE = 44100
AUDIO_CHANNELS = 2
TIMESCALE = "30000"

HTTP_TIMEOUT = 30
MAX_SEARCH_ATTEMPTS = 3


def _get_json(url: str, **kwargs) -> dict:
    """GET with small retry/backoff; returns {} instead of raising."""
    for attempt in range(MAX_SEARCH_ATTEMPTS):
        try:
            resp = requests.get(url, timeout=HTTP_TIMEOUT, **kwargs)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 3 * (attempt + 1)))
                print(f"  [stock API rate limited, waiting {wait}s]")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            if attempt == MAX_SEARCH_ATTEMPTS - 1:
                print(f"  [stock lookup failed: {type(exc).__name__}: {exc}]")
                return {}
            time.sleep(2 * (attempt + 1))
    return {}


def _search_pexels_videos(keyword: str, orientation: str) -> list:
    """Return candidate download links from Pexels, best-sized first."""
    if not config.PEXELS_API_KEY:
        return []
    data = _get_json(
        "https://api.pexels.com/videos/search",
        headers={"Authorization": config.PEXELS_API_KEY},
        params={"query": keyword, "orientation": orientation,
                "per_page": 5, "size": "medium"},
    )
    links = []
    for video in data.get("videos", []):
        files = [f for f in video.get("video_files", []) if f.get("link")]
        if not files:
            continue
        # Prefer ~720-1280px wide mp4s: enough for 1080p output after scaling,
        # without pulling multi-hundred-MB 4K originals on a free runner.
        def rank(f):
            width = f.get("width") or 0
            return (0 if 720 <= width <= 1920 else 1, abs(width - 1280))
        files.sort(key=rank)
        links.append(files[0]["link"])
    return links


def _search_pixabay_videos(keyword: str, orientation: str) -> list:
    if not config.PIXABAY_API_KEY:
        return []
    data = _get_json(
        "https://pixabay.com/api/videos/",
        params={"key": config.PIXABAY_API_KEY, "q": keyword, "per_page": 5,
                "safesearch": "true"},
    )
    links = []
    for hit in data.get("hits", []):
        videos = hit.get("videos", {})
        for quality in ("medium", "small", "large", "tiny"):
            url = (videos.get(quality) or {}).get("url")
            if url:
                links.append(url)
                break
    return links


def _download(url: str, out_path: str) -> bool:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    try:
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
    except Exception as exc:
        print(f"  [download failed: {type(exc).__name__}: {exc}]")
        return False
    # A truncated/empty file would fail much later inside ffmpeg with an
    # opaque error, so reject it here where the cause is still obvious.
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 8192:
        print("  [download produced an empty/truncated file, discarding]")
        return False
    return True


def _fetch_clip_for_scene(keyword: str, orientation: str, out_path: str) -> bool:
    """Try every candidate link from both providers before giving up."""
    candidates = _search_pexels_videos(keyword, orientation)
    candidates += _search_pixabay_videos(keyword, orientation)
    if not candidates:
        print(f"  [no stock footage found for '{keyword}', using generated background]")
        return False
    for link in candidates[:4]:
        if _download(link, out_path):
            return True
    print(f"  [all stock candidates failed for '{keyword}', using generated background]")
    return False


def _make_fallback_clip(duration: float, resolution: tuple, out_path: str, seed: str = ""):
    """Generated gradient card used when no stock clip is usable.

    Better than a flat dark rectangle: a slow-drifting gradient keeps the
    frame from looking like a broken/blank video.
    """
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    width, height = resolution
    rnd = random.Random(seed or out_path)
    hue = rnd.randint(0, 359)
    run_ffmpeg(
        ["-f", "lavfi",
         "-i", f"gradients=s={width}x{height}:d={max(duration, 1):.3f}:"
               f"speed=0.06:x0=0:y0=0:x1={width}:y1={height}:"
               f"c0=0x11131a:c1=0x232a3b:nb_colors=2:seed={hue}",
         "-t", f"{max(duration, 0.5):.3f}",
         "-r", str(TARGET_FPS), "-c:v", "libx264", "-preset", "veryfast",
         "-pix_fmt", "yuv420p", out_path],
        description="fallback background",
    )


def _render_scene(src_video: str, audio_path: str, duration: float,
                  resolution: tuple, caption_png: str | None, out_path: str):
    """Fit footage to duration, overlay the caption, mux narration -- one pass."""
    width, height = resolution
    # scale+crop to fill, force CFR and square pixels so every scene is
    # byte-compatible for stream-copy concatenation.
    chain = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
             f"crop={width}:{height},fps={TARGET_FPS},setsar=1,format=yuv420p")

    args = ["-stream_loop", "-1", "-i", src_video]
    if caption_png:
        args += ["-i", caption_png]
    args += ["-i", audio_path]

    audio_index = 2 if caption_png else 1
    if caption_png:
        filter_complex = f"[0:v]{chain}[bg];[bg][1:v]overlay=0:0:format=auto,format=yuv420p[v]"
    else:
        filter_complex = f"[0:v]{chain}[v]"

    args += [
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", f"{audio_index}:a",
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-profile:v", "high", "-level", "4.1",
        "-r", str(TARGET_FPS), "-video_track_timescale", TIMESCALE,
        "-c:a", "aac", "-b:a", "128k", "-ar", str(AUDIO_RATE), "-ac", str(AUDIO_CHANNELS),
        "-movflags", "+faststart",
        out_path,
    ]
    run_ffmpeg(args, description="scene render")


def assemble_video(content_item: dict, work_dir: str) -> str:
    os.makedirs(work_dir, exist_ok=True)
    is_short = content_item.get("format") == "short"
    resolution = config.SHORT_RESOLUTION if is_short else config.LONG_RESOLUTION
    orientation = "portrait" if is_short else "landscape"

    scenes = content_item.get("scenes") or []
    if not scenes:
        raise ValueError("content item has no scenes to assemble")

    scene_video_paths = []
    for i, scene in enumerate(scenes):
        duration = float(scene.get("duration_seconds") or 0)
        if duration <= 0:
            print(f"  scene {i}: no audio duration, skipping")
            continue

        raw_clip = os.path.join(work_dir, f"raw_{i:03d}.mp4")
        final_clip = os.path.join(work_dir, f"final_{i:03d}.mp4")
        keyword = scene.get("visual_keyword") or content_item.get("niche", "abstract")

        found = _fetch_clip_for_scene(keyword, orientation, raw_clip)
        if not found:
            _make_fallback_clip(duration, resolution, raw_clip, seed=f"{content_item.get('id')}-{i}")

        caption_png = render_caption_png(
            scene.get("narration", ""), resolution,
            os.path.join(work_dir, f"cap_{i:03d}.png"),
        )

        try:
            _render_scene(raw_clip, scene["audio_path"], duration,
                          resolution, caption_png, final_clip)
        except MediaError as exc:
            # A single corrupt stock clip shouldn't kill the whole video --
            # regenerate this scene over the fallback background instead.
            print(f"  [scene {i} failed on stock footage ({exc}); retrying on fallback]")
            _make_fallback_clip(duration, resolution, raw_clip, seed=f"retry-{i}")
            _render_scene(raw_clip, scene["audio_path"], duration,
                          resolution, caption_png, final_clip)

        scene_video_paths.append(final_clip)
        print(f"  scene {i} assembled ({duration:.1f}s): {keyword}")

        # Free disk on the runner; raw stock clips are by far the biggest files.
        for tmp in (raw_clip, caption_png):
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    if not scene_video_paths:
        raise RuntimeError("no scenes could be assembled")

    concat_list_path = os.path.join(work_dir, "concat.txt")
    with open(concat_list_path, "w") as f:
        for p in scene_video_paths:
            escaped = os.path.abspath(p).replace("'", r"'\''")
            f.write(f"file '{escaped}'\n")

    output_path = os.path.join(work_dir, f"{content_item['id']}.mp4")
    try:
        run_ffmpeg(["-f", "concat", "-safe", "0", "-i", concat_list_path,
                    "-c", "copy", "-movflags", "+faststart", output_path],
                   description="concat")
    except MediaError as exc:
        print(f"  [stream-copy concat failed ({exc}); re-encoding]")
        run_ffmpeg(["-f", "concat", "-safe", "0", "-i", concat_list_path,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-r", str(TARGET_FPS), "-video_track_timescale", TIMESCALE,
                    "-c:a", "aac", "-b:a", "128k", "-ar", str(AUDIO_RATE),
                    "-ac", str(AUDIO_CHANNELS), "-movflags", "+faststart",
                    output_path],
                   description="concat (re-encode)")

    for p in scene_video_paths:
        try:
            os.remove(p)
        except OSError:
            pass

    print(f"Final video assembled: {output_path}")
    return output_path


if __name__ == "__main__":
    item_path = sys.argv[1]
    with open(item_path) as f:
        item = json.load(f)
    work_dir = os.path.join(config.DATA_DIR, "video", item["id"])
    os.makedirs(work_dir, exist_ok=True)
    assemble_video(item, work_dir)
