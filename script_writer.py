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
"""

import json
import os
import sys
import time
import uuid
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
import requests


SYSTEM_PROMPT = """You are a YouTube content strategist and scriptwriter for a
faceless, voiceover-driven channel. You write scripts optimized for retention:
strong hook in the first 3 seconds, punchy sentences, no fluff, a clear payoff.
You always respond with STRICT JSON only, no markdown fences, no commentary."""


def _call_llm(prompt: str, max_retries: int = 5) -> str:
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
    for attempt in range(max_retries):
        resp = requests.post(f"{config.GROQ_BASE_URL}/chat/completions",
                              headers=headers, json=body, timeout=60)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 20 * (attempt + 1)))
            print(f"  [Groq rate limit hit, waiting {wait}s before retry "
                  f"{attempt + 1}/{max_retries}]")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    raise RuntimeError("Groq API still rate-limited after all retries -- "
                        "try again later or reduce SHORTS_PER_DAY / queue size.")


LONGFORM_PROMPT_TEMPLATE = """
Niche: {niche}

Write ONE long-form YouTube video script (~1100-1400 words, ~8 minutes spoken)
on a specific, non-generic topic within this niche that has not been done to death.
Pick the exact angle/topic yourself.

Return STRICT JSON with this exact schema:
{{
  "title": "clickable but not clickbait-lying title, under 70 chars",
  "description": "2-3 sentence YouTube description with 1 natural keyword-rich sentence",
  "tags": ["8 to 12 relevant tags"],
  "hook": "the first 2 spoken sentences, must grab attention immediately",
  "scenes": [
    {{"narration": "1-3 spoken sentences for this beat", "visual_keyword": "2-4 word stock footage search term matching this beat"}}
    ... (aim for 18-28 scenes total covering the full script)
  ]
}}
"""

SHORTS_PROMPT_TEMPLATE = """
Niche: {niche}
Source long-form title for inspiration (do not just repeat it, extract a punchy sub-angle): {longform_title}

Write ONE YouTube Shorts script: 45-55 seconds spoken (~130-160 words), single
idea, big hook in first line, fast pacing, a punchy last line that lands.

Return STRICT JSON with this exact schema:
{{
  "title": "under 60 chars, includes a hook word, no hashtags in title",
  "description": "1 sentence description",
  "tags": ["6 to 10 relevant tags"],
  "scenes": [
    {{"narration": "1-2 spoken sentences", "visual_keyword": "2-4 word stock footage search term"}}
    ... (aim for 6-10 scenes total)
  ]
}}
"""


def _parse_json_response(raw: str) -> dict:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.replace("json\n", "", 1)
    return json.loads(cleaned)


def generate_longform(niche: str) -> dict:
    raw = _call_llm(LONGFORM_PROMPT_TEMPLATE.format(niche=niche))
    data = _parse_json_response(raw)
    data["id"] = str(uuid.uuid4())
    data["format"] = "longform"
    data["niche"] = niche
    data["created_at"] = datetime.utcnow().isoformat()
    return data


def generate_short(niche: str, longform_title: str) -> dict:
    raw = _call_llm(SHORTS_PROMPT_TEMPLATE.format(niche=niche, longform_title=longform_title))
    data = _parse_json_response(raw)
    data["id"] = str(uuid.uuid4())
    data["format"] = "short"
    data["niche"] = niche
    data["created_at"] = datetime.utcnow().isoformat()
    return data


def _load_queue():
    if os.path.exists(config.CONTENT_QUEUE_FILE):
        with open(config.CONTENT_QUEUE_FILE) as f:
            return json.load(f)
    return []


def _save_queue(queue):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(config.CONTENT_QUEUE_FILE, "w") as f:
        json.dump(queue, f, indent=2)


def refill_queue(niche: str, shorts_per_longform: int = 3):
    """Generates one long-form + a few derived Shorts and appends to the queue."""
    queue = _load_queue()

    longform = generate_longform(niche)
    queue.append(longform)
    print(f"Generated long-form: {longform['title']}")

    for _ in range(shorts_per_longform):
        time.sleep(5)  # small gap between calls to stay under free-tier rate limits
        short = generate_short(niche, longform["title"])
        queue.append(short)
        print(f"Generated short: {short['title']}")

    _save_queue(queue)
    return queue


if __name__ == "__main__":
    with open(config.CURRENT_NICHE_FILE) as f:
        niche_data = json.load(f)
    refill_queue(niche_data["niche"])
