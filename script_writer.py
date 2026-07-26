"""
script_writer.py
-----------------
Generates one long-form script + N derived Shorts scripts for the current
niche, with zero human input. Uses Groq's free-tier LLM endpoint
(OpenAI-compatible). Swap GROQ_* config values for any other free/paid
OpenAI-compatible endpoint if you like.

Output: a JSON "content item" with title, description, tags, and a list of
scenes (each with narration text + a visual search keyword used later to
fetch matching stock footage).

Robustness notes (each fixes a failure that actually occurred):
  * JSON extraction handled ``` fences but not the prose LLMs habitually wrap
    around them ("Here's your script: {...} Hope this helps!"), nor trailing
    commas -- both raised JSONDecodeError and killed the run. Parsing now
    brace-matches the JSON object out of any surrounding text, repairs
    trailing commas, and retries the request with a corrective message.
  * `response_format: json_object` is requested so Groq enforces valid JSON.
  * Only HTTP 429 was retried; connection resets and 5xx crashed the run.
  * Nothing validated the model's output shape, so a missing "scenes" key
    surfaced as a KeyError three modules later, mid-render.
  * Shorts are trimmed to fit config.SHORT_MAX_SECONDS -- an over-length
    "Short" is silently published by YouTube as a regular video.
"""

import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import requests


SYSTEM_PROMPT = """You are a YouTube content strategist and scriptwriter for a
faceless, voiceover-driven channel. You write scripts optimized for retention:
strong hook in the first 3 seconds, punchy sentences, no fluff, a clear payoff.
You always respond with STRICT JSON only, no markdown fences, no commentary."""

# Rough spoken-word rate used to sanity-check Shorts length before render.
WORDS_PER_SECOND = 2.6


class ScriptGenerationError(RuntimeError):
    pass


def _call_llm(prompt: str, max_retries: int = 5, expect_json: bool = True) -> str:
    if not config.GROQ_API_KEY:
        raise ScriptGenerationError(
            "GROQ_API_KEY is not set. Add it as a GitHub Actions secret "
            "(Settings -> Secrets and variables -> Actions)."
        )

    headers = {
        "Authorization": f"Bearer {config.GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": config.GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.85,
        "max_tokens": 4000,
    }
    if expect_json:
        # Let Groq enforce syntactically valid JSON server-side.
        body["response_format"] = {"type": "json_object"}

    last_error = None
    for attempt in range(max_retries):
        try:
            resp = requests.post(f"{config.GROQ_BASE_URL}/chat/completions",
                                 headers=headers, json=body, timeout=120)
        except requests.RequestException as exc:
            # Connection resets/timeouts were previously fatal.
            last_error = exc
            wait = min(60, 5 * (attempt + 1))
            print(f"  [Groq connection error ({type(exc).__name__}), retry "
                  f"{attempt + 1}/{max_retries} in {wait}s]")
            time.sleep(wait)
            continue

        if resp.status_code == 429:
            wait = 20 * (attempt + 1)
            try:
                wait = int(float(resp.headers.get("Retry-After", wait)))
            except (TypeError, ValueError):
                pass
            wait = min(wait, 120)
            print(f"  [Groq rate limit hit, waiting {wait}s before retry "
                  f"{attempt + 1}/{max_retries}]")
            time.sleep(wait)
            continue

        if resp.status_code >= 500:
            wait = min(60, 5 * (attempt + 1))
            print(f"  [Groq {resp.status_code}, retry {attempt + 1}/{max_retries} in {wait}s]")
            time.sleep(wait)
            continue

        if resp.status_code == 400 and expect_json:
            # Some models reject response_format; retry once without it.
            print("  [Groq rejected response_format, retrying as plain text]")
            return _call_llm(prompt, max_retries=max_retries - attempt, expect_json=False)

        if resp.status_code in (401, 403):
            raise ScriptGenerationError(
                f"Groq rejected the API key ({resp.status_code}). Check GROQ_API_KEY."
            )

        if resp.status_code == 404:
            raise ScriptGenerationError(
                f"Groq model '{config.GROQ_MODEL}' not found (404). It was likely "
                f"decommissioned -- pick a current one from "
                f"https://console.groq.com/docs/models and update GROQ_MODEL in config.py."
            )

        resp.raise_for_status()
        try:
            return resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            last_error = exc
            print(f"  [unexpected Groq response shape, retry {attempt + 1}/{max_retries}]")
            time.sleep(5)

    raise ScriptGenerationError(
        f"Groq API unavailable after {max_retries} retries (last error: {last_error}). "
        f"Try again later or reduce queue size."
    )


