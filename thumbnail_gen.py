"""
thumbnail_gen.py
-----------------
Generates a clickable thumbnail for free using PIL: grabs a frame from the
assembled video, darkens it slightly, and overlays bold title text.
No paid image-generation API needed.
"""

import os
import subprocess
import sys
import textwrap

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

FONT_PATH_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _extract_frame(video_path: str, out_path: str, timestamp_sec: float = 2.0):
    subprocess.run([
        "ffmpeg", "-y", "-ss", str(timestamp_sec), "-i", video_path,
        "-frames:v", "1", out_path
    ], check=True, capture_output=True)


def generate_thumbnail(video_path: str, title: str, out_path: str, is_short: bool):
    frame_path = out_path.replace(".jpg", "_frame.jpg")
    _extract_frame(video_path, frame_path)

    img = Image.open(frame_path).convert("RGB")
    target_size = config.SHORT_RESOLUTION if is_short else (1280, 720)
    img = img.resize(target_size)

    # darken for text contrast
    img = ImageEnhance.Brightness(img).enhance(0.6)

    draw = ImageDraw.Draw(img)
    font_size = int(target_size[1] * 0.11)
    font = ImageFont.truetype(FONT_PATH_BOLD, font_size)

    wrap_width = 16 if is_short else 22
    lines = textwrap.wrap(title.upper(), width=wrap_width)[:3]

    total_text_h = len(lines) * (font_size + 10)
    y = (target_size[1] - total_text_h) // 2

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        w = bbox[2] - bbox[0]
        x = (target_size[0] - w) // 2
        # simple stroke for readability
        for dx, dy in [(-3, 0), (3, 0), (0, -3), (0, 3)]:
            draw.text((x + dx, y + dy), line, font=font, fill="black")
        draw.text((x, y), line, font=font, fill="white")
        y += font_size + 10

    img.save(out_path, quality=92)
    os.remove(frame_path)
    print(f"Thumbnail saved: {out_path}")
    return out_path


if __name__ == "__main__":
    video_path, title, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    generate_thumbnail(video_path, title, out_path, is_short="short" in video_path)
