"""
Central configuration for the YouTube automation pipeline.
Everything content-related is decided by the code at runtime (niche_research.py)
-- this file only holds structural/operational settings, not the niche itself.
"""

import os

# ---------------------------------------------------------------------------
# Secrets (never hardcode these -- pulled from environment / GitHub Actions secrets)
# ---------------------------------------------------------------------------
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")             # free tier: console.groq.com
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")         # free: pexels.com/api
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "")       # free: pixabay.com/api/docs
YT_CLIENT_ID = os.environ.get("YOUTUBE_CLIENT_ID", "")
YT_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
YT_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")

# ---------------------------------------------------------------------------
# LLM settings (free tier via Groq, OpenAI-compatible endpoint)
# ---------------------------------------------------------------------------
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL = "llama-3.3-70b-versatile"   # check console.groq.com/docs/models for current free model

# ---------------------------------------------------------------------------
# Video format specs
# ---------------------------------------------------------------------------
SHORT_RESOLUTION = (1080, 1920)   # 9:16
LONG_RESOLUTION = (1920, 1080)    # 16:9
SHORT_MAX_SECONDS = 59
LONG_TARGET_SECONDS = 8 * 60      # ~8 min sweet spot for mid-roll ads + retention

# ---------------------------------------------------------------------------
# Posting cadence (tune freely -- this is what GitHub Actions cron will trigger)
# ---------------------------------------------------------------------------
SHORTS_PER_DAY = 3
LONGFORM_PER_WEEK = 3

# ---------------------------------------------------------------------------
# Niche re-evaluation cadence
# ---------------------------------------------------------------------------
NICHE_REEVALUATE_DAYS = 30   # re-run niche_research.py monthly to catch market shifts

# ---------------------------------------------------------------------------
# Candidate niche pool the research script scores every cycle.
# This is a starting seed list, not the final choice -- niche_research.py
# scores each one against live trend + competition data and picks the winner.
# Edit this list any time to broaden/narrow what the bot is allowed to consider.
# ---------------------------------------------------------------------------
CANDIDATE_NICHES = [
    "true crime facts",
    "personal finance tips",
    "AI news explained",
    "space and astronomy facts",
    "psychology facts",
    "history mysteries",
    "productivity and self improvement",
    "tech gadget reviews",
    "mythology and folklore",
    "science experiments explained",
    "stoic philosophy motivation",
    "health and longevity facts",
    "unsolved mysteries",
    "animal facts",
    "geography and maps facts",
]

# Data file locations
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CURRENT_NICHE_FILE = os.path.join(DATA_DIR, "current_niche.json")
CONTENT_QUEUE_FILE = os.path.join(DATA_DIR, "content_queue.json")
UPLOAD_LOG_FILE = os.path.join(DATA_DIR, "upload_log.json")