LONGFORM_PROMPT_TEMPLATE = """
Channel: an evidence-led car documentary channel about cars, EVs, engineering,
Indian and global auto markets, motorsport technology, and the future of mobility.
Niche angle: {niche}
Language: {language} (use natural native phrasing; Hinglish may mix Hindi and English)

Write ONE original 8–12 minute documentary-style video (1500–1900 spoken words)
on a timely, specific car topic with a clear question or conflict. Choose a fresh
angle yourself; never invent a launch, quote, statistic, test result, or source.
Use careful language where facts may change and include named sources/links in
the description when possible. Build a narrative: cold open, context, stakes,
comparison, counterargument, and a useful conclusion. Be informative, not hype.

Return STRICT JSON with this exact schema:
{{
  "title": "clickable but not clickbait-lying title, under 70 chars",
  "description": "4-6 sentence keyword-rich description: hook, what is explained, named factual sources or source types, and a clear subscribe/comment CTA. Disclose that visuals may be AI-generated.",
  "tags": ["8 to 12 relevant tags"],
  "hook": "the first 2 spoken sentences, must grab attention immediately",
  "scenes": [
    {{"narration": "2-5 spoken sentences for this beat", "visual_keyword": "2-5 word cinematic car stock-footage search term matching this beat"}}
    ... (aim for 24-36 varied scenes total; each scene must have a distinct visual idea)
  ]
}}

The narration fields must contain ONLY spoken words -- no stage directions,
no speaker labels, no bracketed notes, no emoji.
"""

SHORTS_PROMPT_TEMPLATE = """
Channel: evidence-led car explainers
Niche: {niche}
Language: {language}
Source long-form title for inspiration (do not repeat it; extract a genuinely different sub-angle): {longform_title}

Write ONE original YouTube Short: 45-55 seconds spoken (~130-160 words), one
verifiable car insight, an immediate hook, fast but human pacing, and a memorable
last line. Never use unsafe driving advice, fabricated specs, or empty hype.

Return STRICT JSON with this exact schema:
{{
  "title": "under 60 chars, includes a hook word, no hashtags in title",
  "description": "2 short sentences with a factual promise, source cue, CTA, and an AI-visual disclosure",
  "tags": ["6 to 10 relevant tags"],
  "scenes": [
    {{"narration": "1-2 spoken sentences", "visual_keyword": "2-4 word stock footage search term"}}
    ... (aim for 6-10 scenes total)
  ]
}}

The narration fields must contain ONLY spoken words -- no stage directions,
no speaker labels, no bracketed notes, no emoji. Total narration across all
scenes must stay under 160 words.
"""


def _extract_json_object(raw: str) -> str:
    """Pull the outermost {...} out of a response that may be wrapped in prose."""
    text = (raw or "").strip()

    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()

    start = text.find("{")
    if start == -1:
        return text

    # Brace-match, ignoring braces inside strings.
    depth, in_string, escaped = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]


def _parse_json_response(raw: str) -> dict:
    candidate = _extract_json_object(raw)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    # Repair the two mistakes LLMs make most: trailing commas and stray
    # control characters inside strings.
    repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
    repaired = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", repaired)
    return json.loads(repaired)


_BRACKETED = re.compile(r"\[[^\]]*\]|\([^)]*\)")
# Applied after bracket removal, so a label like "[music] Narrator:" is still
# caught even though it is no longer at the start of the string.
_SPEAKER_LABEL = re.compile(
    r"^\s*(?:narrator|host|voiceover|voice over|vo|speaker)\s*:\s*", re.IGNORECASE)


def _clean_narration(text: str) -> str:
    """Strip stage directions/speaker labels so the TTS doesn't read them aloud."""
    cleaned = _BRACKETED.sub(" ", str(text or ""))
    cleaned = re.sub(r"[*_#`]+", "", cleaned)
    cleaned = " ".join(cleaned.split())
    # A line can carry several stacked labels ("Narrator: Host: ...").
    for _ in range(3):
        stripped = _SPEAKER_LABEL.sub("", cleaned)
        if stripped == cleaned:
            break
        cleaned = stripped
    return cleaned.strip()


