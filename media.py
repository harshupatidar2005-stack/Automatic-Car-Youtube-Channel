"""
media.py
---------
Shared low-level media + runtime helpers used by the rest of the pipeline.

Why this module exists (all of these were real breakages):

  * `ffmpeg`/`ffprobe` are not always on PATH (GitHub runners have them, most
    dev boxes and slim containers don't). We resolve a usable binary once,
    falling back to the `imageio-ffmpeg` wheel, and we can measure durations
    with plain ffmpeg when ffprobe is absent entirely.
  * Not every ffmpeg build ships the `drawtext` filter (it needs libfreetype
    at compile time). Burning captions with drawtext therefore fails hard on
    those builds, and drawtext's escaping rules mangle apostrophes/colons/
    percent signs that appear constantly in narration. We render captions to
    a transparent PNG with Pillow and `overlay` them instead, which works on
    every build and needs no text escaping at all.
  * Fonts are not guaranteed to live at one hardcoded path.
"""

import os
import re
import shutil
import subprocess

from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont


class MediaError(RuntimeError):
    """Raised when an ffmpeg invocation fails, with the tail of stderr attached."""


# ---------------------------------------------------------------------------
# Binary discovery
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def ffmpeg_bin() -> str:
    """Path to a usable ffmpeg binary (PATH first, then the imageio-ffmpeg wheel)."""
    env = os.environ.get("FFMPEG_BINARY")
    if env and (os.path.isfile(env) or shutil.which(env)):
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - only hit on a broken install
        raise MediaError(
            "No ffmpeg binary available. Install ffmpeg system-wide, or "
            "`pip install imageio-ffmpeg`, or set FFMPEG_BINARY."
        ) from exc


@lru_cache(maxsize=1)
def ffprobe_bin() -> str | None:
    """Path to ffprobe, or None. Absence is tolerated -- see probe_duration()."""
    env = os.environ.get("FFPROBE_BINARY")
    if env and (os.path.isfile(env) or shutil.which(env)):
        return env
    return shutil.which("ffprobe")


@lru_cache(maxsize=1)
def has_filter(name: str = "drawtext") -> bool:
    """Whether this ffmpeg build exposes a given filter."""
    try:
        out = subprocess.run([ffmpeg_bin(), "-hide_banner", "-filters"],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return False
    return re.search(rf"^\s*\S+\s+{re.escape(name)}\s", out, re.MULTILINE) is not None


@lru_cache(maxsize=1)
def font_path() -> str:
    """First available bold TTF, searched across common distro locations."""
    env = os.environ.get("CAPTION_FONT")
    if env and os.path.isfile(env):
        return env
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    for root in ("/usr/share/fonts", "/usr/local/share/fonts",
                 os.path.expanduser("~/.fonts")):
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                if fn.lower().endswith((".ttf", ".otf")):
                    return os.path.join(dirpath, fn)
    raise MediaError(
        "No usable TrueType font found. Install fonts-dejavu (Debian/Ubuntu) "
        "or set CAPTION_FONT to a .ttf path."
    )


def load_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_path(), size)


# ---------------------------------------------------------------------------
# ffmpeg invocation
# ---------------------------------------------------------------------------
def run_ffmpeg(args: list, *, timeout: int = 900, description: str = "ffmpeg"):
    """Run ffmpeg with the resolved binary, raising MediaError with real context.

    The original code used check=True + capture_output=True, so any failure
    surfaced as a bare CalledProcessError with the actual reason swallowed.
    """
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error", "-y"] + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"{description}: timed out after {timeout}s") from exc
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-8:])
        raise MediaError(f"{description}: ffmpeg exited {proc.returncode}\n{tail}")
    return proc


_DURATION_RE = re.compile(r"time=(\d+):(\d\d):(\d\d(?:\.\d+)?)")


def probe_duration(path: str) -> float:
    """Duration of a media file in seconds.

    Prefers ffprobe, but falls back to decoding with ffmpeg when ffprobe isn't
    installed. The original implementation assumed ffprobe always existed and
    that its stdout always parsed as a float -- both assumptions crash the run
    (FileNotFoundError / ValueError) rather than degrading.
    """
    probe = ffprobe_bin()
    if probe:
        proc = subprocess.run(
            [probe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=120,
        )
        raw = (proc.stdout or "").strip()
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass  # fall through to the ffmpeg decode path

    proc = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-nostdin", "-i", path, "-f", "null", "-"],
        capture_output=True, text=True, timeout=600,
    )
    matches = _DURATION_RE.findall(proc.stderr or "")
    if not matches:
        raise MediaError(f"Could not determine duration of {path}")
    h, m, s = matches[-1]
    return int(h) * 3600 + int(m) * 60 + float(s)


# ---------------------------------------------------------------------------
# Captions rendered with Pillow (no drawtext dependency, no escaping hazards)
# ---------------------------------------------------------------------------
def render_caption_png(text: str, resolution: tuple, out_path: str,
                       max_lines: int = 4) -> str | None:
    """Render narration text as a transparent PNG sized to the video frame.

    Returns the path, or None when there is nothing to draw.
    """
    text = " ".join((text or "").split())
    if not text:
        return None

    width, height = resolution
    is_portrait = height >= width

    # Wrap to the frame width rather than a fixed character count, so long
    # narration lines can never run off the edge of the screen.
    usable = int(width * 0.86)
    scratch = ImageDraw.Draw(Image.new("RGBA", (8, 8)))

    def wrap_at(font) -> list:
        def text_width(s: str) -> int:
            box = scratch.textbbox((0, 0), s, font=font)
            return box[2] - box[0]

        out, current = [], ""
        for word in text.split():
            trial = f"{current} {word}".strip()
            if text_width(trial) <= usable or not current:
                current = trial
            else:
                out.append(current)
                current = word
        if current:
            out.append(current)
        return out

    # Shrink the font until the whole line fits in max_lines. Truncating the
    # caption would show the viewer different words than they hear, so text
    # is only clipped as an absolute last resort.
    start_size = max(18, int(width * (0.052 if is_portrait else 0.036)))
    min_size = max(12, int(start_size * 0.55))
    font_size = start_size
    while True:
        font = load_font(font_size)
        lines = wrap_at(font)
        if len(lines) <= max_lines or font_size <= min_size:
            break
        font_size = max(min_size, int(font_size * 0.92))

    if not lines:
        return None
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" ,;:") + "..."

    def text_width(s: str) -> int:
        box = scratch.textbbox((0, 0), s, font=font)
        return box[2] - box[0]

    line_h = int(font_size * 1.28)
    pad = int(font_size * 0.5)
    block_h = line_h * len(lines) + pad * 2
    block_w = min(width - int(width * 0.06), max(text_width(l) for l in lines) + pad * 2)

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    bottom_margin = int(height * (0.17 if is_portrait else 0.08))
    top = height - bottom_margin - block_h
    left = (width - block_w) // 2
    draw.rectangle([left, top, left + block_w, top + block_h], fill=(0, 0, 0, 150))

    y = top + pad
    stroke = max(2, font_size // 14)
    for line in lines:
        x = (width - text_width(line)) // 2
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255),
                  stroke_width=stroke, stroke_fill=(0, 0, 0, 235))
        y += line_h

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    img.save(out_path)
    return out_path
