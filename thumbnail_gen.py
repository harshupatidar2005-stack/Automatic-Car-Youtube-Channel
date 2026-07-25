"""
thumbnail_gen.py
-----------------
Generates a clickable thumbnail for free using PIL: grabs a frame from the
assembled video, darkens it slightly, and overlays bold title text.
No paid image-generation API needed.

Robustness notes (each fixes a failure that actually occurred):
  * The font path was hardcoded to one Debian location; anywhere else raised
    OSError. Font discovery now lives in media.font_path().
  * ffmpeg/ffprobe were assumed to be on PATH.
  * Extraction at a fixed t=2.0s fails on clips shorter than 2s (producing a
    zero-byte file that Pillow then refuses to open). We clamp the timestamp
    to the real duration and fall back through several positions.
  * The title was force-fit into 3 lines at a fixed character width, so long
    titles overflowed the canvas. Text is now wrapped to pixel width and the
    font auto-shrinks to fit.
  * Output was written without checking YouTube's 2MB thumbnail limit; a
    1080x1920 JPEG at q=92 can exceed it. Quality steps down until it fits.
  * `img.resize(target)` stretched non-matching aspect ratios; we now
    center-crop to preserve the framing.
"""

import os
import sys

from PIL import Image, ImageDraw, ImageEnhance

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

from media import MediaError, load_font, probe_duration, run_ffmpeg

MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024


def _extract_frame(video_path: str, out_path: str) -> str:
    """Grab a representative frame, tolerating very short clips."""
    try:
        duration = probe_duration(video_path)
    except Exception:
        duration = 0.0

    # Prefer a frame a little way in (avoids fade-ins / black first frames).
    candidates = []
    if duration > 0:
        candidates = [min(max(duration * 0.25, 0.5), max(duration - 0.2, 0.1)),
                      duration * 0.5, 0.5, 0.0]
    else:
        candidates = [2.0, 0.5, 0.0]

    last_error = None
    for ts in candidates:
        try:
            run_ffmpeg(["-ss", f"{max(ts, 0):.3f}", "-i", video_path,
                        "-frames:v", "1", "-q:v", "2", out_path],
                       description="thumbnail frame extraction")
            if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
                return out_path
        except MediaError as exc:
            last_error = exc
    raise MediaError(f"could not extract a frame from {video_path}: {last_error}")


def _cover_resize(img: Image.Image, target: tuple) -> Image.Image:
    """Scale + center-crop to target, preserving aspect ratio (no stretching)."""
    tw, th = target
    sw, sh = img.size
    scale = max(tw / sw, th / sh)
    new_size = (max(tw, int(sw * scale + 0.5)), max(th, int(sh * scale + 0.5)))
    img = img.resize(new_size, Image.LANCZOS)
    left = (img.width - tw) // 2
    top = (img.height - th) // 2
    return img.crop((left, top, left + tw, top + th))


def _wrap_to_width(draw, text: str, font, max_width: int, max_lines: int):
    words = text.split()
    lines, current = [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        box = draw.textbbox((0, 0), trial, font=font)
        if box[2] - box[0] <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
            if len(lines) == max_lines:
                break
    if current and len(lines) < max_lines:
        lines.append(current)
    return lines


def generate_thumbnail(video_path: str, title: str, out_path: str, is_short: bool) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    frame_path = os.path.splitext(out_path)[0] + "_frame.jpg"
    _extract_frame(video_path, frame_path)

    try:
        img = Image.open(frame_path).convert("RGB")
        target_size = config.SHORT_RESOLUTION if is_short else (1280, 720)
        img = _cover_resize(img, target_size)
        img = ImageEnhance.Brightness(img).enhance(0.55)
        img = ImageEnhance.Contrast(img).enhance(1.15)

        draw = ImageDraw.Draw(img)
        width, height = target_size
        usable = int(width * 0.88)
        max_lines = 3

        text = " ".join(str(title or "").split()).upper() or "NEW VIDEO"

        # Shrink the font until the title fits in max_lines.
        font_size = int(height * (0.085 if is_short else 0.115))
        while font_size > 14:
            font = load_font(font_size)
            lines = _wrap_to_width(draw, text, font, usable, max_lines)
            rendered = " ".join(lines).replace(" ", "")
            if rendered and len(rendered) >= len(text.replace(" ", "")) * 0.92:
                break
            font_size = int(font_size * 0.9)
        else:
            font = load_font(14)
            lines = _wrap_to_width(draw, text, font, usable, max_lines)

        line_h = int(font_size * 1.16)
        total_h = line_h * len(lines)
        y = (height - total_h) // 2
        stroke = max(3, font_size // 12)

        for line in lines:
            box = draw.textbbox((0, 0), line, font=font)
            x = (width - (box[2] - box[0])) // 2
            draw.text((x, y), line, font=font, fill="white",
                      stroke_width=stroke, stroke_fill="black")
            y += line_h

        # Step quality down until we're under YouTube's 2MB limit.
        for quality in (92, 85, 78, 70, 60, 50):
            img.save(out_path, "JPEG", quality=quality, optimize=True)
            if os.path.getsize(out_path) <= MAX_THUMBNAIL_BYTES:
                break
    finally:
        if os.path.exists(frame_path):
            try:
                os.remove(frame_path)
            except OSError:
                pass

    print(f"Thumbnail saved: {out_path} ({os.path.getsize(out_path) // 1024}KB)")
    return out_path


if __name__ == "__main__":
    video_path, title, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    generate_thumbnail(video_path, title, out_path, is_short="short" in video_path.lower())
