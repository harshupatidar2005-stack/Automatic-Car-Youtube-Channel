"""
video_assembler.py
-------------------
Turns a content_item (with narration audio already generated per scene) into
a finished mp4: fetches matching free stock footage per scene from Pexels
(fallback: Pixabay), trims/loops each clip to match its scene's audio
duration, burns in simple captions, concatenates everything, and muxes in
the voiceover track.

Free stack: Pexels/Pixabay stock video APIs + ffmpeg (both free).
"""

import json
import os
import subprocess
import sys
import requests

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


def _search_pexels_video(keyword: str, orientation: str) -> str | None:
    if not config.PEXELS_API_KEY:
        return None
    url = "https://api.pexels.com/videos/search"
    headers = {"Authorization": config.PEXELS_API_KEY}
    params = {"query": keyword, "orientation": orientation, "per_page": 1}
    resp = requests.get(url, headers=headers, params=params, timeout=20).json()
    videos = resp.get("videos", [])
    if not videos:
        return None
    # pick a reasonably sized file (not the 4k original, to save bandwidth/time)
    files = sorted(videos[0]["video_files"], key=lambda f: f.get("width", 0))
    for f in files:
        if 720 <= f.get("width", 0) <= 1280:
            return f["link"]
    return files[-1]["link"] if files else None


def _search_pixabay_video(keyword: str) -> str | None:
    if not config.PIXABAY_API_KEY:
        return None
    url = "https://pixabay.com/api/videos/"
    params = {"key": config.PIXABAY_API_KEY, "q": keyword, "per_page": 3}
    resp = requests.get(url, params=params, timeout=20).json()
    hits = resp.get("hits", [])
    if not hits:
        return None
    return hits[0]["videos"]["medium"]["url"]


def _download(url: str, out_path: str):
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)


def _fetch_clip_for_scene(keyword: str, orientation: str, out_path: str) -> bool:
    link = _search_pexels_video(keyword, orientation) or _search_pixabay_video(keyword)
    if not link:
        print(f"  [no stock footage found for '{keyword}', will use solid fallback]")
        return False
    _download(link, out_path)
    return True


def _make_fallback_clip(duration: float, resolution: tuple, out_path: str):
    """Solid dark background as a safety net if no stock clip is found."""
    w, h = resolution
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"color=c=0x101015:s={w}x{h}:d={duration}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path
    ], check=True, capture_output=True)


def _trim_or_loop_to_duration(src_path: str, duration: float, resolution: tuple, out_path: str):
    w, h = resolution
    vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    subprocess.run([
        "ffmpeg", "-y", "-stream_loop", "-1", "-i", src_path,
        "-t", str(duration), "-vf", vf, "-an",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path
    ], check=True, capture_output=True)


def _burn_caption(src_path: str, text: str, out_path: str):
    safe_text = text.replace("'", r"\'").replace(":", r"\:")
    drawtext = (
        f"drawtext=text='{safe_text}':fontcolor=white:fontsize=44:"
        f"box=1:boxcolor=black@0.55:boxborderw=18:"
        f"x=(w-text_w)/2:y=h-th-120:line_spacing=8:"
        f"fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    )
    subprocess.run([
        "ffmpeg", "-y", "-i", src_path, "-vf", drawtext,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "copy", out_path
    ], check=True, capture_output=True)


def assemble_video(content_item: dict, work_dir: str) -> str:
    os.makedirs(work_dir, exist_ok=True)
    is_short = content_item["format"] == "short"
    resolution = config.SHORT_RESOLUTION if is_short else config.LONG_RESOLUTION
    orientation = "portrait" if is_short else "landscape"

    scene_video_paths = []
    for i, scene in enumerate(content_item["scenes"]):
        raw_clip = os.path.join(work_dir, f"raw_{i:03d}.mp4")
        fitted_clip = os.path.join(work_dir, f"fit_{i:03d}.mp4")
        captioned_clip = os.path.join(work_dir, f"cap_{i:03d}.mp4")
        final_clip = os.path.join(work_dir, f"final_{i:03d}.mp4")

        found = _fetch_clip_for_scene(scene["visual_keyword"], orientation, raw_clip)
        if not found:
            _make_fallback_clip(scene["duration_seconds"], resolution, raw_clip)

        _trim_or_loop_to_duration(raw_clip, scene["duration_seconds"], resolution, fitted_clip)
        _burn_caption(fitted_clip, scene["narration"], captioned_clip)

        # mux this scene's own narration onto its own visual segment
        subprocess.run([
            "ffmpeg", "-y", "-i", captioned_clip, "-i", scene["audio_path"],
            "-c:v", "copy", "-c:a", "aac", "-shortest", final_clip
        ], check=True, capture_output=True)
        scene_video_paths.append(final_clip)
        print(f"  scene {i} assembled: {scene['visual_keyword']}")

    # concat all scenes
    concat_list_path = os.path.join(work_dir, "concat.txt")
    with open(concat_list_path, "w") as f:
        for p in scene_video_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")

    output_path = os.path.join(work_dir, f"{content_item['id']}.mp4")
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path,
        "-c", "copy", output_path
    ], check=True, capture_output=True)

    print(f"Final video assembled: {output_path}")
    return output_path


if __name__ == "__main__":
    item_path = sys.argv[1]
    with open(item_path) as f:
        item = json.load(f)
    work_dir = os.path.join(config.DATA_DIR, "video", item["id"])
    os.makedirs(work_dir, exist_ok=True)
    assemble_video(item, work_dir)
