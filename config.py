"""Central configuration for the car-focused YouTube automation pipeline."""

import os

# Secrets are read only from environment / GitHub Actions secrets.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "")
YT_CLIENT_ID = os.environ.get("YOUTUBE_CLIENT_ID", "")
YT_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
YT_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")
YOUTUBE_CHANNEL_ID = os.environ.get("YOUTUBE_CHANNEL_ID", "")
YOUTUBE_DATA_API_KEY = os.environ.get("YOUTUBE_DATA_API_KEY", "")

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

SHORT_RESOLUTION = (1080, 1920)
LONG_RESOLUTION = (1920, 1080)
SHORT_MAX_SECONDS = 59
LONG_TARGET_SECONDS = 9 * 60  # prompts target 8–12 minutes; narration controls final length

# Deliberately sustainable rather than spammy. This is within the normal free
# YouTube API quota and leaves room for retries and metadata calls.
SHORTS_PER_DAY = 3
LONGFORM_PER_WEEK = 3
NICHE_REEVALUATE_DAYS = 30
CONTENT_LANGUAGES = ["English", "Hindi", "Hinglish"]

# A single coherent channel focus is more valuable than an automated niche hop.
# Research may choose among these sub-angles, but never leaves the car space.
CANDIDATE_NICHES = [
    "automotive news and new car launches",
    "EV technology and charging explained",
    "supercars and hypercars explained",
    "self driving and car technology myth vs reality",
    "Indian car market and future cars",
    "car engineering and hidden features",
    "car industry comparisons and business strategy",
    "future cars and AI automotive design",
]

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CURRENT_NICHE_FILE = os.path.join(DATA_DIR, "current_niche.json")
CONTENT_QUEUE_FILE = os.path.join(DATA_DIR, "content_queue.json")
UPLOAD_LOG_FILE = os.path.join(DATA_DIR, "upload_log.json")