def _validate_and_normalise(data: dict, fmt: str, niche: str, language: str = "English") -> dict:
    """Guarantee the downstream contract instead of failing deep in the render."""
    if not isinstance(data, dict):
        raise ScriptGenerationError(f"model returned {type(data).__name__}, expected an object")

    title = str(data.get("title") or "").strip()
    if not title:
        raise ScriptGenerationError("model returned no title")

    raw_scenes = data.get("scenes")
    if not isinstance(raw_scenes, list) or not raw_scenes:
        raise ScriptGenerationError("model returned no scenes")

    scenes = []
    for scene in raw_scenes:
        if not isinstance(scene, dict):
            continue
        narration = _clean_narration(scene.get("narration"))
        if not narration:
            continue
        keyword = " ".join(str(scene.get("visual_keyword") or "").split()) or niche
        scenes.append({"narration": narration, "visual_keyword": keyword[:80]})

    if not scenes:
        raise ScriptGenerationError("model returned scenes but none had usable narration")

    # A "Short" over 60s is published as a normal video, silently breaking the
    # entire Shorts strategy -- trim to fit.
    if fmt == "short":
        budget_words = int(config.SHORT_MAX_SECONDS * WORDS_PER_SECOND)
        kept, used = [], 0
        for scene in scenes:
            words = len(scene["narration"].split())
            if used + words > budget_words and kept:
                break
            kept.append(scene)
            used += words
        if len(kept) < len(scenes):
            print(f"  [trimmed short from {len(scenes)} to {len(kept)} scenes "
                  f"to fit {config.SHORT_MAX_SECONDS}s]")
        scenes = kept

    tags = data.get("tags")
    if not isinstance(tags, list):
        tags = []
    clean_tags, seen = [], set()
    for tag in tags:
        tag = " ".join(str(tag).split()).strip("#")
        key = tag.lower()
        if tag and key not in seen and len(tag) <= 60:
            seen.add(key)
            clean_tags.append(tag)
    if not clean_tags:
        clean_tags = [niche]

    description = " ".join(str(data.get("description") or "").split())
    if not description:
        description = f"{title} -- a short look at {niche}."
    # Make the disclosure consistent even when the model forgets it. This is
    # transparency, not a substitute for reviewing realistic synthetic scenes.
    if "ai-generated" not in description.lower() and "ai generated" not in description.lower():
        description += " Visuals may include AI-generated reconstructions; factual claims are presented for education."

    return {
        "id": str(uuid.uuid4()),
        "format": fmt,
        "niche": niche,
        "language": language,
        "title": title[:100],
        "description": description,
        "tags": clean_tags,
        "hook": " ".join(str(data.get("hook") or "").split()),
        "scenes": scenes,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _generate(prompt: str, fmt: str, niche: str, language: str = "English", attempts: int = 3) -> dict:
    last_error = None
    for attempt in range(attempts):
        try:
            raw = _call_llm(prompt)
            return _validate_and_normalise(_parse_json_response(raw), fmt, niche, language)
        except (json.JSONDecodeError, ScriptGenerationError) as exc:
            last_error = exc
            print(f"  [script attempt {attempt + 1}/{attempts} unusable: "
                  f"{type(exc).__name__}: {str(exc)[:140]}]")
            if attempt < attempts - 1:
                time.sleep(4)
    raise ScriptGenerationError(f"could not generate a valid {fmt} script: {last_error}")


def generate_longform(niche: str, language: str = "English") -> dict:
    return _generate(LONGFORM_PROMPT_TEMPLATE.format(niche=niche, language=language),
                     "longform", niche, language)


def generate_short(niche: str, longform_title: str, language: str = "English") -> dict:
    return _generate(
        SHORTS_PROMPT_TEMPLATE.format(niche=niche, longform_title=longform_title, language=language),
        "short", niche, language,
    )


def _load_queue():
    if os.path.exists(config.CONTENT_QUEUE_FILE):
        try:
            with open(config.CONTENT_QUEUE_FILE) as f:
                queue = json.load(f)
            if isinstance(queue, list):
                return queue
            print("[content_queue.json is not a list, starting fresh]")
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[content_queue.json unreadable ({exc}), starting fresh]")
    return []


def _save_queue(queue):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    tmp_path = config.CONTENT_QUEUE_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(queue, f, indent=2)
    os.replace(tmp_path, config.CONTENT_QUEUE_FILE)  # atomic


def refill_queue(niche: str, shorts_per_longform: int = 3):
    """Generates one long-form + a few derived Shorts and appends to the queue.

    Partial success is kept: if Shorts generation dies halfway, whatever was
    produced is still saved rather than discarded.
    """
    queue = _load_queue()
    longform_title = None

    try:
        # Rotate languages so the channel serves Hindi, English and Hinglish
        # without translating the same video three times.
        language = config.CONTENT_LANGUAGES[len(queue) % len(config.CONTENT_LANGUAGES)]
        longform = generate_longform(niche, language)
        queue.append(longform)
        longform_title = longform["title"]
        print(f"Generated long-form: {longform_title} ({len(longform['scenes'])} scenes)")
        _save_queue(queue)
    except ScriptGenerationError as exc:
        print(f"[long-form generation failed: {exc}]")

    for i in range(shorts_per_longform):
        time.sleep(5)  # stay under free-tier rate limits
        try:
            short = generate_short(niche, longform_title or niche)
            queue.append(short)
            print(f"Generated short: {short['title']} ({len(short['scenes'])} scenes)")
            _save_queue(queue)
        except ScriptGenerationError as exc:
            print(f"[short {i + 1} generation failed: {exc}]")

    return queue


if __name__ == "__main__":
    with open(config.CURRENT_NICHE_FILE) as f:
        niche_data = json.load(f)
    refill_queue(niche_data["niche"])
